"""Conservative, durable EUR reservations; never a promise of provider billing precision."""

from __future__ import annotations

import asyncio
import math
import sqlite3
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
DEFAULT_TRANSCRIPTION_MODEL = "gpt-transcribe"
DEFAULT_SUMMARY_MODEL = "gpt-6-luna"
TRANSCRIPTION_ALLOWANCE = Decimal("0.05")
# Global standard tariffs checked 2026-11-23. Cached input discounts are ignored.
TRANSCRIPTION_TARIFFS = {
    "gpt-transcribe": Decimal("0.0045"),
    "gpt-4o-mini-transcribe": Decimal("0.003"),
}
SUMMARY_TARIFFS = {
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
}
RETIREMENT_DATES = {
    "gpt-4.1-mini": date(2027, 1, 14),
    "gpt-4o-mini-transcribe": date(2027, 2, 26),
}


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
        self,
        cid: str,
        amount: Decimal,
        *,
        now: datetime | None = None,
        horizon_seconds: int = 660,
        incremental: bool = False,
    ) -> None:
        with self.store.transaction() as db:
            self._reserve(
                db,
                cid,
                amount,
                now=now or datetime.now(UTC),
                horizon_seconds=horizon_seconds,
                incremental=incremental,
            )

    def reserve_summary_attempt(self, cid: str, token: str, amount: Decimal) -> bool:
        """Authorize one paid request while the worker still owns the summary job."""
        with self.store.transaction() as db:
            now = datetime.now(UTC)
            lease = db.execute(
                "SELECT 1 FROM summary_jobs WHERE call_id=? AND state='leased' "
                "AND lease_token=? AND lease_until>?",
                (cid, token, now.timestamp()),
            ).fetchone()
            if lease is None:
                return False
            inserted = db.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                ("summary_attempt:" + cid, "started"),
            ).rowcount
            if not inserted:
                return False
            self._reserve(db, cid, amount, now=now, horizon_seconds=60, incremental=True)
            return True

    def _reserve(
        self,
        db: sqlite3.Connection,
        cid: str,
        amount: Decimal,
        *,
        now: datetime,
        horizon_seconds: int,
        incremental: bool,
    ) -> None:
        months = {self.month(now), self.month(now + timedelta(seconds=horizon_seconds))}
        requested = units(amount)
        if requested <= 0:
            raise BudgetError("A positive reservation is required")
        for month in months:
            current = db.execute(
                "SELECT * FROM budget_reservations WHERE call_id=? AND month=?", (cid, month)
            ).fetchone()
            if current and current["closed"]:
                raise BudgetError("Settled calls cannot make additional paid requests")
            previous = max(current["reserved"], current["consumed"]) if current else 0
            # Follow-up work needs its full allowance above already consumed
            # usage, including when the provider exceeded the original hold.
            additional = current["consumed"] if current and incremental else 0
            wanted = max(requested + additional, previous)
            total = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN closed=1 THEN consumed "
                "ELSE MAX(reserved,consumed) END),0) "
                "FROM budget_reservations WHERE month=?",
                (month,),
            ).fetchone()[0]
            if total - previous + wanted > self.limit:
                raise BudgetError("Monthly family API budget has no sufficient free reservation")
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
            unfinished = db.execute(
                "SELECT 1 FROM calls WHERE id=? AND ended IS NULL UNION ALL "
                "SELECT 1 FROM summary_jobs WHERE call_id=? AND state IN ('pending','leased')",
                (cid, cid),
            ).fetchone()
            if unfinished:
                raise BudgetError(
                    "Wait for the call and summary work to finish before reconciliation"
                )
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


def review_state(reviewed: date, today: date) -> str:
    remaining = 30 - (today - reviewed).days
    if remaining > 30:
        return "invalid"
    if remaining < 0:
        return "expired"
    return "expires_soon" if remaining <= 5 else "current"


def billing_status(env: dict[str, str], *, today: date | None = None) -> dict[str, str | None]:
    try:
        reviewed = date.fromisoformat(env.get("BILLING_REVIEWED_ON", ""))
    except ValueError:
        return {"state": "unreviewed", "reviewed_on": None, "valid_through": None}
    valid_through = reviewed + min(timedelta(days=30), date.max - reviewed)
    return {
        "state": review_state(reviewed, today or datetime.now(UTC).date()),
        "reviewed_on": reviewed.isoformat(),
        "valid_through": valid_through.isoformat(),
    }


def count_tokens(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise BudgetError("Malformed provider usage")
    return value


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
    transcription_usd_per_minute: Decimal = TRANSCRIPTION_ALLOWANCE
    reviewed_on: date | None = None
    transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL
    summary_model: str = DEFAULT_SUMMARY_MODEL

    @classmethod
    def load(cls, env: dict[str, str], *, today: date | None = None) -> RateCard:
        today = today or datetime.now(UTC).date()
        try:
            reviewed = date.fromisoformat(env.get("BILLING_REVIEWED_ON", ""))
        except ValueError:
            raise ConfigError(
                "Set BILLING_REVIEWED_ON after reviewing current provider tariffs"
            ) from None
        if review_state(reviewed, today) in {"expired", "invalid"}:
            raise ConfigError("Billing policy review is stale or in the future")
        fx = money(env.get("USD_TO_EUR_UPPER_BOUND", ""), "USD_TO_EUR_UPPER_BOUND")
        model = env.get("REALTIME_MODEL", "")
        # Per-million Realtime tariffs by modality.
        prices = {
            "gpt-realtime-2.1": ("32", "64", "4", "24"),
            "gpt-realtime-2.1-mini": ("10", "20", "0.60", "2.40"),
        }
        if model not in prices:
            raise ConfigError("Realtime model needs an explicitly reviewed rate card")
        audio_in, audio_out, text_in, text_out = (Decimal(n) for n in prices[model])
        transcription = env.get("TRANSCRIPTION_MODEL") or DEFAULT_TRANSCRIPTION_MODEL
        summary = env.get("SUMMARY_MODEL") or DEFAULT_SUMMARY_MODEL
        if transcription not in TRANSCRIPTION_TARIFFS:
            raise ConfigError("Transcription model needs a reviewed Realtime tariff")
        if summary not in SUMMARY_TARIFFS:
            raise ConfigError("Summary model needs a reviewed structured-output tariff")
        card = cls(
            model,
            fx,
            audio_in,
            audio_out,
            text_in,
            text_out,
            reviewed_on=reviewed,
            transcription_usd_per_minute=max(
                TRANSCRIPTION_ALLOWANCE, TRANSCRIPTION_TARIFFS[transcription]
            ),
            transcription_model=transcription,
            summary_model=summary,
        )
        card.assert_current(today=today)
        return card

    def assert_current(self, *, today: date | None = None) -> None:
        today = today or datetime.now(UTC).date()
        if self.reviewed_on is None or review_state(self.reviewed_on, today) in {
            "invalid",
            "expired",
        }:
            raise ConfigError("Billing review is required before another paid request")
        for model in (self.transcription_model, self.summary_model):
            retired = RETIREMENT_DATES.get(model)
            if retired and today >= retired:
                raise ConfigError("A selected provider model is retired; review the configuration")

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
        # Allow 48 kB of transcript plus the instructions and structured output schema.
        price_in, price_out = SUMMARY_TARIFFS[self.summary_model]
        return (50000 * price_in + 768 * price_out) / MICRO * self.usd_to_eur_upper_bound

    def summary_tariff(self, usage: Any) -> dict[str, str]:
        if not isinstance(usage, dict):
            raise BudgetError("Malformed summary usage")
        tokens_in = count_tokens(usage.get("input_tokens"))
        tokens_out = count_tokens(usage.get("output_tokens"))
        price_in, price_out = SUMMARY_TARIFFS[self.summary_model]
        amount = (tokens_in * price_in + tokens_out * price_out) / MICRO
        return {
            "model": self.summary_model,
            "input_usd_per_million": str(price_in),
            "output_usd_per_million": str(price_out),
            "usd_to_eur_upper_bound": str(self.usd_to_eur_upper_bound),
            "eur": str(amount * self.usd_to_eur_upper_bound),
        }

    def response_cost(self, usage: dict[str, Any]) -> Decimal:
        total_in = count_tokens(usage.get("input_tokens"))
        total_out = count_tokens(usage.get("output_tokens"))
        detail_in = usage.get("input_token_details", {})
        detail_out = usage.get("output_token_details", {})
        if (
            not isinstance(detail_in, dict)
            or not isinstance(detail_out, dict)
            or "input_token_details" not in usage
            or "output_token_details" not in usage
        ):
            raise BudgetError("Provider usage token details are unavailable")
        audio_in = count_tokens(detail_in.get("audio_tokens", 0))
        audio_out = count_tokens(detail_out.get("audio_tokens", 0))
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
