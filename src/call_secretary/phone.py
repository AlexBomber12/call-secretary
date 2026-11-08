"""Hardware-gated phone service; injectable transports permit complete offline journeys."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from call_secretary import health
from call_secretary.audio import FRAME_BYTES, AudioEndpoint, FatalAudioError
from call_secretary.budget import BudgetError, RateCard
from call_secretary.calls import CallController
from call_secretary.config import ConfigError, Settings
from call_secretary.fallback_audio import local_phrase
from call_secretary.isolated_audio import AudioSpec, IsolatedAudio
from call_secretary.locking import InstanceLock
from call_secretary.modem import ModemError, at_query, open_serial
from call_secretary.routing import Router
from call_secretary.sim7600 import ATChannel, CallControl, ObservedCall
from call_secretary.storage import Store
from call_secretary.summaries import destination
from call_secretary.voice import Realtime, connect_api


@dataclass(frozen=True)
class PhoneOptions:
    device: str
    ingress: str
    audio: AudioSpec

    @classmethod
    def load(cls, settings: Settings) -> PhoneOptions:
        env = settings.env
        if env.get("PHONE_H1_CONFIRMED") != "true":
            raise ConfigError("Phone service requires recorded H1 hardware acceptance")
        required = ("MODEM_CONTROL_DEVICE", "MODEM_AUDIO_DEVICE", "PHONE_INGRESS_ID")
        if any(not env.get(key, "").strip() for key in required):
            raise ConfigError("Set explicit accepted modem, audio names and ingress")
        try:
            rate = int(env.get("MODEM_AUDIO_RATE", ""))
        except ValueError:
            raise ConfigError("Set the H1 verified mono PCM16 sample rate") from None
        if rate not in {8000, 16000, 24000, 48000}:
            raise ConfigError("Unsupported phone audio sample rate")
        return cls(
            env[required[0]],
            env[required[2]],
            AudioSpec(
                env.get("MODEM_AUDIO_INPUT") or env[required[1]],
                env.get("MODEM_AUDIO_OUTPUT") or env[required[1]],
                rate,
            ),
        )


def open_control(device: str) -> CallControl:
    port = open_serial(device)
    try:
        if not any("SIM7600" in line for line in at_query(port, "AT+CGMM")):
            raise ModemError("Selected device is not a SIM7600; no call commands sent")
        return CallControl(ATChannel(port))
    except BaseException:
        port.close()
        raise


async def race_call(operation: Coroutine[Any, Any, None], watch: Coroutine[Any, Any, None]) -> bool:
    tasks = [asyncio.create_task(operation), asyncio.create_task(watch)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        return tasks[1] in done  # Ended or replaced by a different logical call.
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, FatalAudioError):
                raise result  # Fatal cleanup outranks caller/USB completion.


async def handle_call(
    settings: Settings,
    store: Store,
    options: PhoneOptions,
    control: CallControl,
    observed: ObservedCall,
    phrase: bytes,
    *,
    audio_factory: Callable[[AudioSpec], Awaitable[AudioEndpoint]] = IsolatedAudio.open,
    connector: Any = connect_api,
) -> str:
    call = CallController(store, settings.family, observed.incoming(options.ingress))
    endpoint: AudioEndpoint | None = None
    voice: Realtime | None = None
    rates: RateCard | None = None
    attempted = False
    audio_ok = False
    status = "interrupted"
    deadline = call.started + settings.family.max_duration_seconds

    async def watch() -> None:
        while True:
            await asyncio.sleep(0.5)
            current = await asyncio.to_thread(control.poll)
            health.update(store, "modem", "active")
            if current is None or current.token != observed.token:
                return

    async def converse() -> None:
        nonlocal voice, rates, attempted, status, endpoint
        endpoint = await audio_factory(options.audio)
        health.update(store, "audio", "active")
        await asyncio.to_thread(control.answer, observed.token)
        for _ in range(6):
            current = await asyncio.to_thread(control.poll)
            if current is None or current.token != observed.token:
                raise ModemError("Answered call ended or was replaced")
            if current.state == 0:
                break
            await asyncio.sleep(0.1)
        else:
            raise ModemError("Modem did not confirm an active answered call")
        try:
            settings.require("OPENAI_API_KEY", "REALTIME_MODEL", "REALTIME_VOICE")
            rates = RateCard.load(settings.env)
            call.ledger.reserve(
                call.cid,
                rates.reservation(
                    call.router.max_prompt_bytes(), settings.family.max_duration_seconds
                ),
                horizon_seconds=settings.family.max_duration_seconds + 60,
            )
            call.paid, call.rates = True, rates
            attempted = True
            health.update(store, "voice", "starting")
            async with connector(settings.env["OPENAI_API_KEY"], rates.model) as socket:
                router = Router(settings.family)
                voice = Realtime(
                    socket,
                    endpoint,
                    call,
                    rates,
                    router.instructions(call.profile),
                    router.tools(call.profile),
                    settings.env["REALTIME_VOICE"],
                )
                health.update(store, "voice", "active")

                async def closing() -> None:
                    await asyncio.sleep(max(0, deadline - time.monotonic() - 10))
                    assert voice is not None
                    await voice.close_soon()

                closer = asyncio.create_task(closing())
                monitor = asyncio.create_task(call.monitor(voice))
                try:
                    await voice.run()
                    status = (
                        "ended"
                        if voice.committed_items <= voice.transcribed_items
                        else "ended_text_incomplete"
                    )
                finally:
                    closer.cancel()
                    monitor.cancel()
                    await asyncio.gather(closer, monitor, return_exceptions=True)
        except FatalAudioError:
            raise  # Never recover while a native worker may still own hardware.
        except Exception as exc:
            status = "budget_refused" if isinstance(exc, BudgetError) else "provider_or_audio_error"
            health.update(store, "voice", "unavailable")
            # The watcher remains active while the local prompt plays.
            if attempted:
                # Realtime cancellation may have killed a native output lane.
                # Cleanup must succeed before opening replacement workers; an
                # unreaped native failure stays fatal through final cleanup.
                await endpoint.close()
                endpoint = await audio_factory(options.audio)
            await endpoint.interrupt()
            for offset in range(0, len(phrase), FRAME_BYTES):
                await endpoint.write(phrase[offset : offset + FRAME_BYTES])
            await asyncio.sleep(min(2, endpoint.latency_ms / 1000))

    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            if await race_call(converse(), watch()):
                status = "caller_hangup"
    except TimeoutError:
        status = "duration_limit"
    except FatalAudioError:
        status = "audio_error"
        raise
    except Exception:
        status = "provider_or_audio_error"
    finally:
        try:
            # CallControl rechecks ownership: never hang up a replacement/foreign call.
            await asyncio.to_thread(control.hangup, observed.token)
        except ModemError:
            health.update(store, "modem", "unavailable")
        try:
            if endpoint:
                await endpoint.close()
                audio_ok = True
        except Exception:
            status = "audio_error"
            raise
        finally:
            try:
                if voice and rates:
                    amount = Decimal(voice.input_bytes) / (24000 * 2 * 60)
                    call.ledger.consume(
                        call.cid,
                        call.cid + ":transcription_allowance",
                        amount * rates.transcription_usd_per_minute * rates.usd_to_eur_upper_bound,
                    )
            finally:
                complete = not attempted or bool(
                    voice and voice.usage_complete and status == "ended"
                )
                call.finish(status, usage_complete=complete)
                health.update(
                    store,
                    "audio",
                    "idle" if audio_ok and status != "audio_error" else "unavailable",
                )
                if status in {"ended", "ended_text_incomplete", "caller_hangup", "duration_limit"}:
                    health.update(store, "voice", "idle")
    return call.cid


async def run_phone(settings: Settings) -> None:
    options = PhoneOptions.load(settings)  # Gate before opening devices or generating speech.
    settings.require("TELEGRAM_BOT_TOKEN")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        phrase = await asyncio.to_thread(local_phrase)
        with (
            Store(settings.state_dir, mode="live") as store,
            InstanceLock(settings.state_dir / "runtime.lock"),
        ):
            if not any(p.enabled for p in settings.family.profiles) or not all(
                destination(store, p) for p in settings.family.profiles if p.enabled
            ):
                raise ConfigError("Enable and enroll phone recipients first")
            store.recover_calls()
            while not stop.is_set():
                control = None
                fatal_error = None
                try:
                    health.update(store, "modem", "starting")
                    control = await asyncio.to_thread(open_control, options.device)
                    while not stop.is_set():
                        health.update(store, "modem", "ready")
                        observed = await asyncio.to_thread(control.poll)
                        if observed:
                            task = asyncio.create_task(
                                handle_call(settings, store, options, control, observed, phrase)
                            )
                            stopping = asyncio.create_task(stop.wait())
                            try:
                                await asyncio.wait(
                                    (task, stopping), return_when=asyncio.FIRST_COMPLETED
                                )
                                if stopping.done():
                                    task.cancel()
                                await task
                            except asyncio.CancelledError:
                                if not stop.is_set():
                                    raise
                            finally:
                                task.cancel()
                                stopping.cancel()
                                results = await asyncio.gather(
                                    task, stopping, return_exceptions=True
                                )
                                for result in results:
                                    if isinstance(result, FatalAudioError):
                                        raise result
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=0.5)
                        except TimeoutError:
                            pass
                except FatalAudioError as exc:
                    fatal_error = exc
                    raise
                except ModemError:
                    health.update(store, "modem", "unavailable")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        pass
                finally:
                    if control:
                        try:
                            await asyncio.to_thread(control.channel.close)
                        except ModemError:
                            health.update(store, "modem", "unavailable")
                            if fatal_error is not None:
                                raise fatal_error from None
                            raise
            for component in ("modem", "audio", "voice"):
                health.update(store, component, "stopped")
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
