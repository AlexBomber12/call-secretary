"""User journeys across real controllers, SQLite, billing, summaries and delivery.

All callers, model outputs, audio and Telegram transports are synthetic. These
tests prove application behavior, not recognition, translation or phone service.
"""

import asyncio
import base64
import json
import shutil
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from call_secretary.budget import Ledger
from call_secretary.calls import CallController
from call_secretary.config import IngressRoute
from call_secretary.routing import Incoming, Router
from call_secretary.storage import StorageError, Store
from call_secretary.summaries import FIELDS, LABELS, SummaryWorker
from call_secretary.telegram import (
    BotPoller,
    CapturedTelegram,
    DeliveryWorker,
    Enrollment,
    TelegramClient,
    TelegramError,
)
from call_secretary.voice import Realtime

USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "input_token_details": {"audio_tokens": 2},
    "output_token_details": {"audio_tokens": 3},
}
WORDS = {
    "it": "Sono il corriere di prova. Può richiamarmi domani?",
    "ru": "Это тестовый курьер. Попросите перезвонить завтра.",
    "en": "This is the test courier. Please call me back tomorrow.",
}


class SilentAudio:
    latency_ms = 0

    async def interrupt(self):
        pass


class RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


async def complete_response(voice, rid, tool=None):
    await voice.handle({"type": "response.created", "response": {"id": rid}})
    output = []
    if tool:
        name, arguments = tool
        output.append(
            {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(arguments),
                "call_id": "tool-" + rid,
            }
        )
    await voice.handle(
        {
            "type": "response.done",
            "response": {
                "id": rid,
                "status": "completed",
                "usage": USAGE,
                "output": output,
            },
        }
    )


async def caller_words(voice, iid, text):
    await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": iid})
    await voice.handle({"type": "input_audio_buffer.speech_stopped", "item_id": iid})
    await voice.handle({"type": "input_audio_buffer.committed", "item_id": iid})
    await voice.handle(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": iid,
            "transcript": text,
        }
    )


@pytest.mark.parametrize("caller_language", ["it", "ru", "en"])
@pytest.mark.parametrize("report_language", ["ru", "it", "en"])
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_three_family_calls_reach_only_their_private_chat_across_restarts(
    tmp_path, family, rates, caller_language, report_language, automatic, hidden
):
    """US-F01/F02/R01/T01: 36 combinations, each traversing all three profiles."""

    async def scenario():
        numbers = [f"+3900000010{n}" for n in range(3)]
        profiles = tuple(replace(p, report_language=report_language) for p in family.profiles)
        f = replace(
            family,
            profiles=profiles,
            routes=tuple(
                IngressRoute("shared", p.id, number, True)
                for p, number in zip(profiles, numbers, strict=True)
            ),
        )
        cids, updates, deliveries = [], [], []
        with Store(tmp_path / "state", mode="live") as store:
            enrollment = Enrollment(store, f)
            for n, p in enumerate(profiles, start=101):
                token = enrollment.invite(p.id)
                updates.append(
                    {
                        "update_id": n,
                        "message": {
                            "chat": {"type": "private", "id": n},
                            "from": {"id": n},
                            "text": "/start " + token,
                        },
                    }
                )

        def telegram_response(request):
            method = request.url.path.rsplit("/", 1)[-1]
            payload = json.loads(request.content)
            if method == "getUpdates":
                result = updates if payload["offset"] == 0 else []
            else:
                assert method == "sendMessage"
                deliveries.append(payload)
                result = {"message_id": len(deliveries)}
            return httpx.Response(200, json={"ok": True, "result": result})

        api = TelegramClient("synthetic-token", transport=httpx.MockTransport(telegram_response))
        try:
            with Store(tmp_path / "state", mode="live") as store:
                await BotPoller(store, f, api).poll_once()
                assert len(deliveries) == 3  # Enrollment acknowledgements.
                deliveries.clear()
                for n, p in enumerate(profiles):
                    incoming = Incoming(
                        "shared",
                        None if hidden else "+39000000999",
                        numbers[n] if automatic else None,
                    )
                    call = CallController(store, f, incoming, rates=rates, paid=True)
                    socket = RecordingSocket()
                    router = Router(f)
                    voice = Realtime(
                        socket,
                        SilentAudio(),
                        call,
                        rates,
                        router.instructions(call.profile),
                        router.tools(call.profile),
                        "marin",
                    )
                    await voice.handle({"type": "session.updated"})
                    greeting = p.greeting if automatic else f.unresolved_greeting
                    assert greeting in socket.sent[-1]["response"]["instructions"]
                    await complete_response(voice, "greeting")
                    if not automatic:
                        await caller_words(voice, "recipient", p.owner_name)
                        await complete_response(
                            voice, "selection", ("select_recipient", {"profile_id": p.id})
                        )
                        assert call.profile == p
                        assert all(
                            other.owner_name not in voice.prompt for other in profiles if other != p
                        )
                        await complete_response(voice, "question")
                    await caller_words(voice, "message", WORDS[caller_language])
                    await complete_response(voice, "farewell", ("finish_call", {}))
                    await asyncio.wait_for(voice.finish_event.wait(), timeout=1)
                    # A later tool cannot redirect a fixed recipient.
                    assert (
                        await call.tool(
                            "select_recipient", {"profile_id": profiles[(n + 1) % 3].id}
                        )
                    ).instructions is None
                    call.finish(usage_complete=voice.usage_complete)
                    cids.append(call.cid)
                assert store.counts()["summary_pending"] == 3
            # Real persisted state is reopened between each stage of the journey.
            with Store(tmp_path / "state", mode="live") as store:
                provider = GeneratedSummary(
                    purpose={
                        "ru": "Тестовый курьер просит перезвонить завтра.",
                        "it": "Il corriere di prova chiede di essere richiamato domani.",
                        "en": "The test courier asks for a callback tomorrow.",
                    }[report_language]
                )
                for _ in profiles:
                    assert await SummaryWorker(
                        store, f, provider=provider, rates=rates
                    ).process_one()
                assert not await SummaryWorker(store, f).process_one()
                assert len(provider.requests) == 3
                for payload, language in provider.requests:
                    assert language == report_language
                    assert WORDS[caller_language] in [r["text"] for r in payload["utterances"]]
            with Store(tmp_path / "state", mode="live") as store:
                for _ in profiles:
                    assert await DeliveryWorker(store, f, api).process_one()
                assert not await DeliveryWorker(store, f, api).process_one()
                for n, (cid, p, message) in enumerate(
                    zip(cids, profiles, deliveries, strict=True), start=101
                ):
                    assert message["chat_id"] == str(n) and cid in message["text"]
                    assert p.owner_name in message["text"]
                    assert all(other not in message["text"] for other in cids if other != cid)
                    assert store.call(cid)["profile_id"] == p.id
                    assert store.call(cid)["caller_number"] == (None if hidden else "+39000000999")
                    assert (
                        store.db.execute(
                            "SELECT closed FROM budget_reservations WHERE call_id=?", (cid,)
                        ).fetchone()[0]
                        == 1
                    )
                assert store.counts() == {"calls": 3, "summary_pending": 0, "delivery_pending": 0}
                assert Decimal(
                    Ledger(store, f.monthly_budget_eur).status()["committed_eur"]
                ) < Decimal("1")
        finally:
            await api.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "revoke_at", ["active", "before_summary", "during_summary", "after_summary"]
)
def test_rebinding_never_sends_old_call_to_new_person(tmp_path, family, rates, revoke_at):
    """US-T08: revocation applies across all call/summary lifecycle stages."""

    async def scenario():
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, family)
            assert enrollment.claim(enrollment.invite("member_a"), "101")
            call = CallController(store, family, Incoming("shared"), operator_profile="member_a")
            await call.text("caller", "caller", "Old private message", False, 1)

            def rebind():
                enrollment.unbind("member_a")
                assert enrollment.claim(enrollment.invite("member_a"), "202")

            if revoke_at == "active":
                rebind()
            call.finish()
            if revoke_at == "before_summary":
                rebind()
            provider = GeneratedSummary(action=rebind if revoke_at == "during_summary" else None)
            await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            if revoke_at == "after_summary":
                rebind()
        with Store(tmp_path / "state") as store:
            Enrollment(store, family).import_explicit_destinations()
            api = CapturedTelegram()
            assert not await DeliveryWorker(store, family, api).process_one()
            assert api.receipts == []
            assert store.db.execute("SELECT state FROM outbox").fetchone()[0] == "blocked"
            # A new call after reenrollment goes to the new intended chat.
            new = CallController(store, family, Incoming("shared"), operator_profile="member_a")
            await new.text("new", "caller", "New private message", False, 2)
            new.finish()
            await SummaryWorker(store, family).process_one()
            await DeliveryWorker(store, family, api).process_one()
            assert len(api.receipts) == 1 and api.receipts[0]["chat_id"] == "202"
            assert "Old private message" not in api.receipts[0]["text"]

    asyncio.run(scenario())


def test_crash_unknown_bill_api_failure_telegram_retry_and_backup_restore(tmp_path, family, rates):
    """US-O02/O05: partial text and unknown costs survive a chain of failures."""

    class FailedProvider:
        async def generate(self, payload, language):
            raise TimeoutError("synthetic provider timeout")

    class RetryTelegram:
        async def request(self, method, payload):
            raise TelegramError(429, 60)

    async def scenario():
        with Store(tmp_path / "state", mode="live") as store:
            enrollment = Enrollment(store, family)
            assert enrollment.claim(enrollment.invite("member_b"), "102")
            call = CallController(
                store,
                family,
                Incoming("shared"),
                rates=rates,
                paid=True,
                operator_profile="member_b",
            )
            await call.text("partial", "caller", "Synthetic unfinished request", False, 1)
            cid = call.cid
            held = call.ledger.status()["committed_eur"]
            with pytest.raises(StorageError, match="Another call"):
                CallController(store, family, Incoming("other"))
            # Process dies before controller.finish().
        with Store(tmp_path / "state", mode="live") as store:
            assert store.recover_calls() == 1 and store.recover_calls() == 0
            await SummaryWorker(store, family, provider=FailedProvider(), rates=rates).process_one()
            await DeliveryWorker(store, family, RetryTelegram()).process_one(now=10)
            assert store.call(cid)["status"] == "interrupted"
            assert Ledger(store, family.monthly_budget_eur).status()["committed_eur"] == held
            store.backup(tmp_path / "backup.sqlite3")
        restored = tmp_path / "restored"
        restored.mkdir(mode=0o700)
        shutil.copyfile(tmp_path / "backup.sqlite3", restored / "secretary.sqlite3")
        # copyfile obeys umask, so restore the required private permissions explicitly.
        (restored / "secretary.sqlite3").chmod(0o600)
        with Store(restored, mode="live") as store:
            api = CapturedTelegram()
            assert not await DeliveryWorker(store, family, api).process_one(now=69)
            assert await DeliveryWorker(store, family, api).process_one(now=71)
            assert api.receipts[0]["chat_id"] == "102"
            assert "Synthetic unfinished request" in api.receipts[0]["text"]
            assert cid in api.receipts[0]["text"]
            assert store.transcript(cid)[0]["text"] == "Synthetic unfinished request"
            assert store.db.execute("SELECT closed FROM budget_reservations").fetchone()[0] == 0
            Ledger(store, family.monthly_budget_eur).reconcile(cid, Decimal("0.25"))
            assert Ledger(store, family.monthly_budget_eur).status()["committed_eur"] == "0.25"

    asyncio.run(scenario())


def test_unresolved_recipient_never_broadcasts_or_spends_on_summary(tmp_path, family, rates):
    """US-F05: three failed clarifications retain a private, unassigned record."""

    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("shared", "+39000000999"))
            for n in range(3):
                reply = await call.tool("select_recipient", {"profile_id": "not-a-family-member"})
                assert reply.finish == (n == 2)
            await call.text("caller", "caller", "Private unassigned request", False, 1)
            call.finish()
            provider = GeneratedSummary()
            await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            api = CapturedTelegram()
            assert not await DeliveryWorker(store, family, api).process_one()
            assert provider.requests == [] and api.receipts == []
            assert store.call(call.cid)["profile_id"] is None
            assert store.call(call.cid)["status"] == "unassigned"
            assert report_for(store, call.cid)["caller_name"] is None
            assert store.db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0

    asyncio.run(scenario())


def test_reissued_invitation_invalidates_old_link_without_consuming_new_one(tmp_path, family):
    """US-T06: replacing a lost invitation does not leave two valid links."""
    with Store(tmp_path / "state") as store:
        enrollment = Enrollment(store, family)
        old = enrollment.invite("member_a", now=10)
        new = enrollment.invite("member_a", now=11)
        assert old != new
        assert not enrollment.claim(old, "101", now=12)
        assert enrollment.claim(new, "101", now=13)
        assert not enrollment.claim(new, "102", now=14)


def test_ambiguous_telegram_success_retries_same_report_not_a_new_destination(tmp_path, family):
    """US-O04: a lost receipt can duplicate a send; call ID/destination stay fixed."""

    class LostReceipt:
        def __init__(self):
            self.receipts = []

        async def request(self, method, payload):
            self.receipts.append(dict(payload))  # Telegram may already have accepted it.
            raise TelegramError(0)

    async def scenario():
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, family)
            enrollment.claim(enrollment.invite("member_a"), "101")
            call = CallController(store, family, Incoming("shared"), operator_profile="member_a")
            await call.text("caller", "caller", "Synthetic message", False, 1)
            call.finish()
            await SummaryWorker(store, family).process_one()
            lost = LostReceipt()
            await DeliveryWorker(store, family, lost).process_one(now=10)
        with Store(tmp_path / "state") as store:
            api = CapturedTelegram()
            await DeliveryWorker(store, family, api).process_one(now=20)
            assert api.receipts == lost.receipts
            assert call.cid in api.receipts[0]["text"]
            assert store.counts()["delivery_pending"] == 0

    asyncio.run(scenario())


def test_barge_in_persists_only_interrupted_text_not_audio_or_transport_data(
    tmp_path, family, rates
):
    """US-O07/V09: text survives an interrupted reply; raw audio stays ephemeral."""

    async def scenario():
        pcm = b"synthetic-audio-not-for-storage!" * 32
        encoded = base64.b64encode(pcm).decode("ascii")
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("shared"), operator_profile="member_a")
            voice = Realtime(
                RecordingSocket(), SilentAudio(), call, rates, "synthetic", [], "marin"
            )
            await voice.handle({"type": "response.created", "response": {"id": "reply"}})
            await voice.handle(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "reply",
                    "item_id": "assistant",
                    "delta": encoded,
                }
            )
            await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "caller"})
            await voice.handle(
                {
                    "type": "response.output_audio_transcript.done",
                    "item_id": "assistant",
                    "transcript": "Interrupted synthetic reply",
                }
            )
            call.finish()
            cid = call.cid
        with Store(tmp_path / "state") as store:
            assert store.transcript(cid) == [
                {
                    "item_id": "assistant",
                    "role": "assistant",
                    "text": "Interrupted synthetic reply",
                    "interrupted": 1,
                }
            ]
        for path in (tmp_path / "state").iterdir():
            data = path.read_bytes()
            assert pcm not in data and encoded.encode("ascii") not in data
        assert {p.suffix for p in (tmp_path / "state").iterdir()} == {".sqlite3"}

    asyncio.run(scenario())


class GeneratedSummary:
    def __init__(self, *, purpose="Synthetic message", uncertainties=(), action=None):
        self.requests = []
        self.purpose = purpose
        self.uncertainties = uncertainties
        self.action = action

    async def generate(self, payload, language):
        self.requests.append((payload, language))
        if self.action:
            self.action()
        result = dict.fromkeys(FIELDS) | {
            "purpose": self.purpose,
            "urgency": "unknown",
            "uncertainties": list(self.uncertainties),
        }
        return {
            "id": "synthetic-summary",
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "text": json.dumps(result),
        }


def report_for(store, cid):
    return json.loads(
        store.db.execute("SELECT content FROM summaries WHERE call_id=?", (cid,)).fetchone()[0]
    )


@pytest.mark.parametrize("language", ["ru", "it", "en"])
@pytest.mark.parametrize("cached", [False, True])
def test_long_generated_report_keeps_limit_warning_route_and_status_after_restart(
    tmp_path, family, rates, language, cached
):
    """US-R07/R08: truncation cannot look like a complete, certain message."""

    async def scenario():
        f = replace(
            family, profiles=tuple(replace(p, report_language=language) for p in family.profiles)
        )
        with Store(tmp_path / "state") as store:
            call = CallController(store, f, Incoming("shared"))
            await call.tool("select_recipient", {"profile_id": "member_a"})
            for n in range(5):
                await call.text(str(n), "caller", "A" * 16000, False, n)
            call.finish("duration_limit")
            provider = GeneratedSummary(purpose="P" * 1000)
            # Return a valid, very verbose model result, deliberately omitting warnings.
            original_generate = provider.generate

            async def verbose(payload, language):
                response = await original_generate(payload, language)
                value = json.loads(response["text"])
                value.update({k: "X" * 1000 for k in FIELDS})
                response["text"] = json.dumps(value)
                return response

            provider.generate = verbose
            if cached:
                response = await provider.generate({}, language)
                store.db.execute(
                    "INSERT INTO metadata VALUES(?,?)",
                    ("summary_checkpoint:" + call.cid, json.dumps(response)),
                )
                Ledger(store, f.monthly_budget_eur).reserve(call.cid, Decimal("1"))
            assert await SummaryWorker(store, f, provider=provider, rates=rates).process_one()
            cid = call.cid
        with Store(tmp_path / "state") as store:
            result = report_for(store, cid)
            assert "transcript_truncated" in result["uncertainties"]
            warning = {"ru": "часть текста", "it": "parte del testo", "en": "part of the text"}
            route = {
                "ru": "со слов звонящего",
                "it": "indicato dal chiamante",
                "en": "selected by caller",
            }
            status = {"ru": "лимит времени", "it": "limite di durata", "en": "duration limit"}
            assert warning[language] in result["report"]
            assert route[language] in result["report"]
            assert status[language] in result["report"]
            assert all(label + ":" in result["report"] for label in LABELS[language][3:10])
            assert "…" in result["report"]
            assert cid in result["report"] and len(result["report"]) <= 4000
            assert sum(len(r["text"]) for r in store.transcript(cid)) == 80000

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["ru", "it", "en"])
def test_long_offline_report_explicitly_says_only_part_of_caller_text_is_shown(
    tmp_path, family, language
):
    """US-R09: a shortened original quote is not presented as the full message."""

    async def scenario():
        f = replace(family, profiles=(replace(family.profiles[0], report_language=language),))
        with Store(tmp_path / "state") as store:
            call = CallController(store, f, Incoming("shared"), operator_profile="member_a")
            await call.text("caller", "caller", "M" * 1001, False, 1)
            call.finish()
            await SummaryWorker(store, f).process_one()
            result = report_for(store, call.cid)
            assert "transcript_truncated" in result["uncertainties"]
            warning = {"ru": "часть текста", "it": "parte del testo", "en": "part of the text"}
            assert warning[language] in result["report"]
            assert store.transcript(call.cid)[0]["text"] == "M" * 1001

    asyncio.run(scenario())


def test_summary_cannot_start_paid_work_when_voice_usage_leaves_insufficient_budget(
    tmp_path, family, rates
):
    """US-B06: an unexpectedly expensive call cannot bypass the family ceiling."""

    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(
                store,
                family,
                Incoming("shared"),
                rates=rates,
                paid=True,
                operator_profile="member_a",
            )
            await call.text("caller", "caller", "Synthetic callback request", False, 1)
            # A provider overrun is recorded, even above the original conservative hold.
            assert not call.ledger.consume(call.cid, "unexpected-voice-bill", Decimal("49.99"))
            call.finish("provider_or_audio_error")
            provider = GeneratedSummary()
            await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            assert provider.requests == []
            assert (
                "summary_unavailable_original_text" in report_for(store, call.cid)["uncertainties"]
            )
            assert Decimal(call.ledger.status()["committed_eur"]) == Decimal("49.99")

    asyncio.run(scenario())
