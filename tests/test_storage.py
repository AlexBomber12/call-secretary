import sqlite3

import pytest

from call_secretary.storage import StorageError, Store


def test_restart_preserves_text_routing_jobs_and_old_history(tmp_path):
    with Store(tmp_path / "state") as store:
        cid = store.create_call("shared", now=1)
        store.route_call(cid, "member_a", "caller_selected")
        store.utterance(cid, "u1", "caller", "Please call tomorrow.", now=2)
        store.end_call(cid, now=3)
        store.end_call(cid, now=4)
    with Store(tmp_path / "state") as reopened:
        assert reopened.call(cid)["profile_id"] == "member_a"
        assert reopened.call(cid)["ended"] == 3
        assert reopened.transcript(cid)[0]["text"] == "Please call tomorrow."
        assert reopened.counts() == {"calls": 1, "summary_pending": 1, "delivery_pending": 0}


def test_transaction_rolls_back_on_failure(tmp_path):
    with Store(tmp_path / "state") as store:
        with pytest.raises(RuntimeError), store.transaction() as db:
            db.execute("INSERT INTO metadata VALUES('partial','wrong')")
            raise RuntimeError("simulated interruption")
        assert store.db.execute("SELECT * FROM metadata").fetchall() == []


def test_expired_lease_is_recovered_and_stale_worker_cannot_complete(tmp_path):
    with Store(tmp_path / "state") as store:
        cid = store.create_call("shared")
        store.route_call(cid, "member_a", "dedicated_line")
        store.end_call(cid)
        first = store.claim_job("summary_jobs", now=10, lease_seconds=5)
        assert store.claim_job("summary_jobs", now=11) is None
        second = store.claim_job("summary_jobs", now=16)
        assert not store.complete_summary(cid, first["lease_token"], {"report": "old"}, "123")
        assert store.complete_summary(cid, second["lease_token"], {"report": "valid"}, "123")
        assert store.counts()["delivery_pending"] == 1


def test_unassigned_call_is_not_broadcast_and_recipient_cannot_change(tmp_path):
    with Store(tmp_path / "state") as store:
        cid = store.create_call("shared")
        store.end_call(cid)
        job = store.claim_job("summary_jobs")
        store.complete_summary(cid, job["lease_token"], {"report": "private"}, "123")
        assert store.counts()["delivery_pending"] == 0
        other = store.create_call("shared")
        store.route_call(other, "member_a", "caller_selected")
        with pytest.raises(StorageError):
            store.route_call(other, "member_b", "caller_selected")


def test_recovery_marks_active_call_and_backup_is_consistent(tmp_path):
    with Store(tmp_path / "state") as store:
        cid = store.create_call("shared", now=1)
        assert store.recover_calls(now=20) == 1
        assert store.recover_calls(now=21) == 0
        assert store.call(cid)["status"] == "interrupted"
        store.backup(tmp_path / "backup.sqlite3")
        with sqlite3.connect(tmp_path / "backup.sqlite3") as copy:
            assert copy.execute("SELECT status FROM calls").fetchone()[0] == "interrupted"
