import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from call_secretary.budget import BudgetError, Deadline, Ledger, RateCard, units
from call_secretary.config import ConfigError
from call_secretary.storage import Store


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN")])
def test_invalid_cost_is_never_accounted(value):
    with pytest.raises(BudgetError):
        units(value)


def test_ledger_refuses_unreserved_closed_or_unknown_paid_work(tmp_path):
    with Store(tmp_path / "state") as store:
        ledger = Ledger(store, Decimal(50))
        cid = store.create_call("fake")
        with pytest.raises(BudgetError):
            ledger.month(datetime(2026, 1, 1))
        with pytest.raises(BudgetError):
            ledger.reserve(cid, Decimal(0))
        with pytest.raises(BudgetError):
            ledger.consume(cid, "e", Decimal(1))
        with pytest.raises(BudgetError):
            ledger.reconcile(cid, Decimal(1))
        ledger.reserve(cid, Decimal(2))
        ledger.reconcile(cid, Decimal("1.25"))
        assert ledger.status()["accounted_eur"] == "1.25"
        with pytest.raises(BudgetError):
            ledger.reserve(cid, Decimal(2))
        with pytest.raises(BudgetError):
            ledger.consume(cid, "e", Decimal(1))


@pytest.mark.parametrize(
    "changes",
    [
        {"BILLING_REVIEWED_ON": "invalid"},
        {"BILLING_REVIEWED_ON": "2000-01-01"},
        {"REALTIME_MODEL": "unreviewed-model"},
    ],
)
def test_paid_calls_require_valid_reviewed_rates(live_env, changes):
    with pytest.raises(ConfigError):
        RateCard.load(live_env | changes)


def test_background_paid_work_checks_review_freshness(rates):
    rates.assert_current()
    with pytest.raises(ConfigError):
        replace(rates, reviewed_on=None).assert_current()
    with pytest.raises(ConfigError):
        replace(rates, reviewed_on=datetime(2000, 1, 1, tzinfo=UTC).date()).assert_current()


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": True, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0, "input_token_details": []},
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "input_token_details": {},
            "output_token_details": [],
        },
        {"input_tokens": 0, "output_tokens": 0, "input_token_details": {}},
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "input_token_details": {"audio_tokens": 1},
            "output_token_details": {},
        },
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "input_token_details": {},
            "output_token_details": {"audio_tokens": 1},
        },
    ],
)
def test_provider_usage_must_be_complete_and_consistent(rates, usage):
    with pytest.raises(BudgetError):
        rates.response_cost(usage)


def test_invalid_deadline_does_not_execute_paid_operation():
    async def scenario():
        ran = []

        async def operation():
            ran.append(True)

        task = operation()
        try:
            with pytest.raises(BudgetError):
                await Deadline(0).run(task)
        finally:
            task.close()
        assert ran == []

    asyncio.run(scenario())
