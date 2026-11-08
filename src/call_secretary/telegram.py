"""Private Telegram enrollment and durable delivery. Tokens never enter logs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from typing import Any, Protocol

import httpx

from call_secretary.config import ConfigError, Family, chat_id
from call_secretary.storage import Store


class TelegramError(RuntimeError):
    def __init__(self, code: int = 0, retry_after: int = 0):
        super().__init__(f"Telegram request failed (code {code})")
        self.code = code
        self.retry_after = min(86400, max(0, retry_after))

    @property
    def transient(self) -> bool:
        return self.code in {0, 429} or self.code >= 500


class TelegramAPI(Protocol):
    async def request(self, method: str, payload: dict[str, Any]) -> Any: ...


class CapturedTelegram:
    """Offline simulation sink, never a Telegram connection."""

    def __init__(self) -> None:
        self.receipts: list[dict[str, Any]] = []

    async def request(self, method: str, payload: dict[str, Any]) -> Any:
        if method != "sendMessage":
            raise RuntimeError("Simulation supports only captured reports")
        self.receipts.append(dict(payload))
        return {"message_id": len(self.receipts)}


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not token or any(c.isspace() or c in "/?&#" for c in token):
            raise ConfigError("Invalid Telegram token format")
        self._token = token
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport)

    def __repr__(self) -> str:
        return "TelegramClient(<redacted>)"

    async def close(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {"getMe", "getUpdates", "sendMessage", "getWebhookInfo"}:
            raise TelegramError(400)
        try:
            response = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/{method}", json=payload
            )
            if len(response.content) > 524288:
                raise TelegramError(0)
            body = response.json()
            if not isinstance(body, dict):
                raise TelegramError(0)
            if response.status_code >= 400 or body.get("ok") is not True:
                code = body.get("error_code", response.status_code)
                parameters = body.get("parameters")
                delay = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
                raise TelegramError(
                    code if type(code) is int else 0, delay if type(delay) is int else 0
                )
            return body.get("result")
        except (httpx.HTTPError, ValueError, TypeError):
            raise TelegramError(0) from None


class Enrollment:
    def __init__(self, store: Store, family: Family):
        self.store, self.family = store, family

    def invite(self, profile_id: str, *, ttl_seconds: int = 3600, now: float | None = None) -> str:
        self.family.profile(profile_id)
        if not 60 <= ttl_seconds <= 86400:
            raise ConfigError("Invitation lifetime must be 60..86400 seconds")
        token = "v1_" + secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM bindings WHERE profile_id=?", (profile_id,)).fetchone():
                raise ConfigError(
                    "Profile is already bound; explicitly unbind it before reassignment"
                )
            db.execute(
                "INSERT INTO invitations VALUES(?,?,?) ON CONFLICT(profile_id) DO UPDATE "
                "SET token_hash=excluded.token_hash,expires=excluded.expires",
                (profile_id, digest, (time.time() if now is None else now) + ttl_seconds),
            )
        return token

    def claim(self, token: str, destination: str, *, now: float | None = None) -> bool:
        target = chat_id(destination, "invitation chat")
        if target is None or not 10 <= len(token) <= 64:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        timestamp = time.time() if now is None else now
        with self.store.transaction() as db:
            invitation = db.execute(
                "SELECT * FROM invitations WHERE token_hash=? AND expires>=?", (digest, timestamp)
            ).fetchone()
            if invitation is None:
                return False
            pid = invitation["profile_id"]
            try:
                self.family.profile(pid)
            except ConfigError:
                db.execute("DELETE FROM invitations WHERE profile_id=?", (pid,))
                return False
            if db.execute(
                "SELECT 1 FROM bindings WHERE profile_id=? OR chat_id=?", (pid, target)
            ).fetchone():
                return False
            db.execute("INSERT INTO bindings VALUES(?,?,1)", (pid, target))
            db.execute("DELETE FROM invitations WHERE profile_id=?", (pid,))
            db.execute(
                "UPDATE outbox SET chat_id=?,state='pending',not_before=0 "
                "WHERE profile_id=? AND state='waiting_destination'",
                (target, pid),
            )
        return True

    def unbind(self, profile_id: str) -> None:
        if self.family.profile(profile_id).telegram_chat_id:
            raise ConfigError("Remove the explicit YAML destination before unbinding this profile")
        with self.store.transaction() as db:
            # Include active calls and jobs whose outbox row does not exist yet.
            # The marker is checked atomically when a summary creates its report.
            db.execute(
                "INSERT OR IGNORE INTO metadata(key,value) "
                "SELECT 'delivery_revoked:' || id,'1' FROM calls WHERE profile_id=?",
                (profile_id,),
            )
            db.execute("DELETE FROM bindings WHERE profile_id=?", (profile_id,))
            db.execute("DELETE FROM invitations WHERE profile_id=?", (profile_id,))
            db.execute(
                "UPDATE outbox SET state='blocked',lease_token=NULL,lease_until=0 "
                "WHERE profile_id=? AND state!='complete'",
                (profile_id,),
            )

    def import_explicit_destinations(self) -> None:
        """Import YAML destinations and synchronize all existing private bindings."""
        with self.store.transaction() as db:
            for p in self.family.profiles:
                if p.telegram_chat_id:
                    old = db.execute(
                        "SELECT chat_id FROM bindings WHERE profile_id=?", (p.id,)
                    ).fetchone()
                    if old and old[0] != p.telegram_chat_id:
                        raise ConfigError(
                            "Destination differs from enrolled chat; explicit unbind required"
                        )
                    db.execute(
                        "INSERT OR IGNORE INTO bindings VALUES(?,?,?)",
                        (p.id, p.telegram_chat_id, int(p.enabled)),
                    )
                db.execute(
                    "UPDATE bindings SET enabled=? WHERE profile_id=?", (int(p.enabled), p.id)
                )
                db.execute(
                    "UPDATE outbox SET chat_id=(SELECT chat_id FROM bindings WHERE profile_id=?),"
                    "state='pending',not_before=0 WHERE profile_id=? "
                    "AND state='waiting_destination' AND EXISTS "
                    "(SELECT 1 FROM bindings WHERE profile_id=? AND enabled=1)",
                    (p.id, p.id, p.id),
                )


class DeliveryWorker:
    def __init__(self, store: Store, family: Family, api: TelegramAPI):
        self.store, self.family, self.api = store, family, api

    async def process_one(self, *, now: float | None = None) -> bool:
        row = self.store.claim_job("outbox", now=now)
        if row is None:
            return False
        oid, token = row["id"], row["lease_token"]
        try:
            profile = self.family.profile(row["profile_id"])
        except ConfigError:
            self.store.retry_job("outbox", oid, token, 1, now=now, blocked=True)
            return True
        with self.store.transaction() as db:
            binding = db.execute(
                "SELECT * FROM bindings WHERE profile_id=?", (profile.id,)
            ).fetchone()
        allowed = bool(
            profile.enabled
            and binding
            and binding["enabled"]
            and binding["chat_id"] == row["chat_id"]
        )
        if not allowed:
            self.store.retry_job("outbox", oid, token, 1, now=now, blocked=True)
            return True
        try:
            await self.api.request("sendMessage", {"chat_id": row["chat_id"], "text": row["text"]})
            self.store.delivery_complete(oid, token)
        except TelegramError as exc:
            delay = max(exc.retry_after, min(3600, 2 ** min(12, row["attempts"])))
            self.store.retry_job(
                "outbox",
                oid,
                token,
                delay,
                now=now,
                blocked=not exc.transient and exc.code not in {401, 409},
            )
            if exc.code in {401, 409}:
                raise
        return True


class BotPoller:
    def __init__(self, store: Store, family: Family, api: TelegramAPI):
        self.store, self.family, self.api = store, family, api

    async def poll_once(self) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='telegram_offset'").fetchone()
        offset = int(row[0]) if row else 0
        updates = await self.api.request(
            "getUpdates",
            {"offset": offset, "timeout": 20, "limit": 10, "allowed_updates": ["message"]},
        )
        if not isinstance(updates, list):
            raise TelegramError(0)
        for update in updates:
            uid = update.get("update_id")
            if type(uid) is not int or uid < offset:
                continue
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            author = msg.get("from", {})
            text = msg.get("text", "")
            private = (
                chat.get("type") == "private"
                and type(chat.get("id")) is int
                and chat["id"] > 0
                and author.get("id") == chat["id"]
            )
            if private and isinstance(text, str) and text.split(maxsplit=1)[0:1] == ["/start"]:
                parts = text.split(maxsplit=1)
                claimed = len(parts) == 2 and Enrollment(self.store, self.family).claim(
                    parts[1], str(chat["id"])
                )
                reply = (
                    "Collegamento attivato. Riceverai qui i messaggi destinati a te."
                    if claimed
                    else "Apri un invito personale valido del proprietario."
                )
                try:
                    await self.api.request(
                        "sendMessage", {"chat_id": str(chat["id"]), "text": reply}
                    )
                except TelegramError:
                    pass  # Binding is durable even if this acknowledgement cannot be delivered.
            offset = uid + 1
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO metadata VALUES('telegram_offset',?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value",
                    (str(offset),),
                )


async def run_workers(store: Store, family: Family, api: TelegramAPI, summary_worker: Any) -> None:
    webhook = await api.request("getWebhookInfo", {})
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise ConfigError(
            "Use a dedicated bot without an active webhook; existing bot settings are unchanged"
        )
    Enrollment(store, family).import_explicit_destinations()
    delivery, poller = DeliveryWorker(store, family, api), BotPoller(store, family, api)

    async def summaries() -> None:
        while True:
            await summary_worker.process_one()
            await asyncio.sleep(0.5)

    async def deliveries() -> None:
        while True:
            await delivery.process_one()
            await asyncio.sleep(0.5)

    async def updates() -> None:
        while True:
            try:
                await poller.poll_once()
            except TelegramError as exc:
                if not exc.transient:
                    raise
                await asyncio.sleep(max(2, exc.retry_after))

    async with asyncio.TaskGroup() as group:
        group.create_task(summaries())
        group.create_task(deliveries())
        group.create_task(updates())
