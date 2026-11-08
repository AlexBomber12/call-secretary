import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from call_secretary.budget import BudgetError, Deadline, Ledger, RateCard
from call_secretary.config import ConfigError
from call_secretary.storage import Store


def test_reserved_unknown_spend_survives_restart_and_usage_is_idempotent(tmp_path):
    with Store(tmp_path / "state") as s:
        cid = s.create_call("shared")
        ledger = Ledger(s, Decimal("50"))
        ledger.reserve(cid, Decimal("20"))
        ledger.consume(cid, "response-1", Decimal("2"))
        ledger.consume(cid, "response-1", Decimal("2"))
        ledger.settle(cid, complete=False)
    with Store(tmp_path / "state") as s:
        ledger = Ledger(s, Decimal("50"))
        assert ledger.status()["committed_eur"] == "20"
        assert ledger.status()["accounted_eur"] == "2"
        ledger.settle(cid, complete=True)
        assert ledger.status()["committed_eur"] == "2"


def test_concurrent_reservations_cannot_overspend_month(tmp_path):
    with Store(tmp_path / "state") as s:
        ids = [s.create_call("shared") for _ in range(3)]
        ledger = Ledger(s, Decimal("50"))

        def reserve(cid):
            try:
                ledger.reserve(cid, Decimal("20"))
                return True
            except BudgetError:
                return False

        with ThreadPoolExecutor(max_workers=3) as pool:
            assert sum(pool.map(reserve, ids)) == 2
        assert ledger.status()["committed_eur"] == "40"


def test_month_boundary_reserves_in_both_rome_months_and_new_month_is_not_reset(tmp_path):
    with Store(tmp_path / "state") as s:
        ledger = Ledger(s, Decimal("50"))
        cid = s.create_call("shared")
        now = datetime(2026, 11, 30, 22, 59, tzinfo=UTC)  # 23:59 Europe/Rome
        ledger.reserve(cid, Decimal("30"), now=now)
        later = datetime(2026, 12, 1, 0, tzinfo=UTC)
        assert ledger.status(now=later)["committed_eur"] == "30"
        with pytest.raises(BudgetError):
            ledger.reserve(s.create_call("shared"), Decimal("30"), now=later)


def test_actual_overrun_is_saved_and_stops_further_work(tmp_path):
    with Store(tmp_path / "state") as s:
        ledger = Ledger(s, Decimal("50"))
        cid = s.create_call("shared")
        ledger.reserve(cid, Decimal("2"))
        assert not ledger.consume(cid, "unexpected", Decimal("3"))
        assert ledger.status()["committed_eur"] == "3"


def test_unknown_or_stale_rates_do_not_enable_paid_requests():
    with pytest.raises(ConfigError):
        RateCard.load({})
    with pytest.raises(ConfigError):
        RateCard.load({"BILLING_REVIEWED_ON": "2026-01-01"}, today=date(2026, 11, 8))
    rates = RateCard.load(
        {
            "BILLING_REVIEWED_ON": "2026-11-08",
            "USD_TO_EUR_UPPER_BOUND": "1.5",
            "REALTIME_MODEL": "gpt-realtime-2.1",
        },
        today=date(2026, 11, 8),
    )
    assert rates.reservation(2000, 600) > 0
    assert (
        rates.response_cost(
            {
                "input_tokens": 100,
                "output_tokens": 100,
                "input_token_details": {"audio_tokens": 50},
                "output_token_details": {"audio_tokens": 50},
            }
        )
        > 0
    )


def test_deadline_cancels_an_unresponsive_provider_without_waiting_ten_minutes():
    async def scenario():
        cancelled = asyncio.Event()

        async def stuck():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(TimeoutError):
            await Deadline(0.02).run(stuck())
        assert cancelled.is_set()

    asyncio.run(scenario())
