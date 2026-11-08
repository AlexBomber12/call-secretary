from __future__ import annotations

import asyncio
from decimal import Decimal

from call_secretary.audio import AudioEndpoint, Microphone
from call_secretary.budget import Deadline, RateCard
from call_secretary.config import ConfigError, Settings
from call_secretary.locking import InstanceLock
from call_secretary.routing import Incoming, Router
from call_secretary.storage import Store
from call_secretary.voice import Realtime, VoiceError, connect_api


async def run_microphone(settings: Settings, profile_id: str | None) -> str:
    from call_secretary.calls import CallController

    settings.validate_live()
    rates = RateCard.load(settings.env)
    if not any(p.enabled for p in settings.family.profiles):
        raise ConfigError("Enable a profile before microphone testing")
    router = Router(settings.family)
    with (
        Store(settings.state_dir, mode="live") as store,
        InstanceLock(store.path.parent / "runtime.lock"),
    ):
        store.recover_calls()
        controller = CallController(
            store,
            settings.family,
            Incoming("microphone"),
            rates=rates,
            paid=True,
            operator_profile=profile_id,
        )
        cid = controller.cid
        endpoint: AudioEndpoint | None = None
        voice: Realtime | None = None
        closer: asyncio.Task[None] | None = None
        monitor: asyncio.Task[None] | None = None
        status = "interrupted"
        attempted = False
        try:
            endpoint = Microphone()
            attempted = True
            async with connect_api(settings.env["OPENAI_API_KEY"], rates.model) as socket:
                session = Realtime(
                    socket,
                    endpoint,
                    controller,
                    rates,
                    router.instructions(controller.profile),
                    router.tools(controller.profile),
                    settings.env["REALTIME_VOICE"],
                )
                voice = session

                async def closing() -> None:
                    await asyncio.sleep(max(1, settings.family.max_duration_seconds - 10))
                    await session.close_soon()

                closer = asyncio.create_task(closing())
                monitor = asyncio.create_task(controller.monitor(session))
                await Deadline(settings.family.max_duration_seconds).run(session.run())
                status = (
                    "ended"
                    if session.committed_items <= session.transcribed_items
                    else "ended_text_incomplete"
                )
        except TimeoutError:
            status = "duration_limit"
        except Exception:
            status = "provider_or_audio_error"
        finally:
            if closer:
                closer.cancel()
                await asyncio.gather(closer, return_exceptions=True)
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if endpoint:
                try:
                    await endpoint.close()
                except Exception:
                    status = "audio_error"
            complete = bool(not attempted or (voice and voice.usage_complete and status == "ended"))
            try:
                if voice:
                    allowance = (
                        Decimal(voice.input_bytes)
                        / (24000 * 2 * 60)
                        * rates.transcription_usd_per_minute
                        * rates.usd_to_eur_upper_bound
                    )
                    controller.ledger.consume(cid, f"{cid}:transcription_allowance", allowance)
            finally:
                controller.finish(status, usage_complete=complete)
        if status not in {"ended", "ended_text_incomplete", "duration_limit"}:
            raise VoiceError("Voice prototype interrupted; pending state retained")
        return cid
