"""Private SQLite state; atomic jobs, no call audio or provider event dumps."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    pass


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE calls (
 id TEXT PRIMARY KEY, ingress_id TEXT NOT NULL, caller_number TEXT,
 original_called_number TEXT, profile_id TEXT, route_source TEXT,
 status TEXT NOT NULL DEFAULT 'incoming', started REAL NOT NULL, ended REAL,
 voice_usage_complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE utterances (
 call_id TEXT NOT NULL REFERENCES calls(id), item_id TEXT NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 PRIMARY KEY (call_id,item_id)
);
CREATE TABLE summary_jobs (
 call_id TEXT PRIMARY KEY REFERENCES calls(id), state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE summaries (
 call_id TEXT PRIMARY KEY REFERENCES calls(id), content TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE outbox (
 id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), profile_id TEXT NOT NULL,
 chat_id TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
 UNIQUE(call_id,profile_id)
);
CREATE TABLE bindings (
 profile_id TEXT PRIMARY KEY, chat_id TEXT UNIQUE NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE invitations (
 profile_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL
);
CREATE TABLE budget_reservations (
 call_id TEXT NOT NULL REFERENCES calls(id), month TEXT NOT NULL,
 reserved INTEGER NOT NULL CHECK(reserved>=0), consumed INTEGER NOT NULL DEFAULT 0,
 closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(call_id,month)
);
CREATE TABLE usage_events (
 id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
 amount INTEGER NOT NULL CHECK(amount>=0), created REAL NOT NULL
);
CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
PRAGMA user_version=1;
COMMIT;
"""


class Store:
    def __init__(self, directory: Path, *, mode: str | None = None):
        directory = directory.absolute()
        if directory.is_symlink():
            raise StorageError("State directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt" and directory.stat().st_mode & 0o077:
            raise StorageError("State directory permissions must be 0700")
        path = directory / "secretary.sqlite3"
        if path.is_symlink():
            raise StorageError("State database must not be a symlink")
        if not path.exists():
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise StorageError("State database permissions must be 0600")
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                self.db.close()
                raise StorageError("Unrecognized existing database; refusing to migrate")
            self.db.executescript(SCHEMA)
        elif version != 1:
            self.db.close()
            raise StorageError("Unsupported database schema")
        if mode is not None:
            try:
                self.assert_mode(mode)
            except BaseException:
                self.db.close()
                raise

    def assert_mode(self, mode: str) -> None:
        if mode not in {"live", "simulation"}:
            raise StorageError("Unknown state mode")
        with self.transaction() as db:
            old = db.execute("SELECT value FROM metadata WHERE key='state_mode'").fetchone()
            if old and old[0] != mode:
                raise StorageError("Live and simulation state must use different directories")
            if not old:
                simulated = db.execute(
                    "SELECT 1 FROM calls WHERE ingress_id='simulation'"
                ).fetchone()
                real = db.execute("SELECT 1 FROM calls WHERE ingress_id!='simulation'").fetchone()
                bindings = db.execute("SELECT 1 FROM bindings").fetchone()
                if (mode == "live" and simulated) or (mode == "simulation" and (real or bindings)):
                    raise StorageError("Existing state cannot be assigned this mode")
                db.execute("INSERT INTO metadata VALUES('state_mode',?)", (mode,))

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def create_call(
        self,
        ingress_id: str,
        caller_number: str | None = None,
        original_called_number: str | None = None,
        *,
        now: float | None = None,
        single_active: bool = False,
    ) -> str:
        cid = str(uuid.uuid4())
        with self.transaction() as db:
            if single_active and db.execute("SELECT 1 FROM calls WHERE ended IS NULL").fetchone():
                raise StorageError("Another call is active")
            db.execute(
                "INSERT INTO calls(id,ingress_id,caller_number,original_called_number,started)"
                " VALUES(?,?,?,?,?)",
                (
                    cid,
                    ingress_id,
                    caller_number,
                    original_called_number,
                    time.time() if now is None else now,
                ),
            )
        return cid

    def call(self, cid: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise StorageError("Unknown call")
        return dict(row)

    def route_call(self, cid: str, profile_id: str, source: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT profile_id,ended FROM calls WHERE id=?", (cid,)).fetchone()
            if row is None or row["ended"] is not None:
                raise StorageError("Cannot route inactive call")
            if row["profile_id"] not in (None, profile_id):
                raise StorageError("Call recipient is already fixed")
            db.execute(
                "UPDATE calls SET profile_id=?,route_source=?,status='conversing' WHERE id=?",
                (profile_id, source, cid),
            )

    def utterance(
        self,
        cid: str,
        item_id: str,
        role: str,
        text: str,
        *,
        interrupted: bool = False,
        now: float | None = None,
    ) -> None:
        if role not in {"caller", "assistant"} or not text or len(text) > 16000:
            raise StorageError("Invalid textual utterance")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO utterances VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(call_id,item_id) DO UPDATE SET text=excluded.text,"
                "interrupted=MAX(utterances.interrupted,excluded.interrupted)",
                (cid, item_id, role, text, int(interrupted), time.time() if now is None else now),
            )

    def interrupt_utterance(self, cid: str, item_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE utterances SET interrupted=1 WHERE call_id=? AND item_id=?", (cid, item_id)
            )

    def transcript(self, cid: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT item_id,role,text,interrupted FROM utterances WHERE call_id=? "
                    "ORDER BY created,rowid",
                    (cid,),
                ).fetchall()
            ]

    def end_call(
        self,
        cid: str,
        status: str = "ended",
        *,
        now: float | None = None,
        voice_usage_complete: bool = False,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE calls SET status=?,ended=?,voice_usage_complete=? "
                "WHERE id=? AND ended IS NULL",
                (status, time.time() if now is None else now, int(voice_usage_complete), cid),
            )
            db.execute("INSERT OR IGNORE INTO summary_jobs(call_id) VALUES(?)", (cid,))

    def recover_calls(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self.transaction() as db:
            rows = db.execute("SELECT id FROM calls WHERE ended IS NULL").fetchall()
            for row in rows:
                db.execute(
                    "UPDATE calls SET status='interrupted',ended=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                db.execute("INSERT OR IGNORE INTO summary_jobs(call_id) VALUES(?)", (row["id"],))
        return len(rows)

    def claim_job(
        self, kind: str, *, now: float | None = None, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        if kind not in {"summary_jobs", "outbox"}:
            raise StorageError("Unknown job type")
        timestamp = time.time() if now is None else now
        key = "call_id" if kind == "summary_jobs" else "id"
        token = secrets.token_hex(16)
        with self.transaction() as db:
            row = db.execute(
                f"SELECT * FROM {kind} WHERE state IN ('pending','leased') "
                "AND not_before<=? AND lease_until<=? ORDER BY rowid LIMIT 1",
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                f"UPDATE {kind} SET state='leased',lease_token=?,lease_until=?,"
                f"attempts=attempts+1 WHERE {key}=?",
                (token, timestamp + lease_seconds, row[key]),
            )
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}

    def retry_job(
        self,
        kind: str,
        key_value: str,
        token: str,
        delay: float,
        *,
        now: float | None = None,
        blocked: bool = False,
    ) -> None:
        if kind not in {"summary_jobs", "outbox"}:
            raise StorageError("Unknown job type")
        key = "call_id" if kind == "summary_jobs" else "id"
        with self.transaction() as db:
            db.execute(
                f"UPDATE {kind} SET state=?,not_before=?,lease_until=0,lease_token=NULL "
                f"WHERE {key}=? AND lease_token=?",
                (
                    "blocked" if blocked else "pending",
                    (time.time() if now is None else now) + max(1, delay),
                    key_value,
                    token,
                ),
            )

    def complete_summary(
        self,
        cid: str,
        token: str,
        summary: dict[str, Any],
        destination: str | None,
        *,
        now: float | None = None,
        settle_budget: bool = False,
    ) -> bool:
        with self.transaction() as db:
            job = db.execute(
                "SELECT lease_token,state FROM summary_jobs WHERE call_id=?", (cid,)
            ).fetchone()
            if job is None or job["state"] != "leased" or job["lease_token"] != token:
                return False
            call = db.execute("SELECT profile_id FROM calls WHERE id=?", (cid,)).fetchone()
            content = json.dumps(summary, ensure_ascii=False)
            db.execute(
                "INSERT INTO summaries VALUES(?,?,?) ON CONFLICT(call_id) DO NOTHING",
                (cid, content, time.time() if now is None else now),
            )
            if call["profile_id"]:
                revoked = (
                    db.execute(
                        "SELECT 1 FROM metadata WHERE key=?", ("delivery_revoked:" + cid,)
                    ).fetchone()
                    is not None
                )
                text = str(summary["report"])
                if not 1 <= len(text) <= 4000:
                    raise StorageError("Report must fit one Telegram message")
                db.execute(
                    "INSERT OR IGNORE INTO outbox(id,call_id,profile_id,chat_id,text,state) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        cid,
                        call["profile_id"],
                        "" if revoked else (destination or ""),
                        text,
                        "blocked"
                        if revoked
                        else ("pending" if destination else "waiting_destination"),
                    ),
                )
            db.execute(
                "UPDATE summary_jobs SET state='complete',lease_token=NULL,lease_until=0 "
                "WHERE call_id=?",
                (cid,),
            )
            if settle_budget:
                db.execute("UPDATE budget_reservations SET closed=1 WHERE call_id=?", (cid,))
            return True

    def delivery_complete(self, oid: str, token: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE outbox SET state='complete',lease_token=NULL,lease_until=0 "
                "WHERE id=? AND lease_token=?",
                (oid, token),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                "summary_pending": self.db.execute(
                    "SELECT COUNT(*) FROM summary_jobs WHERE state!='complete'"
                ).fetchone()[0],
                "delivery_pending": self.db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state!='complete'"
                ).fetchone()[0],
            }

    def backup(self, destination: Path) -> None:
        if destination.exists():
            raise StorageError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with self._lock, sqlite3.connect(destination) as copy:
            self.db.backup(copy)
