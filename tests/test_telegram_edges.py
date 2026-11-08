import asyncio
from dataclasses import replace

import httpx
import pytest
from test_telegram import API

from call_secretary import telegram
from call_secretary.calls import simulate
from call_secretary.config import ConfigError
from call_secretary.storage import Store
from call_secretary.summaries import SummaryWorker
from call_secretary.telegram import (
    BotPoller,
    CapturedTelegram,
    DeliveryWorker,
    Enrollment,
    TelegramClient,
    TelegramError,
    run_workers,
)


@pytest.mark.parametrize("token", ["", "bad token", "bad/token"])
def test_token_format_is_validated_without_exposing_it(token):
    with pytest.raises(ConfigError):
        TelegramClient(token)


def test_capture_sink_cannot_be_used_as_a_live_bot():
    with pytest.raises(RuntimeError):
        asyncio.run(CapturedTelegram().request("getMe", {}))


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json={"ok": True, "result": {"id": 1}}), None),
        (httpx.Response(200, content=b"x" * 524289), 0),
        (httpx.Response(200, json=[]), 0),
        (httpx.Response(200, content=b"not-json"), 0),
        (
            httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}
            ),
            429,
        ),
        (
            httpx.Response(
                400, json={"ok": False, "error_code": "bad", "parameters": {"retry_after": True}}
            ),
            0,
        ),
        (httpx.Response(400, json={"ok": False, "parameters": []}), 400),
    ],
    ids=[
        "ok",
        "oversized",
        "array",
        "invalid-json",
        "retry",
        "invalid-codes",
        "invalid-parameters",
    ],
)
def test_http_response_validation_is_sanitized(response, expected):
    async def scenario():
        client = TelegramClient(
            "synthetic-token", transport=httpx.MockTransport(lambda request: response)
        )
        try:
            with pytest.raises(TelegramError):
                await client.request("deleteWebhook", {})
            if expected is None:
                assert await client.request("getMe", {}) == {"id": 1}
            else:
                with pytest.raises(TelegramError) as err:
                    await client.request("getMe", {})
                assert err.value.code == expected
        finally:
            await client.close()

    asyncio.run(scenario())


def test_invalid_invites_and_explicit_destination_changes_fail_closed(tmp_path, family):
    with Store(tmp_path / "state") as store:
        enrollment = Enrollment(store, family)
        with pytest.raises(ConfigError, match="lifetime"):
            enrollment.invite("member_a", ttl_seconds=1)
        assert not enrollment.claim("short", "101")
        assert not enrollment.claim("x" * 65, "101")
        assert not enrollment.claim("x" * 20, "")
        profile = replace(family.profiles[0], telegram_chat_id="101")
        configured = replace(family, profiles=(profile, *family.profiles[1:]))
        explicit = Enrollment(store, configured)
        with pytest.raises(ConfigError, match="YAML"):
            explicit.unbind("member_a")
        explicit.import_explicit_destinations()
        explicit.import_explicit_destinations()
        store.db.execute("UPDATE bindings SET chat_id='102' WHERE profile_id='member_a'")
        with pytest.raises(ConfigError, match="differs"):
            explicit.import_explicit_destinations()


@pytest.mark.parametrize("reason", ["missing", "disabled", "revoked", "mismatch"])
def test_delivery_blocks_invalid_private_destination(tmp_path, family, reason):
    async def scenario():
        with Store(tmp_path / "state") as store:
            enroll = Enrollment(store, family)
            enroll.claim(enroll.invite("member_a"), "101")
            await simulate(store, family, "member_a", "synthetic private report")
            await SummaryWorker(store, family).process_one()
            actual_family = family
            if reason == "missing":
                store.db.execute("DELETE FROM bindings")
            elif reason == "disabled":
                actual_family = replace(
                    family, profiles=(replace(family.profiles[0], enabled=False),)
                )
            elif reason == "revoked":
                store.db.execute("UPDATE bindings SET enabled=0")
            else:
                store.db.execute("UPDATE bindings SET chat_id='102'")
            api = API()
            assert await DeliveryWorker(store, actual_family, api).process_one()
            assert not api.sent
            assert store.db.execute("SELECT state FROM outbox").fetchone()[0] == "blocked"

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [401, 409, 403, 500])
def test_delivery_preserves_pending_work_on_auth_or_transient_errors(tmp_path, family, code):
    async def scenario():
        with Store(tmp_path / "state") as store:
            enroll = Enrollment(store, family)
            enroll.claim(enroll.invite("member_a"), "101")
            await simulate(store, family, "member_a", "synthetic")
            await SummaryWorker(store, family).process_one()
            worker = DeliveryWorker(store, family, API(failure=TelegramError(code)))
            if code in {401, 409}:
                with pytest.raises(TelegramError):
                    await worker.process_one(now=10)
            else:
                assert await worker.process_one(now=10)
            row = store.db.execute("SELECT * FROM outbox").fetchone()
            assert row["state"] == ("blocked" if code == 403 else "pending")
            assert row["not_before"] > 10

    asyncio.run(scenario())


def test_poller_ignores_stale_and_invalid_updates_and_retains_binding_on_ack_failure(
    tmp_path, family
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            store.db.execute("INSERT INTO metadata VALUES('telegram_offset','5')")
            token = Enrollment(store, family).invite("member_a")
            private = {"chat": {"id": 101, "type": "private"}, "from": {"id": 101}}
            api = API(
                failure=TelegramError(500),
                updates=[
                    {"update_id": "bad"},
                    {"update_id": 1},
                    {"update_id": 5, "message": private | {"text": "/start"}},
                    {"update_id": 6, "message": private | {"text": "/start " + token}},
                    {"update_id": 7, "message": private | {"text": "ordinary words"}},
                ],
            )
            await BotPoller(store, family, api).poll_once()
            assert store.db.execute("SELECT chat_id FROM bindings").fetchone()[0] == "101"
            api.updates = {"unexpected": True}
            with pytest.raises(TelegramError):
                await BotPoller(store, family, api).poll_once()

    asyncio.run(scenario())


def test_worker_group_retries_transient_poll_errors_then_stops_on_auth_failure(
    tmp_path, family, monkeypatch
):
    original_sleep = asyncio.sleep
    counts = {"summary": 0, "delivery": 0, "poll": 0}
    sleeps = []

    class Summary:
        async def process_one(self):
            counts["summary"] += 1

    class Delivery:
        def __init__(self, *args):
            pass

        async def process_one(self):
            counts["delivery"] += 1

    class Poll(API):
        async def request(self, method, payload):
            if method == "getUpdates":
                counts["poll"] += 1
                if counts["poll"] == 1:
                    raise TelegramError(429, 3)
                if counts["poll"] > 2:
                    raise TelegramError(401)
                return []
            return await super().request(method, payload)

    async def sleep(seconds):
        sleeps.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(telegram, "DeliveryWorker", Delivery)
    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)

    async def scenario():
        with Store(tmp_path / "state") as store:
            with pytest.raises(ExceptionGroup) as error:
                await run_workers(store, family, Poll(), Summary())
            assert any(isinstance(e, TelegramError) for e in error.value.exceptions)
            assert counts["summary"] and counts["delivery"] and counts["poll"] == 3
            assert 3 in sleeps and 0.5 in sleeps

    asyncio.run(scenario())
