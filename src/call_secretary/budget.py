"""Conservative, durable EUR reservations; never a promise of provider billing precision."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from call_secretary.config import ConfigError, money
from call_secretary.storage import Store

MICRO = Decimal(1_000_000)
T = TypeVar("T")


class BudgetError(RuntimeError):
    pass


def units(amount: Decimal) -> int:
    if not amount.is_finite() or amount < 0:
        raise BudgetError("Invalid cost amount")
    return int((amount * MICRO).to_integral_value(rounding=ROUND_CEILING))


class Ledger:
    def __init__(self, store: Store, limit: Decimal, timezone: str = "Europe/Rome"):
        self.store = store
        self.limit = units(limit)
        self.zone = ZoneInfo(timezone)

    def month(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise BudgetError("Budget timestamps must include timezone")
        return now.astimezone(self.zone).strftime("%Y-%m")

    def reserve(
        self, cid: str, amount: Decimal, *, now: datetime | None = None, horizon_seconds: int = 660
    ) -> None:
        now = now or datetime.now(UTC)
        months = {self.month(now), self.month(now + timedelta(seconds=horizon_seconds))}
        requested = units(amount)
        if requested <= 0:
            raise BudgetError("A positive reservation is required")
        with self.store.transaction() as db:
            for month in months:
                current = db.execute(
                    "SELECT * FROM budget_reservations WHERE call_id=? AND month=?", (cid, month)
                ).fetchone()
                if current and current["closed"]:
                    raise BudgetError("Settled calls cannot make additional paid requests")
                previous = max(current["reserved"], current["consumed"]) if current else 0
                wanted = max(requested, previous)
                total = db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN closed=1 THEN consumed "
                    "ELSE MAX(reserved,consumed) END),0) "
                    "FROM budget_reservations WHERE month=?",
                    (month,),
                ).fetchone()[0]
                if total - previous + wanted > self.limit:
                    raise BudgetError(
                        "Monthly family API budget has no sufficient free reservation"
                    )
                db.execute(
                    "INSERT INTO budget_reservations(call_id,month,reserved) VALUES(?,?,?) "
                    "ON CONFLICT(call_id,month) DO UPDATE SET reserved=excluded.reserved",
                    (cid, month, wanted),
                )

    def consume(self, cid: str, event_id: str, amount: Decimal) -> bool:
        cost = units(amount)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM budget_reservations WHERE call_id=?", (cid,)
            ).fetchall()
            if not rows or any(r["closed"] for r in rows):
                raise BudgetError("Paid usage has no open reservation")
            inserted = db.execute(
                "INSERT OR IGNORE INTO usage_events VALUES(?,?,?,?)",
                (event_id, cid, cost, time.time()),
            ).rowcount
            if inserted:
                db.execute(
                    "UPDATE budget_reservations SET consumed=consumed+? WHERE call_id=?",
                    (cost, cid),
                )
            # Persist an unexpectedly high bill even when it exceeds its reservation.
            over = db.execute(
                "SELECT 1 FROM budget_reservations WHERE call_id=? AND consumed>reserved", (cid,)
            ).fetchone()
        return over is None

    def settle(self, cid: str, *, complete: bool) -> None:
        if not complete:
            return  # Missing usage never releases the unknown part of a reservation.
        with self.store.transaction() as db:
            db.execute("UPDATE budget_reservations SET closed=1 WHERE call_id=?", (cid,))

    def reconcile(self, cid: str, amount_eur: Decimal) -> None:
        """Explicit operator reconciliation against provider billing, not an automatic reset."""
        amount = units(amount_eur)
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM budget_reservations WHERE call_id=?", (cid,)
            ).fetchone():
                raise BudgetError("Unknown budget reservation")
            db.execute(
                "UPDATE budget_reservations SET consumed=?,closed=1 WHERE call_id=?", (amount, cid)
            )

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        month = self.month(now or datetime.now(UTC))
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(consumed),0),COALESCE(SUM(CASE WHEN closed=1 "
                "THEN consumed ELSE MAX(reserved,consumed) END),0) "
                "FROM budget_reservations WHERE month=?",
                (month,),
            ).fetchone()
        return {
            "month": month,
            "limit_eur": str(Decimal(self.limit) / MICRO),
            "accounted_eur": str(Decimal(row[0]) / MICRO),
            "committed_eur": str(Decimal(row[1]) / MICRO),
            "available_eur": str(Decimal(max(0, self.limit - row[1])) / MICRO),
        }


@dataclass(frozen=True)
class RateCard:
    model: str
    usd_to_eur_upper_bound: Decimal
    audio_in: Decimal
    audio_out: Decimal
    text_in: Decimal
    text_out: Decimal
    max_responses: int = 24
    max_output_tokens: int = 512
    context_tokens: int = 4096
    # Upper accounting allowance, not a quoted transcription tariff.
    transcription_usd_per_minute: Decimal = Decimal("0.05")
    reviewed_on: date | None = None

    @classmethod
    def load(cls, env: dict[str, str], *, today: date | None = None) -> RateCard:
        today = today or datetime.now(UTC).date()
        try:
            reviewed = date.fromisoformat(env.get("BILLING_REVIEWED_ON", ""))
        except ValueError:
            raise ConfigError(
                "Set BILLING_REVIEWED_ON after reviewing current provider tariffs"
            ) from None
        if not 0 <= (today - reviewed).days <= 30:
            raise ConfigError("Billing policy review is stale or in the future")
        fx = money(env.get("USD_TO_EUR_UPPER_BOUND", ""), "USD_TO_EUR_UPPER_BOUND")
        model = env.get("REALTIME_MODEL", "")
        # Official pricing reviewed 2026-11-08; discounts deliberately ignored.
        prices = {
            "gpt-realtime-2.1": ("32", "64", "4", "24"),
            "gpt-realtime-2.1-mini": ("10", "20", "0.60", "2.40"),
        }
        if model not in prices:
            raise ConfigError("Realtime model needs an explicitly reviewed rate card")
        audio_in, audio_out, text_in, text_out = (Decimal(n) for n in prices[model])
        return cls(model, fx, audio_in, audio_out, text_in, text_out, reviewed_on=reviewed)

    def assert_current(self) -> None:
        if (
            self.reviewed_on is None
            or not 0 <= (datetime.now(UTC).date() - self.reviewed_on).days <= 30
        ):
            raise ConfigError("Billing review is required before another paid request")

    def reservation(self, prompt_bytes: int, seconds: int) -> Decimal:
        # A token cannot encode less than a UTF-8 byte; include protocol/tool margin.
        # Worst case charges every context token as the dearer audio/text input type.
        input_cap = (prompt_bytes + self.context_tokens + 2048) * self.max_responses
        output_cap = self.max_output_tokens * self.max_responses
        voice = (
            input_cap * max(self.audio_in, self.text_in)
            + output_cap * max(self.audio_out, self.text_out)
        ) / MICRO
        transcription = Decimal(seconds) / 60 * self.transcription_usd_per_minute
        return (voice + transcription) * self.usd_to_eur_upper_bound + self.summary_reservation()

    def summary_reservation(self) -> Decimal:
        # Summary input is bounded to 48 kB and output to 768 tokens, at 4.1-mini rates.
        return (
            (Decimal(50000) * Decimal("0.40") + 768 * Decimal("1.60"))
            / MICRO
            * self.usd_to_eur_upper_bound
        )

    def response_cost(self, usage: dict[str, Any]) -> Decimal:
        def count(value: Any) -> int:
            if type(value) is not int or value < 0:
                raise BudgetError("Malformed provider usage")
            return value

        total_in = count(usage.get("input_tokens"))
        total_out = count(usage.get("output_tokens"))
        detail_in = usage.get("input_token_details", {})
        detail_out = usage.get("output_token_details", {})
        if (
            not isinstance(detail_in, dict)
            or not isinstance(detail_out, dict)
            or "input_token_details" not in usage
            or "output_token_details" not in usage
        ):
            raise BudgetError("Provider usage token details are unavailable")
        audio_in = count(detail_in.get("audio_tokens", 0))
        audio_out = count(detail_out.get("audio_tokens", 0))
        if audio_in > total_in or audio_out > total_out:
            raise BudgetError("Inconsistent provider usage")
        return (
            (
                audio_in * self.audio_in
                + (total_in - audio_in) * self.text_in
                + audio_out * self.audio_out
                + (total_out - audio_out) * self.text_out
            )
            / MICRO
            * self.usd_to_eur_upper_bound
        )


@dataclass
class Deadline:
    seconds: float
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    async def run(self, operation: Awaitable[T]) -> T:
        if not math.isfinite(self.seconds) or self.seconds <= 0:
            raise BudgetError("Invalid call deadline")
        async with asyncio.timeout(self.remaining()):
            return await operation
