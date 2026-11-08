import asyncio
import json
import sys
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_audio_edges import Stream
from test_summaries import Provider
from test_telegram import API
from test_voice import Endpoint, Hooks, Socket

from call_secretary.audio import AudioError, Microphone
from call_secretary.budget import BudgetError, Ledger
from call_secretary.calls import simulate
from call_secretary.modem import ModemError, at_query
from call_secretary.storage import Store
from call_secretary.summaries import OpenAISummary, SummaryError, SummaryWorker
from call_secretary.telegram import DeliveryWorker, Enrollment, TelegramClient, TelegramError
from call_secretary.voice import Realtime, ToolReply


@pytest.mark.parametrize("tool", ["select_recipient", "finish_call", None])
def test_completed_but_interrupted_response_cannot_execute_stale_tools(rates, tool):
    class Track(Hooks):
        def __init__(self):
            super().__init__()
            self.executed = []

        async def tool(self, name, args):
            self.executed.append(name)
            return ToolReply("old decision", finish=name == "finish_call")

    async def scenario():
        hooks = Track()
        voice = Realtime(Socket(), Endpoint(), hooks, rates, "prompt", [], "marin")
        await voice.request_response()
        await voice.handle({"type": "response.created", "response": {"id": "old"}})
        await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "new"})
        await voice.handle({"type": "input_audio_buffer.committed", "item_id": "new"})
        await voice.handle(
            {
                "type": "response.done",
                "response": {
                    "id": "old",
                    "status": "completed",
                    "usage": {},
                    "output": [{"type": "function_call", "name": tool, "call_id": "stale"}]
                    if tool
                    else [{"type": "message"}],
                },
            }
        )
        assert not hooks.executed
        assert len(hooks.usages) == 1
        assert voice.responses == 2
        assert voice._finish_task is None
        replies = [e for e in voice.socket.sent if e["type"] == "conversation.item.create"]
        if tool:
            assert len(replies) == 1
            assert (
                json.loads(replies[0]["item"]["output"])["error"] == "interrupted_response_ignored"
            )
        else:
            assert not replies

    asyncio.run(scenario())


@pytest.mark.parametrize("revoke", [False, True])
def test_temporarily_disabled_delivery_resumes_but_revoked_delivery_does_not(
    tmp_path, family, revoke
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, family)
            enrollment.claim(enrollment.invite("member_a"), "101")
            cid = await simulate(store, family, "member_a", "private synthetic message")
            await SummaryWorker(store, family).process_one()
            disabled = replace(
                family, profiles=(replace(family.profiles[0], enabled=False), *family.profiles[1:])
            )
            Enrollment(store, disabled).import_explicit_destinations()
            api = API()
            assert await DeliveryWorker(store, disabled, api).process_one()
            assert api.sent == []
            if revoke:
                enrollment.unbind("member_a")
                enrollment.claim(enrollment.invite("member_a"), "102")
            enrollment.import_explicit_destinations()
            await DeliveryWorker(store, family, api).process_one()
            if revoke:
                assert api.sent == []
            else:
                assert len(api.sent) == 1 and cid in api.sent[0]["text"]
                assert api.sent[0]["chat_id"] == "101"

    asyncio.run(scenario())


def test_removed_profile_summary_is_held_without_an_infinite_retry(tmp_path, family):
    async def scenario():
        with Store(tmp_path / "state") as store:
            await simulate(store, family, "member_a", "orphaned private message")
            current = replace(family, profiles=family.profiles[1:])
            worker = SummaryWorker(store, current)
            assert await worker.process_one()
            assert store.db.execute("SELECT state FROM summary_jobs").fetchone()[0] == "blocked"
            assert not await worker.process_one()
            assert store.counts()["delivery_pending"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["stale-rates", "budget-refusal"])
def test_summary_preflight_failure_still_produces_an_honest_report(
    tmp_path, family, rates, failure
):
    async def scenario():
        actual_family = (
            replace(family, monthly_budget_eur=Decimal("0.000001"))
            if failure == "budget-refusal"
            else family
        )
        actual_rates = replace(rates, reviewed_on=None) if failure == "stale-rates" else rates
        with Store(tmp_path / "state") as store:
            await simulate(store, actual_family, "member_a", "original caller words")
            provider = Provider()
            worker = SummaryWorker(store, actual_family, provider=provider, rates=actual_rates)
            assert await worker.process_one()
            assert provider.calls == 0
            assert store.counts()["summary_pending"] == 0
            saved = json.loads(store.db.execute("SELECT content FROM summaries").fetchone()[0])
            assert saved["purpose"] == "original caller words"
            assert "summary_unavailable_original_text" in saved["uncertainties"]
            assert store.counts()["delivery_pending"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_action", ["input-abort", "input-close", "output-abort"])
def test_audio_cleanup_attempts_both_streams_after_a_backend_error(monkeypatch, failed_action):
    actions = []

    class Device(Stream):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def act(self, action):
            key = self.name + "-" + action
            actions.append(key)
            if key == failed_action:
                raise RuntimeError("private backend detail")

        def abort(self):
            self.act("abort")

        def close(self):
            self.act("close")

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            RawInputStream=lambda **kw: Device("input"),
            RawOutputStream=lambda **kw: Device("output"),
        ),
    )

    async def scenario():
        with pytest.raises(AudioError):
            await Microphone().close()
        assert set(actions) == {"input-abort", "input-close", "output-abort", "output-close"}

    asyncio.run(scenario())


def test_read_only_probe_reassembles_partial_serial_lines():
    class Port:
        def __init__(self):
            self.chunks = iter([b"AT+CG", b"MM\r\nSIM", b"7600E-H\r\nO", b"K\r\n"])
            self.sizes = []

        def write(self, data):
            return len(data)

        def readline(self):
            return next(self.chunks, b"")

        def read(self, size):
            self.sizes.append(size)
            return next(self.chunks, b"")

    port = Port()
    ticks = iter(i / 100 for i in range(1000))
    assert at_query(port, "AT+CGMM", clock=lambda: next(ticks)) == ["SIM7600E-H"]
    assert port.sizes and max(port.sizes) <= 1024


def test_short_diagnostic_write_does_not_read_an_unrelated_reply():
    port = SimpleNamespace(write=lambda data: 0, read=lambda size: pytest.fail("must not read"))
    with pytest.raises(ModemError, match="incomplete"):
        at_query(port, "AT")


@pytest.mark.parametrize("stage", ["active", "summary-pending", "summary-leased"])
def test_reconciliation_cannot_release_budget_while_paid_work_can_still_run(
    tmp_path, family, stage
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = store.create_call("synthetic")
            ledger = Ledger(store, family.monthly_budget_eur)
            ledger.reserve(cid, Decimal(1))
            if stage != "active":
                store.end_call(cid)
                if stage == "summary-leased":
                    store.claim_job("summary_jobs")
            with pytest.raises(BudgetError, match="finish"):
                ledger.reconcile(cid, Decimal("0.25"))
            assert ledger.status()["committed_eur"] == "1"
            if stage == "summary-leased":
                store.db.execute("UPDATE summary_jobs SET state='pending',lease_until=0")
            store.end_call(cid)
            await SummaryWorker(store, family).process_one()
            ledger.reconcile(cid, Decimal("0.25"))
            assert ledger.status()["committed_eur"] == "0.25"

    asyncio.run(scenario())


def test_slow_dribbling_http_response_has_an_overall_deadline(monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(0.02))

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in b'{"ok":true,"result":true}':
                await asyncio.sleep(0.01)
                yield bytes([byte])

    async def scenario():
        client = TelegramClient(
            "synthetic-token",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow())),
        )
        try:
            with pytest.raises(TelegramError):
                await asyncio.wait_for(client.request("getMe", {}), 0.2)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_truncated_summary_request_is_bounded_after_json_escaping(tmp_path, family, rates):
    class Measure(Provider):
        async def generate(self, payload, language):
            self.size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return await super().generate(payload, language)

    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "synthetic")
            store.utterance(cid, "escaped", "caller", "\0" * 16000)
            provider = Measure()
            await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.size <= 48000

    asyncio.run(scenario())


class CountingBody(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(100):
            self.reads += 1
            yield b"x" * 8192

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("service", ["telegram", "summary"])
def test_http_body_limit_stops_reading_before_the_entire_response(service, monkeypatch):
    stream = CountingBody()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))

    async def scenario():
        if service == "telegram":
            client = TelegramClient("synthetic-token", transport=transport)
            try:
                with pytest.raises(TelegramError):
                    await client.request("getMe", {})
            finally:
                await client.close()
            maximum = 65
        else:
            original = httpx.AsyncClient
            monkeypatch.setattr(
                httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw)
            )
            with pytest.raises(SummaryError):
                await OpenAISummary("synthetic-key").generate({}, "ru")
            maximum = 17
        assert stream.closed and stream.reads <= maximum

    asyncio.run(scenario())
