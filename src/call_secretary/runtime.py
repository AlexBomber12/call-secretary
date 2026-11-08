from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from call_secretary.audio import AudioEndpoint, Microphone
from call_secretary.budget import BudgetError, Deadline, Ledger, RateCard
from call_secretary.config import ConfigError, Profile, Settings
from call_secretary.routing import Router
from call_secretary.storage import Store
from call_secretary.voice import Realtime, ToolReply, VoiceError, connect_api


class RecordingHooks:
    def __init__(self, store: Store, ledger: Ledger, rates: RateCard, cid: str):
        self.store, self.ledger, self.rates, self.cid = store, ledger, rates, cid

    async def text(
        self, item_id: str, role: str, text: str, interrupted: bool, created: float
    ) -> None:
        self.store.utterance(self.cid, item_id, role, text, interrupted=interrupted, now=created)

    async def interrupted(self, item_id: str) -> None:
        self.store.interrupt_utterance(self.cid, item_id)

    async def usage(self, response_id: str, usage: dict[str, Any]) -> None:
        cost = self.rates.response_cost(usage)
        if not self.ledger.consume(self.cid, f"{self.cid}:{response_id}", cost):
            raise BudgetError("Provider usage exceeded reserved allowance")

    async def tool(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        if name == "finish_call" and not arguments:
            return ToolReply("finished", finish=True)
        return ToolReply("unsupported_tool")


def prompt_reservation(router: Router, rates: RateCard, seconds: int) -> Decimal:
    profiles: list[Profile | None] = [None, *router.family.profiles]
    size = max(
        len(router.instructions(p).encode("utf-8"))
        + len(json.dumps(router.tools(p), ensure_ascii=False).encode("utf-8"))
        for p in profiles
    )
    return rates.reservation(size, seconds)


async def run_microphone(settings: Settings, profile_id: str) -> str:
    settings.validate_live()
    rates = RateCard.load(settings.env)
    profile = settings.family.profile(profile_id)
    if not profile.enabled:
        raise ConfigError("Enable the selected profile before microphone testing")
    router = Router(settings.family)
    with Store(settings.state_dir) as store:
        cid = store.create_call("microphone")
        store.route_call(cid, profile.id, "operator_selected")
        ledger = Ledger(store, settings.family.monthly_budget_eur, settings.family.timezone)
        try:
            ledger.reserve(
                cid,
                prompt_reservation(router, rates, settings.family.max_duration_seconds),
                horizon_seconds=settings.family.max_duration_seconds + 60,
            )
        except BudgetError:
            store.end_call(cid, "budget_refused")
            raise
        hooks = RecordingHooks(store, ledger, rates, cid)
        endpoint: AudioEndpoint | None = None
        voice: Realtime | None = None
        closer: asyncio.Task[None] | None = None
        status = "interrupted"
        attempted = False
        try:
            endpoint = Microphone()
            attempted = True
            async with connect_api(settings.env["OPENAI_API_KEY"], rates.model) as socket:
                session = Realtime(
                    socket,
                    endpoint,
                    hooks,
                    rates,
                    router.instructions(profile),
                    router.tools(profile),
                    settings.env["REALTIME_VOICE"],
                )
                voice = session

                async def closing() -> None:
                    await asyncio.sleep(max(1, settings.family.max_duration_seconds - 10))
                    await session.close_soon()

                closer = asyncio.create_task(closing())
                await Deadline(settings.family.max_duration_seconds).run(session.run())
                status = "ended"
        except TimeoutError:
            status = "duration_limit"
        except Exception:
            status = "provider_or_audio_error"
        finally:
            if closer:
                closer.cancel()
                await asyncio.gather(closer, return_exceptions=True)
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
                    ledger.consume(cid, f"{cid}:transcription_allowance", allowance)
            finally:
                store.end_call(cid, status, voice_usage_complete=complete)
        if status not in {"ended", "duration_limit"}:
            raise VoiceError("Voice prototype interrupted; pending state retained")
        return cid
