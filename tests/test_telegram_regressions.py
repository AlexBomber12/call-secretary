import asyncio
from dataclasses import replace

import pytest
from test_telegram import API

from call_secretary.calls import simulate
from call_secretary.storage import Store
from call_secretary.summaries import SummaryWorker, destination
from call_secretary.telegram import DeliveryWorker, Enrollment


@pytest.mark.parametrize("active", [False, True])
def test_unbind_revokes_ungenerated_reports_including_active_calls(tmp_path, family, active):
    async def scenario():
        with Store(tmp_path / "state") as store:
            enroll = Enrollment(store, family)
            enroll.claim(enroll.invite("member_a"), "101")
            if active:
                cid = store.create_call("synthetic")
                store.route_call(cid, "member_a", "caller_selected")
                store.utterance(cid, "a", "caller", "private old message")
            else:
                cid = await simulate(store, family, "member_a", "private old message")
            enroll.unbind("member_a")
            enroll.claim(enroll.invite("member_a"), "102")
            if active:
                store.end_call(cid)
            await SummaryWorker(store, family).process_one()
            api = API()
            await DeliveryWorker(store, family, api).process_one()
            assert api.sent == []
            assert (
                store.db.execute("SELECT state FROM outbox WHERE call_id=?", (cid,)).fetchone()[0]
                == "blocked"
            )
            new = await simulate(store, family, "member_a", "new message")
            await SummaryWorker(store, family).process_one()
            await DeliveryWorker(store, family, api).process_one()
            assert len(api.sent) == 1 and api.sent[0]["chat_id"] == "102"
            assert new in api.sent[0]["text"] and cid not in api.sent[0]["text"]

    asyncio.run(scenario())


def test_yaml_import_releases_waiting_reports_and_refreshes_enablement(tmp_path, family):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, "member_a", "waiting private message")
            await SummaryWorker(store, family).process_one()
            configured = replace(
                family,
                profiles=(replace(family.profiles[0], telegram_chat_id="101", enabled=False),),
            )
            Enrollment(store, configured).import_explicit_destinations()
            assert destination(store, configured.profiles[0]) is None
            assert (
                store.db.execute("SELECT state FROM outbox").fetchone()[0] == "waiting_destination"
            )
        with Store(tmp_path / "state") as store:
            enabled = replace(configured, profiles=(replace(configured.profiles[0], enabled=True),))
            Enrollment(store, enabled).import_explicit_destinations()
            assert destination(store, enabled.profiles[0]) == "101"
            api = API()
            assert await DeliveryWorker(store, enabled, api).process_one()
            assert len(api.sent) == 1 and cid in api.sent[0]["text"]

    asyncio.run(scenario())
