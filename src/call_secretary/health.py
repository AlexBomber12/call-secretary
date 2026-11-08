"""Content-free health snapshots; stale observations never imply readiness."""

from __future__ import annotations

import json
import time
from typing import Any

from call_secretary.storage import Store

COMPONENTS = ("modem", "audio", "voice", "telegram")


def update(store: Store, component: str, state: str, *, now: float | None = None) -> None:
    if component not in COMPONENTS or state not in {
        "starting",
        "idle",
        "active",
        "ready",
        "unavailable",
        "stopped",
    }:
        raise ValueError("Unknown health code")
    value = json.dumps({"state": state, "checked": time.time() if now is None else now})
    with store.transaction() as db:
        db.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("health:" + component, value),
        )


def snapshot(store: Store, *, now: float | None = None) -> dict[str, Any]:
    timestamp = time.time() if now is None else now
    result = {}
    with store.transaction() as db:
        for component in COMPONENTS:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", ("health:" + component,)
            ).fetchone()
            item = json.loads(row[0]) if row else {"state": "unknown", "checked": 0}
            age = timestamp - item["checked"]
            result[component] = {"state": item["state"] if not row or 0 <= age <= 30 else "stale"}
        result["queues"] = {
            table: {
                r[0]: r[1] for r in db.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state")
            }
            for table in ("summary_jobs", "outbox")
        }
    return result
