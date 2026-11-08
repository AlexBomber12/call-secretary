import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from test_summaries import Provider

from call_secretary.calls import simulate
from call_secretary.storage import Store
from call_secretary.summaries import (
    OpenAISummary,
    SummaryError,
    SummaryWorker,
    destination,
    fallback,
    render,
    validate_summary,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": 1},
        {"purpose": "x" * 1001},
        {"uncertainties": None},
        {"uncertainties": ["x"] * 9},
        {"uncertainties": [1]},
        {"uncertainties": ["x" * 201]},
    ],
)
def test_summary_schema_rejects_unsafe_field_values(changes):
    with pytest.raises(SummaryError):
        validate_summary(fallback([]) | changes)


def test_report_preserves_uncertainty_instead_of_claiming_a_fact(family):
    summary = validate_summary(
        fallback([]) | {"purpose": "  call  tomorrow ", "uncertainties": ["?"]}
    )
    text = render(
        {
            "id": "stable",
            "started": 1,
            "caller_number": None,
            "route_source": None,
            "status": "ended",
        },
        family.profiles[0],
        summary,
        family.timezone,
    )
    assert "? ?" in text and "call tomorrow" in text


@pytest.mark.parametrize("status", ["completed", "incomplete"])
def test_summary_http_contract_is_bounded_private_and_structured(monkeypatch, status):
    original = httpx.AsyncClient

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        payload = json.loads(request.content)
        assert payload["store"] is False and payload["max_output_tokens"] == 768
        assert payload["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "s",
                "status": status,
                "usage": {},
                "output": [
                    {"type": "reasoning"},
                    {
                        "type": "message",
                        "content": [{"type": "refusal"}, {"type": "output_text", "text": "{}"}],
                    },
                ],
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = OpenAISummary("synthetic-key")
    assert "synthetic-key" not in repr(provider)
    result = asyncio.run(provider.generate({"utterances": []}, "ru"))
    assert result["text"] == ("{}" if status == "completed" else "")


@pytest.mark.parametrize("body", [[], "oversized"])
def test_summary_http_response_shape_and_size_are_checked(monkeypatch, body):
    original = httpx.AsyncClient
    response = (
        httpx.Response(200, content=b"x" * 131073)
        if body == "oversized"
        else httpx.Response(200, json=body)
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: response), **kwargs
        ),
    )
    with pytest.raises(SummaryError):
        asyncio.run(OpenAISummary("synthetic-key").generate({}, "ru"))


def test_disabled_binding_or_profile_never_becomes_a_destination(tmp_path, family):
    with Store(tmp_path / "state") as store:
        assert destination(store, replace(family.profiles[0], enabled=False)) is None
        store.db.execute("INSERT INTO bindings VALUES('member_a','101',0)")
        assert destination(store, family.profiles[0]) is None


@pytest.mark.parametrize("outcome", ["oversized", "stale", "invalid_usage", "overrun"])
def test_summary_failure_keeps_work_and_billing_consistent(tmp_path, family, rates, outcome):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "synthetic")
            for i in range(4):
                store.utterance(cid, str(i), "assistant" if i == 0 else "caller", "x" * 16000)

            class Generate(Provider):
                async def generate(self, payload, language):
                    assert payload["transcript_truncated"] is True
                    assert len(payload["caller_text"]) == 10000
                    response = await super().generate(payload, language)
                    if outcome == "oversized":
                        return response | {"text": "x" * 24001}
                    if outcome == "stale":
                        store.db.execute("UPDATE summary_jobs SET lease_token='other'")
                    if outcome == "invalid_usage":
                        response["usage"] = {"input_tokens": True, "output_tokens": -1}
                    if outcome == "overrun":
                        response["usage"] = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
                    return response

            provider = Generate()
            assert await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.calls == 1
            if outcome == "stale":
                assert store.counts()["summary_pending"] == 1
            else:
                assert store.counts()["summary_pending"] == 0
                saved = json.loads(store.db.execute("SELECT content FROM summaries").fetchone()[0])
                assert "summary_unavailable_original_text" in saved["uncertainties"]

    asyncio.run(scenario())


def test_attempt_marker_survives_crash_and_blocks_duplicate_paid_generation(
    tmp_path, family, rates
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "synthetic")
            store.db.execute(
                "INSERT INTO metadata VALUES(?,?)", ("summary_attempt:" + cid, "started")
            )
            provider = Provider()
            assert await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.calls == 0

    asyncio.run(scenario())


def test_corrupt_checkpoint_is_retried_without_losing_the_job(tmp_path, family, rates):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "synthetic")
            store.db.execute("INSERT INTO metadata VALUES(?,?)", ("summary_checkpoint:" + cid, "{"))
            assert await SummaryWorker(
                store, family, provider=Provider(), rates=rates
            ).process_one()
            row = store.db.execute("SELECT * FROM summary_jobs").fetchone()
            assert row["state"] == "pending" and row["not_before"] > 0

    asyncio.run(scenario())


def test_no_caller_words_means_no_paid_summary(tmp_path, family, rates):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "")
            store.utterance(cid, "a", "assistant", "synthetic greeting")
            provider = Provider()
            assert await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.calls == 0

    asyncio.run(scenario())
