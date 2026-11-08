from __future__ import annotations

import asyncio
import time
from typing import Any

from call_secretary.budget import BudgetError, Ledger, RateCard
from call_secretary.config import ConfigError, Family, Profile
from call_secretary.routing import Incoming, Router
from call_secretary.storage import Store
from call_secretary.voice import Realtime, ToolReply


class CallController:
    def __init__(
        self,
        store: Store,
        family: Family,
        incoming: Incoming,
        *,
        rates: RateCard | None = None,
        paid: bool = False,
        operator_profile: str | None = None,
    ):
        self.store, self.family, self.rates, self.paid = store, family, rates, paid
        self.router = Router(family)
        self.ledger = Ledger(store, family.monthly_budget_eur, family.timezone)
        self.profile: Profile | None = None
        self.ended = False
        self.attempts = 0
        self.stop_reason: str | None = None
        self.started = self.last_activity = time.monotonic()
        self.caller_speaking = False
        if paid and rates is None:
            raise ConfigError("Paid calls need reviewed pricing")
        if operator_profile and not family.profile(operator_profile).enabled:
            raise ConfigError("Selected profile is disabled")
        self.cid = store.create_call(
            incoming.ingress_id,
            incoming.caller_number,
            incoming.original_called_number,
            single_active=True,
        )
        resolution = self.router.incoming(incoming)
        if operator_profile:
            self.profile = family.profile(operator_profile)
            store.route_call(self.cid, self.profile.id, "operator_selected")
        elif resolution.profile:
            self.profile = resolution.profile
            store.route_call(self.cid, self.profile.id, resolution.source)
        if paid and rates:
            try:
                self.ledger.reserve(
                    self.cid,
                    rates.reservation(self.router.max_prompt_bytes(), family.max_duration_seconds),
                    horizon_seconds=family.max_duration_seconds + 60,
                )
            except BudgetError:
                self.finish("budget_refused", usage_complete=True)
                raise

    async def activity(self, speaking: bool | None = None) -> None:
        self.last_activity = time.monotonic()
        if speaking is not None:
            self.caller_speaking = speaking

    async def text(
        self, item_id: str, role: str, text: str, interrupted: bool, created: float
    ) -> None:
        if self.ended:
            return
        await self.activity()
        self.store.utterance(self.cid, item_id, role, text, interrupted=interrupted, now=created)

    async def interrupted(self, item_id: str) -> None:
        if not self.ended:
            self.store.interrupt_utterance(self.cid, item_id)

    async def usage(self, response_id: str, usage: dict[str, Any]) -> None:
        if self.ended or not self.paid or self.rates is None:
            return
        cost = self.rates.response_cost(usage)
        if not self.ledger.consume(self.cid, f"{self.cid}:{response_id}", cost):
            raise BudgetError("Provider usage exceeded reserved allowance")

    async def tool(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        if self.ended:
            return ToolReply("call_already_ended", finish=True)
        if name == "finish_call" and not arguments:
            return ToolReply("finished", finish=True)
        if name != "select_recipient" or self.profile is not None:
            return ToolReply("recipient_fixed_or_unsupported_tool")
        self.attempts += 1
        pid = arguments.get("profile_id")
        resolution = (
            self.router.spoken(pid)
            if isinstance(pid, str) and set(arguments) == {"profile_id"}
            else None
        )
        if resolution is None or resolution.profile is None:
            if self.attempts >= 3:
                self.stop_reason = "unassigned"
                return ToolReply("recipient_not_resolved", finish=True)
            return ToolReply("Ask the caller to clarify the intended family member.")
        self.profile = resolution.profile
        self.store.route_call(self.cid, self.profile.id, "caller_selected")
        return ToolReply(
            "Recipient confirmed. Ask what to pass on; do not repeat the full greeting.",
            self.router.instructions(self.profile),
            self.router.tools(self.profile),
        )

    async def monitor(self, voice: Realtime) -> None:
        while not self.ended:
            await asyncio.sleep(1)
            if self.profile is None and time.monotonic() - self.started >= 60:
                self.stop_reason = "recipient_timeout"
            elif not self.caller_speaking and time.monotonic() - self.last_activity >= 60:
                self.stop_reason = "silence_timeout"
            if self.stop_reason:
                await voice.close_soon()
                await asyncio.sleep(10)
                voice.finish_event.set()
                return

    def finish(self, status: str = "ended", *, usage_complete: bool = False) -> None:
        if self.ended:
            return
        self.ended = True
        self.store.end_call(
            self.cid,
            self.stop_reason or status,
            voice_usage_complete=usage_complete or not self.paid,
        )


async def simulate(store: Store, family: Family, recipient: str | None, message: str) -> str:
    controller = CallController(store, family, Incoming("simulation"))
    if recipient:
        await controller.tool("select_recipient", {"profile_id": recipient})
    if message:
        await controller.text("synthetic-caller", "caller", message, False, time.time())
    controller.finish()
    return controller.cid
