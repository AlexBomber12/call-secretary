import asyncio
import json
from dataclasses import replace

import pytest
from test_summaries import Provider

from call_secretary import telegram
from call_secretary.calls import CallController
from call_secretary.routing import REPORT_RECIPIENT_CONFLICT, Incoming
from call_secretary.storage import Store
from call_secretary.summaries import SummaryError, SummaryWorker, fallback, render, validate_summary
from call_secretary.telegram import CapturedTelegram, DeliveryWorker, Enrollment, TelegramError


def test_private_report_is_a_narrative_with_the_confirmed_details_and_two_number_sources(
    tmp_path, family, rates
):
    narrative = (
        "Звонящий просит перенести выдачу заказа на пятницу после обеда. "
        "Я уточнил дату, и он поправил: в четверг не сможет. "
        "Он ждёт подтверждения и просит перезвонить по указанному контакту."
    )

    class Generated(Provider):
        async def generate(self, payload, language):
            assert language == "ru"
            assert len(payload["utterances"]) == 3
            response = await super().generate(payload, language)
            summary = json.loads(response["text"])
            summary.update(narrative=narrative, callback="+39000000127")
            return response | {"text": json.dumps(summary)}

    async def scenario():
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, family)
            enrollment.claim(enrollment.invite("member_a"), "101")
            call = CallController(
                store,
                family,
                Incoming("synthetic", "+39000000123"),
                operator_profile="member_a",
            )
            await call.text("first", "caller", "Por favor, retirar otro día.", False, 1)
            await call.text("ask", "assistant", "¿Qué día?", False, 2)
            await call.text("last", "caller", "Viernes, no jueves. Es +39000000127.", False, 3)
            call.finish()
            assert await SummaryWorker(
                store, family, provider=Generated(), rates=rates
            ).process_one()
            api = CapturedTelegram()
            assert await DeliveryWorker(store, family, api).process_one()
            result = api.receipts[0]
            assert result["chat_id"] == "101" and call.cid in result["text"]
            assert narrative in result["text"]
            assert "Номер сети: +39000000123" in result["text"]
            assert "Контакт со слов звонящего: +39000000127" in result["text"]
            assert "Вопрос:" not in result["text"] and "Ответ:" not in result["text"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,speech,provider_fails,expected",
    [
        ("ended", "", False, "Сообщение не оставлено"),
        ("caller_hangup_text_incomplete", "", False, "Часть речи не распознана"),
        ("provider_or_audio_error", "", False, "Сбой голосовой связи"),
        ("ended", "Synthetic original words", True, "без перевода"),
    ],
)
def test_no_message_partial_audio_and_failed_summary_have_distinct_reports(
    tmp_path,
    family,
    rates,
    status,
    speech,
    provider_fails,
    expected,
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("synthetic"), operator_profile="member_a")
            if speech:
                await call.text("sentence", "caller", speech, False, 1)
            call.finish(status)
            provider = Provider(timeout=provider_fails)
            assert await SummaryWorker(store, family, provider=provider, rates=rates).process_one()
            report = store.db.execute("SELECT text FROM outbox").fetchone()[0]
            assert expected in report
            assert provider.calls == int(provider_fails)
            assert ("без перевода" in report) == provider_fails
            if speech:
                assert speech in report

    asyncio.run(scenario())


@pytest.mark.parametrize("disputed", [False, True])
@pytest.mark.parametrize(
    "operator_chat,chat_type",
    [
        ("9001", "private"),
        ("9002", "private"),
        ("9001", "group"),
    ],
)
def test_operator_alert_is_content_free_and_keeps_its_original_chat(
    tmp_path,
    family,
    disputed,
    operator_chat,
    chat_type,
):
    class OperatorAPI(CapturedTelegram):
        async def request(self, method, payload):
            if method == "getChat":
                return {"id": int(payload["chat_id"]), "type": chat_type}
            return await super().request(method, payload)

    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(
                store,
                family,
                Incoming("synthetic", "+39000000123"),
                operator_profile="member_a" if disputed else None,
            )
            await call.text("a", "caller", "Sensitive synthetic request", False, 1)
            if disputed:
                await call.tool(REPORT_RECIPIENT_CONFLICT, {})
            call.finish()

            assert await SummaryWorker(store, family, operator_chat="9001").process_one()
            row = store.db.execute("SELECT * FROM outbox").fetchone()
            assert row["chat_id"] == "9001" and row["call_id"] == call.cid
            report = row["text"]
            assert call.cid in report
            assert ("оспорен" if disputed else "не определён") in report
            assert "Sensitive" not in report and "+39000000123" not in report

        with Store(tmp_path / "state") as store:
            api = OperatorAPI()
            assert await DeliveryWorker(
                store, family, api, operator_chat=operator_chat
            ).process_one()
            assert api.receipts == (
                [{"chat_id": "9001", "text": report}]
                if operator_chat == "9001" and chat_type == "private"
                else []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "facts,expect",
    [
        ({"callback": "12345"}, "Звонящий оставил контакт"),
        ({"purpose": "Call Friday", "urgency": "urgent"}, "дело срочное"),
        ({"narrative": "📦" * 2000}, "…"),
    ],
)
def test_report_keeps_callback_only_urgent_legacy_and_multibyte_narrative(family, facts, expect):
    report = render(
        {
            "id": "synthetic",
            "started": 1,
            "caller_number": None,
            "route_source": None,
            "status": "ended",
        },
        family.profiles[0],
        fallback([]) | facts,
        family.timezone,
    )
    assert expect in report
    assert "Сообщение не оставлено" not in report
    assert "Номер сети: Неизвестно" in report
    assert len(report.encode("utf-16-le")) <= 4000 * 2
    assert report.endswith("ID: synthetic")


def test_paid_result_with_no_facts_keeps_available_original_words(tmp_path, family, rates):
    class EmptySummary(Provider):
        async def generate(self, payload, language):
            result = await super().generate(payload, language)
            return result | {"text": json.dumps(fallback([]))}

    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("synthetic"), operator_profile="member_a")
            await call.text("speech", "caller", "Synthetic original words", False, 1)
            call.finish()
            assert await SummaryWorker(
                store, family, provider=EmptySummary(), rates=rates
            ).process_one()
            report = store.db.execute("SELECT text FROM outbox").fetchone()[0]
            assert "без перевода" in report and "Synthetic original words" in report
        with pytest.raises(SummaryError):
            validate_summary([])

    asyncio.run(scenario())


def test_permanent_telegram_startup_error_does_not_touch_pending_job(tmp_path, family):
    class AuthError:
        async def request(self, method, payload):
            assert method == "getWebhookInfo"
            raise TelegramError(401)

    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("synthetic"))
            call.finish()
            with pytest.raises(TelegramError, match="401"):
                await telegram.run_workers(store, family, AuthError(), SummaryWorker(store, family))
            assert store.counts()["summary_pending"] == 1
            assert telegram.health.snapshot(store)["telegram"]["state"] == "unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("binding_method", ["invite", "config"])
def test_fixed_destination_waiting_for_reenable_cannot_be_reassigned(
    tmp_path, family, binding_method
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            original = Enrollment(store, family)
            original.claim(original.invite("member_a"), "101")
            call = CallController(store, family, Incoming("synthetic"), operator_profile="member_a")
            await call.text("speech", "caller", "Synthetic message", False, 1)
            call.finish()
            assert await SummaryWorker(store, family).process_one()
            frozen = store.db.execute("SELECT id,chat_id FROM outbox").fetchone()
            # Reproduce a historical waiting row with a fixed destination and a missing binding.
            with store.transaction() as db:
                db.execute(
                    "UPDATE outbox SET state='waiting_destination' WHERE id=?", (frozen["id"],)
                )
                db.execute("DELETE FROM bindings WHERE profile_id='member_a'")
            if binding_method == "invite":
                assert original.claim(original.invite("member_a"), "102")
            else:
                changed = replace(
                    family,
                    profiles=(
                        replace(family.profiles[0], telegram_chat_id="102"),
                        *family.profiles[1:],
                    ),
                )
                Enrollment(store, changed).import_explicit_destinations()
            row = store.db.execute(
                "SELECT chat_id,state FROM outbox WHERE id=?", (frozen["id"],)
            ).fetchone()
            assert tuple(row) == ("101", "waiting_destination")
            assert not await DeliveryWorker(store, family, CapturedTelegram()).process_one()

    asyncio.run(scenario())


def test_transient_telegram_startup_resumes_delivery_of_same_durable_report(
    tmp_path,
    family,
    monkeypatch,
):
    class RetryAPI(CapturedTelegram):
        def __init__(self):
            super().__init__()
            self.checks = 0
            self.delivered = asyncio.Event()

        async def request(self, method, payload):
            if method == "getWebhookInfo":
                self.checks += 1
                if self.checks == 1:
                    raise TelegramError(429, 7)
                return {"url": ""}
            if method == "getUpdates":
                await asyncio.Event().wait()
            result = await super().request(method, payload)
            self.delivered.set()
            return result

    actual_sleep = asyncio.sleep
    intervals = []

    async def fast_sleep(seconds):
        intervals.append(seconds)
        await actual_sleep(0)

    monkeypatch.setattr(telegram.asyncio, "sleep", fast_sleep)

    async def scenario():
        path = tmp_path / "state"
        with Store(path) as store:
            enrollment = Enrollment(store, family)
            enrollment.claim(enrollment.invite("member_a"), "101")
            call = CallController(store, family, Incoming("synthetic"), operator_profile="member_a")
            await call.text("a", "caller", "Synthetic reminder", False, 1)
            call.finish()
            assert await SummaryWorker(store, family).process_one()
            row = store.db.execute("SELECT id,chat_id,text FROM outbox").fetchone()
        with Store(path) as store:
            api = RetryAPI()
            run = asyncio.create_task(
                telegram.run_workers(store, family, api, SummaryWorker(store, family))
            )
            try:
                await asyncio.wait_for(api.delivered.wait(), 1)
                assert api.checks == 2 and 7 in intervals
                assert api.receipts == [{"chat_id": row["chat_id"], "text": row["text"]}]
                assert (
                    store.db.execute(
                        "SELECT state FROM outbox WHERE id=?", (row["id"],)
                    ).fetchone()[0]
                    == "complete"
                )
            finally:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())
