import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from call_secretary import storage
from call_secretary.storage import StorageError, Store


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
