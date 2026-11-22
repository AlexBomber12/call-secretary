import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from call_secretary.budget import BudgetError, Deadline, Ledger, RateCard, billing_status, units
from call_secretary.config import ConfigError
from call_secretary.storage import Store


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN")])
def test_invalid_cost_is_never_accounted(value):
    with pytest.raises(BudgetError):
        units(value)


def test_paid_summary_approval_reserves_once_and_rolls_back_when_budget_is_full(tmp_path):
    with Store(tmp_path / "state") as store:
        ledger = Ledger(store, Decimal(50))
        cid = store.create_call("fake")
        store.end_call(cid)
        token = store.claim_job("summary_jobs")["lease_token"]
        with pytest.raises(BudgetError, match="free reservation"):
            ledger.reserve_summary_attempt(cid, token, Decimal(51))
        assert not store.db.execute(
            "SELECT 1 FROM metadata WHERE key=?", ("summary_attempt:" + cid,)
        ).fetchone()
        assert ledger.status()["committed_eur"] == "0"
        assert ledger.reserve_summary_attempt(cid, token, Decimal(2))
        assert not ledger.reserve_summary_attempt(cid, token, Decimal(4))
        assert ledger.status()["committed_eur"] == "2"


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
        store.end_call(cid)
        job = store.claim_job("summary_jobs")
        store.complete_summary(cid, job["lease_token"], {"report": "synthetic final report"}, None)
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
        {"TRANSCRIPTION_MODEL": "not-a-transcriber"},
        {"SUMMARY_MODEL": "not-a-summary-model"},
    ],
)
def test_paid_calls_require_valid_reviewed_rates(live_env, changes):
    with pytest.raises(ConfigError):
        RateCard.load(live_env | changes)


@pytest.mark.parametrize(
    "age, state",
    [
        (-1, "invalid"),
        (0, "current"),
        (24, "current"),
        (25, "expires_soon"),
        (30, "expires_soon"),
        (31, "expired"),
    ],
)
def test_status_reports_the_five_day_billing_review_window(live_env, age, state):
    reviewed = date(2026, 11, 23)
    env = live_env | {"BILLING_REVIEWED_ON": str(reviewed)}
    today = reviewed + timedelta(days=age)
    status = billing_status(env, today=today)
    assert status == {"state": state, "reviewed_on": str(reviewed), "valid_through": "2026-12-23"}
    if state in {"current", "expires_soon"}:
        RateCard.load(env, today=today).assert_current(today=today)
    else:
        with pytest.raises(ConfigError):
            RateCard.load(env, today=today)


@pytest.mark.parametrize("configured_date", ["", "not-a-date", "9999-12-31"])
def test_billing_status_is_available_for_missing_or_invalid_dates(configured_date):
    status = billing_status({"BILLING_REVIEWED_ON": configured_date})
    if configured_date == "9999-12-31":
        assert status == {
            "state": "invalid",
            "reviewed_on": configured_date,
            "valid_through": configured_date,
        }
    else:
        assert status == {"state": "unreviewed", "reviewed_on": None, "valid_through": None}


def test_model_tariffs_follow_the_selected_supported_models(live_env):
    default = RateCard.load(live_env)
    assert default.transcription_model == "gpt-transcribe"
    assert default.summary_model == "gpt-6-luna"
    alternate = RateCard.load(
        live_env
        | {"TRANSCRIPTION_MODEL": "gpt-4o-mini-transcribe", "SUMMARY_MODEL": "gpt-4.1-mini"}
    )
    assert alternate.summary_model == "gpt-4.1-mini"
    assert alternate.transcription_model == "gpt-4o-mini-transcribe"
    assert alternate.summary_reservation() > default.summary_reservation()


@pytest.mark.parametrize(
    "config,today",
    [
        ({"SUMMARY_MODEL": "gpt-4.1-mini"}, date(2027, 1, 14)),
        ({"TRANSCRIPTION_MODEL": "gpt-4o-mini-transcribe"}, date(2027, 2, 26)),
    ],
)
def test_removed_provider_model_is_rejected_even_after_price_review(live_env, config, today):
    env = live_env | config | {"BILLING_REVIEWED_ON": str(today)}
    with pytest.raises(ConfigError, match="retired"):
        RateCard.load(env, today=today)


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
