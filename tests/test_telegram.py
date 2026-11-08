import asyncio
from pathlib import Path

import httpx
import pytest

from call_secretary.calls import simulate
from call_secretary.config import ConfigError, load_family
from call_secretary.storage import Store
from call_secretary.summaries import SummaryWorker
from call_secretary.telegram import (
    BotPoller,
    DeliveryWorker,
    Enrollment,
    TelegramClient,
    TelegramError,
    run_workers,
)


class API:
    def __init__(self, *, failure=None, updates=None, webhook=""):
        self.sent = []
        self.failure = failure
        self.updates = updates or []
        self.webhook = webhook

    async def request(self, method, payload):
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "getUpdates":
            return self.updates
        if self.failure:
            raise self.failure
        self.sent.append(payload)
        return {"message_id": 1}


def family():
    return load_family(Path("config/family.example.yaml"))


def test_expiring_one_time_invites_are_hashed_and_cannot_claim_two_profiles(tmp_path):
    with Store(tmp_path / "state") as store:
        enrollment = Enrollment(store, family())
        token = enrollment.invite("member_a", now=10)
        assert token not in str(store.db.execute("SELECT * FROM invitations").fetchall())
        assert not enrollment.claim(token, "100", now=4000)
        assert enrollment.claim(token, "100", now=20)
        assert not enrollment.claim(token, "101", now=21)
        other = enrollment.invite("member_b", now=10)
        assert not enrollment.claim(other, "100", now=20)
        with pytest.raises(ConfigError):
            enrollment.invite("member_a")


def test_three_profiles_only_receive_their_own_report(tmp_path):
    async def scenario():
        f = family()
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, f)
            calls = []
            for n, p in enumerate(f.profiles, start=101):
                calls.append(await simulate(store, f, p.id, f"Synthetic message {n}"))
                await SummaryWorker(store, f).process_one()
                assert enrollment.claim(enrollment.invite(p.id), str(n))
            api = API()
            delivery = DeliveryWorker(store, f, api)
            for _ in f.profiles:
                assert await delivery.process_one()
            assert [m["chat_id"] for m in api.sent] == ["101", "102", "103"]
            for msg, cid in zip(api.sent, calls, strict=True):
                assert cid in msg["text"]
                assert all(other not in msg["text"] for other in calls if other != cid)
            assert store.counts()["delivery_pending"] == 0

    asyncio.run(scenario())


def test_retry_after_and_restart_preserve_destination_and_report(tmp_path):
    async def scenario():
        f = family()
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, f)
            enrollment.claim(enrollment.invite("member_a"), "101")
            cid = await simulate(store, f, "member_a", "Synthetic retry")
            await SummaryWorker(store, f).process_one()
            await DeliveryWorker(store, f, API(failure=TelegramError(429, 60))).process_one(now=10)
            assert store.claim_job("outbox", now=69) is None
        with Store(tmp_path / "state") as store:
            api = API()
            await DeliveryWorker(store, f, api).process_one(now=71)
            assert api.sent[0]["chat_id"] == "101" and cid in api.sent[0]["text"]

    asyncio.run(scenario())


def test_reassignment_never_redirects_an_existing_report_to_new_chat(tmp_path):
    async def scenario():
        f = family()
        with Store(tmp_path / "state") as store:
            enrollment = Enrollment(store, f)
            enrollment.claim(enrollment.invite("member_a"), "101")
            await simulate(store, f, "member_a", "Private old destination")
            await SummaryWorker(store, f).process_one()
            enrollment.unbind("member_a")
            enrollment.claim(enrollment.invite("member_a"), "102")
            api = API()
            await DeliveryWorker(store, f, api).process_one()
            assert api.sent == []
            assert store.counts()["delivery_pending"] == 1

    asyncio.run(scenario())


def test_private_start_enrolls_and_group_start_cannot_claim(tmp_path):
    async def scenario():
        f = family()
        with Store(tmp_path / "state") as store:
            token = Enrollment(store, f).invite("member_a")
            updates = [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": -1, "type": "group"},
                        "from": {"id": 101},
                        "text": "/start " + token,
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": 101, "type": "private"},
                        "from": {"id": 101},
                        "text": "/start " + token,
                    },
                },
            ]
            api = API(updates=updates)
            await BotPoller(store, f, api).poll_once()
            assert len(api.sent) == 1
            assert store.db.execute("SELECT chat_id FROM bindings").fetchone()[0] == "101"
            assert (
                store.db.execute(
                    "SELECT value FROM metadata WHERE key='telegram_offset'"
                ).fetchone()[0]
                == "3"
            )

    asyncio.run(scenario())


def test_client_errors_and_repr_never_reveal_bot_token():
    async def scenario():
        token = "unit-test-private-token"

        def handler(request):
            raise httpx.ConnectError("private URL " + str(request.url))

        client = TelegramClient(token, transport=httpx.MockTransport(handler))
        try:
            assert token not in repr(client)
            with pytest.raises(TelegramError) as error:
                await client.request("getMe", {})
            assert token not in str(error.value)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_existing_webhook_is_not_changed_or_consumed(tmp_path):
    async def scenario():
        with Store(tmp_path / "state") as store:
            api = API(webhook="https://example.invalid/existing")
            with pytest.raises(ConfigError):
                await run_workers(store, family(), api, None)
            assert api.sent == []

    asyncio.run(scenario())
