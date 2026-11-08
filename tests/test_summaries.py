import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_secretary.budget import Ledger, RateCard
from call_secretary.calls import simulate
from call_secretary.config import load_family
from call_secretary.storage import Store
from call_secretary.summaries import SummaryError, SummaryWorker, fallback, render, validate_summary


def test_schema_rejects_invented_fields_and_invalid_urgency():
    summary = fallback([])
    with pytest.raises(SummaryError):
        validate_summary(summary | {"telegram_chat_id": "999"})
    with pytest.raises(SummaryError):
        validate_summary(summary | {"urgency": "definitely urgent"})


def test_offline_fallback_labels_original_language_and_never_guesses_name():
    summary = fallback([{"role": "caller", "text": "Richiamami domani."}])
    assert summary["caller_name"] is None
    assert summary["urgency"] == "unknown"
    assert summary["purpose"] == "Richiamami domani."
    assert "summary_unavailable_original_text" in summary["uncertainties"]


def test_report_always_preserves_call_id_when_fields_are_very_verbose():
    family = load_family(Path("config/family.example.yaml"))
    summary = fallback([])
    for field in ["caller_name", "organization", "purpose", "callback", "next_step"]:
        summary[field] = "a" * 1000
    report = render(
        {
            "id": "stable-id",
            "started": 1,
            "caller_number": None,
            "route_source": "caller_selected",
            "status": "ended",
        },
        family.profiles[0],
        summary,
        family.timezone,
    )
    assert len(report) < 4096
    assert report.endswith("ID: stable-id")


def test_summary_waits_for_recipient_destination_instead_of_losing_delivery(tmp_path):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_b", "Please call tomorrow.")
            assert await SummaryWorker(store, family).process_one()
            row = store.db.execute("SELECT * FROM outbox").fetchone()
            assert row["call_id"] == cid and row["profile_id"] == "member_b"
            assert row["state"] == "waiting_destination"
            assert store.claim_job("outbox") is None
            saved = json.loads(store.db.execute("SELECT content FROM summaries").fetchone()[0])
            assert "без перевода" in saved["report"]
            assert not await SummaryWorker(store, family).process_one()

    asyncio.run(scenario())


def test_unassigned_summary_never_creates_a_family_delivery(tmp_path):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            await simulate(store, family, None, "Unassigned synthetic message")
            await SummaryWorker(store, family).process_one()
            assert store.counts()["delivery_pending"] == 0

    asyncio.run(scenario())


def current_rates():
    return RateCard.load(
        {
            "REALTIME_MODEL": "gpt-realtime-2.1",
            "BILLING_REVIEWED_ON": datetime.now(UTC).date().isoformat(),
            "USD_TO_EUR_UPPER_BOUND": "1.5",
        }
    )


class Provider:
    def __init__(self, *, invalid=False, timeout=False):
        self.calls = 0
        self.invalid, self.timeout = invalid, timeout

    async def generate(self, payload, language):
        self.calls += 1
        if self.timeout:
            raise TimeoutError("synthetic timeout")
        summary = fallback([])
        summary.update(purpose="Просит перезвонить завтра.", uncertainties=[])
        return {
            "id": "response-synthetic",
            "usage": {"input_tokens": 200, "output_tokens": 50},
            "text": "invalid JSON" if self.invalid else json.dumps(summary),
        }


@pytest.mark.parametrize("invalid", [False, True])
def test_structured_or_invalid_generated_summary_finishes_without_regeneration(tmp_path, invalid):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            await simulate(store, family, "member_a", "Call tomorrow")
            provider = Provider(invalid=invalid)
            worker = SummaryWorker(store, family, provider=provider, rates=current_rates())
            assert await worker.process_one()
            assert not await worker.process_one()
            assert provider.calls == 1
            assert store.counts()["summary_pending"] == 0
            assert store.counts()["delivery_pending"] == 1

    asyncio.run(scenario())


def test_unknown_paid_attempt_keeps_reservation_and_is_not_retried_after_restart(tmp_path):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            await simulate(store, family, "member_a", "Synthetic call")
            provider = Provider(timeout=True)
            await SummaryWorker(
                store, family, provider=provider, rates=current_rates()
            ).process_one()
            assert provider.calls == 1
            budget = Ledger(store, family.monthly_budget_eur).status()
            assert float(budget["committed_eur"]) > 0
        with Store(tmp_path / "state") as store:
            assert not await SummaryWorker(
                store, family, provider=provider, rates=current_rates()
            ).process_one()
            assert provider.calls == 1

    asyncio.run(scenario())


def test_cached_generation_is_reused_after_crash_before_delivery(tmp_path):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_b", "Synthetic call")
            rates = current_rates()
            Ledger(store, family.monthly_budget_eur).reserve(cid, rates.summary_reservation())
            response = await Provider().generate({}, "ru")
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO metadata VALUES(?,?)", ("summary_attempt:" + cid, "started")
                )
                db.execute(
                    "INSERT INTO metadata VALUES(?,?)",
                    ("summary_checkpoint:" + cid, json.dumps(response)),
                )
        with Store(tmp_path / "state") as store:
            provider = Provider()
            await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.calls == 0
            assert store.counts()["summary_pending"] == 0

    asyncio.run(scenario())
