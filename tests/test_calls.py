import asyncio
from pathlib import Path

import pytest

from call_secretary.calls import CallController, simulate
from call_secretary.config import load_family
from call_secretary.locking import InstanceLock
from call_secretary.routing import Incoming
from call_secretary.storage import StorageError, Store


def test_recipient_tool_only_accepts_approved_profile_and_cannot_redirect_report(tmp_path):
    async def scenario():
        family = load_family(Path("config/family.example.yaml"))
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("shared", "+39000000999"))
            assert call.profile is None
            await call.tool("select_recipient", {"profile_id": "member_a", "chat_id": "999"})
            assert call.profile is None
            reply = await call.tool("select_recipient", {"profile_id": "member_b"})
            assert call.profile.id == "member_b"
            assert "Persona A" not in reply.instructions
            await call.tool("select_recipient", {"profile_id": "member_a"})
            assert store.call(call.cid)["profile_id"] == "member_b"
            call.finish()

    asyncio.run(scenario())


def test_unresolved_after_three_attempts_stays_unassigned(tmp_path):
    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(
                store, load_family(Path("config/family.example.yaml")), Incoming("shared")
            )
            for _ in range(3):
                reply = await call.tool("select_recipient", {"profile_id": "unknown"})
            assert reply.finish
            call.finish()
            assert store.call(call.cid)["profile_id"] is None

    asyncio.run(scenario())


def test_late_events_and_duplicate_finish_do_not_reopen_call(tmp_path):
    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(
                store, load_family(Path("config/family.example.yaml")), Incoming("shared")
            )
            call.finish()
            call.finish()
            await call.text("late", "caller", "late message", False, 10)
            await call.tool("select_recipient", {"profile_id": "member_a"})
            assert store.transcript(call.cid) == []
            assert store.counts()["summary_pending"] == 1

    asyncio.run(scenario())


def test_only_one_active_family_call(tmp_path):
    with Store(tmp_path / "state") as store:
        family = load_family(Path("config/family.example.yaml"))
        first = CallController(store, family, Incoming("shared"))
        with pytest.raises(StorageError, match="active"):
            CallController(store, family, Incoming("shared"))
        first.finish()
        CallController(store, family, Incoming("shared")).finish()


def test_process_lock_prevents_another_runtime_and_releases_on_exit(tmp_path):
    with InstanceLock(tmp_path / "runtime.lock"):
        with pytest.raises(RuntimeError):
            with InstanceLock(tmp_path / "runtime.lock"):
                pass
    with InstanceLock(tmp_path / "runtime.lock"):
        pass


def test_simulation_does_not_reserve_real_budget(tmp_path):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(
                store,
                load_family(Path("config/family.example.yaml")),
                "member_c",
                "Synthetic message",
            )
            assert store.call(cid)["profile_id"] == "member_c"
            assert store.db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0

    asyncio.run(scenario())
