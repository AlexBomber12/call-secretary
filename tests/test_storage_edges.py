import multiprocessing
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from call_secretary import storage
from call_secretary.storage import StorageError, Store


def initialize_shared_database(directory, start, observed, outcomes):
    original_connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            if sql == "PRAGMA user_version" and not self.in_transaction:
                # Synchronize the old read-before-lock behavior to expose its race.
                observed.wait(4)
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, factory=Connection, **kwargs)

    storage.sqlite3.connect = connect
    try:
        if not start.wait(10):
            raise TimeoutError("test start gate")
        with Store(directory, mode="live") as store:
            store.create_call("synthetic")
        outcomes.put("ok")
    except Exception as exc:
        outcomes.put(type(exc).__name__)
    finally:
        storage.sqlite3.connect = original_connect


@pytest.mark.parametrize("preexisting_empty", [False, True])
def test_two_processes_can_initialize_the_same_empty_private_database(tmp_path, preexisting_empty):
    directory = tmp_path / "state"
    if preexisting_empty:
        directory.mkdir(mode=0o700)
        path = directory / "secretary.sqlite3"
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.close()
        path.chmod(0o600)

    ctx = multiprocessing.get_context("spawn")
    start, observed, outcomes = ctx.Event(), ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=initialize_shared_database, args=(directory, start, observed, outcomes))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        start.set()
        assert [outcomes.get(timeout=15) for _ in processes] == ["ok", "ok"]
        with Store(directory, mode="live") as store:
            assert store.counts()["calls"] == 2
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        outcomes.close()


@pytest.mark.parametrize("leaf", ["state", "secretary.sqlite3"])
def test_state_rejects_symlink_targets_without_opening_them(tmp_path, monkeypatch, leaf):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == leaf or original(p))
    with pytest.raises(StorageError, match="symlink"):
        Store(tmp_path / "state")


@pytest.mark.parametrize("bad_leaf", ["state", "secretary.sqlite3", None])
def test_posix_state_permissions_are_private(tmp_path, monkeypatch, bad_leaf):
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        values = list(result)
        values[0] = (values[0] & ~0o777) | (0o777 if path.name == bad_leaf else 0o700)
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(storage, "os", SimpleNamespace(**{**vars(os), "name": "posix"}))
    if bad_leaf:
        with pytest.raises(StorageError, match="permissions"):
            Store(tmp_path / "state")
    else:
        with Store(tmp_path / "state"):
            pass


def test_database_creation_race_keeps_existing_file(tmp_path, monkeypatch):
    def race(*args):
        fd = os.open(*args)
        os.close(fd)
        raise FileExistsError

    monkeypatch.setattr(storage, "os", SimpleNamespace(**{**vars(os), "open": race}))
    with Store(tmp_path / "state") as store:
        assert store.counts()["calls"] == 0


@pytest.mark.parametrize("outcome", ["retry", "deadline", "permission", "no_wal"])
def test_wal_initialization_only_retries_a_bounded_busy_error(monkeypatch, outcome):
    db = Mock()
    wait = Mock()
    monkeypatch.setattr(
        storage,
        "time",
        SimpleNamespace(
            monotonic=lambda: 0 if not db.execute.called else 6 if outcome == "deadline" else 1,
            sleep=wait,
        ),
    )
    if outcome == "no_wal":
        db.execute.return_value.fetchone.return_value = ("delete",)
        with pytest.raises(StorageError, match="enable WAL"):
            storage.enable_wal(db)
    else:
        error = sqlite3.OperationalError("synthetic error")
        error.sqlite_errorcode = (
            sqlite3.SQLITE_READONLY if outcome == "permission" else sqlite3.SQLITE_BUSY
        )
        db.execute.side_effect = [error, Mock(fetchone=lambda: ("wal",))]
        if outcome == "retry":
            storage.enable_wal(db)
        else:
            with pytest.raises(sqlite3.OperationalError):
                storage.enable_wal(db)
    assert db.execute.call_count == (2 if outcome == "retry" else 1)
    assert wait.call_count == int(outcome == "retry")


def test_corrupt_database_is_rejected_with_sanitized_error(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    path = directory / "secretary.sqlite3"
    path.write_bytes(b"synthetic invalid SQLite file")
    path.chmod(0o600)
    with pytest.raises(StorageError, match="initialize"):
        Store(directory)


@pytest.mark.parametrize("schema", ["unknown-table", "future-version"])
def test_unrecognized_database_is_not_migrated(tmp_path, schema):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    path = directory / "secretary.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE alien(value)" if schema == "unknown-table" else "PRAGMA user_version=2"
        )
    path.chmod(0o600)
    with pytest.raises(StorageError):
        Store(directory)


def test_unlabelled_state_mode_must_match_existing_history_and_bindings(tmp_path):
    with Store(tmp_path / "state") as store:
        with pytest.raises(StorageError, match="Unknown"):
            store.assert_mode("unexpected")
        store.create_call("simulation")
        with pytest.raises(StorageError, match="Existing"):
            store.assert_mode("live")
        store.assert_mode("simulation")
        store.assert_mode("simulation")
    with Store(tmp_path / "other") as store:
        store.db.execute("INSERT INTO bindings VALUES('member_a','101',1)")
        with pytest.raises(StorageError, match="Existing"):
            store.assert_mode("simulation")


def test_invalid_calls_utterances_jobs_and_report_fail_atomically(tmp_path):
    with Store(tmp_path / "state") as store:
        with pytest.raises(StorageError, match="Unknown call"):
            store.call("missing")
        with pytest.raises(StorageError, match="inactive"):
            store.route_call("missing", "member_a", "caller_selected")
        cid = store.create_call("fake")
        for role, text in [("system", "x"), ("caller", ""), ("caller", "x" * 16001)]:
            with pytest.raises(StorageError):
                store.utterance(cid, "bad", role, text)
        store.route_call(cid, "member_a", "caller_selected")
        assert not store.complete_summary(cid, "missing", {"report": "x"}, None)
        store.end_call(cid)
        with pytest.raises(StorageError, match="inactive"):
            store.route_call(cid, "member_a", "caller_selected")
        job = store.claim_job("summary_jobs")
        with pytest.raises(StorageError, match="fit"):
            store.complete_summary(cid, job["lease_token"], {"report": "x" * 4001}, None)
        assert store.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
        assert store.complete_summary(cid, job["lease_token"], {"report": "x"}, None)
        assert not store.complete_summary(cid, job["lease_token"], {"report": "x"}, None)
        with pytest.raises(StorageError, match="job type"):
            store.claim_job("invalid")
        with pytest.raises(StorageError, match="job type"):
            store.retry_job("invalid", cid, "", 1)
        store.backup(tmp_path / "copy")
        with pytest.raises(StorageError, match="already exists"):
            store.backup(tmp_path / "copy")
