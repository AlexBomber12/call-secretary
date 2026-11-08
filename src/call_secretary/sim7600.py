"""Offline-tested SIM7600 control core; hardware-gated opener/service are in phone.py."""

from __future__ import annotations

import csv
import math
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from call_secretary.modem import ModemError
from call_secretary.routing import Incoming

COMMANDS = frozenset({"AT", "AT+CLCC=0", "AT+CLCC", "ATA", "AT+CHUP"})
TERMINAL = frozenset({"NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"})


class CommandRejected(ModemError):
    """A definite modem rejection; unlike a timeout, the outcome is known."""


class SerialPort(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class CurrentCall:
    identity: int
    direction: int
    state: int
    mode: int
    multiparty: int
    number: str | None = field(repr=False)


def parse_call(line: str) -> CurrentCall:
    try:
        if not line.startswith("+CLCC:"):
            raise ValueError
        values = next(csv.reader([line[6:].strip()], skipinitialspace=True, strict=True))
        if len(values) not in {5, 7, 8}:
            raise ValueError
        identity, direction, state, mode, multiparty = (int(n) for n in values[:5])
        if (
            not 1 <= identity <= 255
            or direction not in {0, 1}
            or state not in range(7)
            or mode not in {0, 1, 2, 9}
            or multiparty not in {0, 1}
        ):
            raise ValueError
        number = None
        if len(values) >= 7:
            address_type = int(values[6])
            if not 0 <= address_type <= 255:
                raise ValueError
            candidate = values[5]
            if address_type == 145 and candidate.isascii() and candidate.isdecimal():
                candidate = "+" + candidate
            if re.fullmatch(r"\+?[0-9]{3,20}", candidate):
                number = candidate
        return CurrentCall(identity, direction, state, mode, multiparty, number)
    except (ValueError, csv.Error):
        raise ModemError("Malformed current-call metadata") from None


class ATChannel:
    """One transaction owner; the supplied serial endpoint needs a bounded read timeout."""

    def __init__(self, port: SerialPort, *, clock: Callable[[], float] = time.monotonic):
        self._port, self._clock = port, clock
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._events: deque[str] = deque()
        self._bytes_read = 0
        self.closed = False
        self._claimed = False

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self.closed = True
                self._buffer.clear()
                try:
                    self._port.close()
                except Exception:
                    raise ModemError("Cannot close selected modem channel") from None

    def events(self) -> list[str]:
        with self._lock:
            result = list(self._events)
            self._events.clear()
            return result

    def _event(self, line: str) -> None:
        if len(self._events) >= 32:
            raise ModemError("Bounded modem event queue exceeded")
        self._events.append(line)

    def _line(self, deadline: float) -> str:
        while self._clock() < deadline:
            delimiter = re.search(rb"[\r\n]", self._buffer)
            if delimiter:
                raw = bytes(self._buffer[: delimiter.start()])
                del self._buffer[: delimiter.end()]
                if len(raw) > 1024:
                    raise ModemError("Modem line exceeds size limit")
                return raw.decode("ascii").strip()
            raw = self._port.read(4096)
            self._bytes_read += len(raw)
            if len(raw) > 4096 or len(self._buffer) + len(raw) > 4096 or self._bytes_read > 16384:
                raise ModemError("Modem response exceeds size limit")
            self._buffer.extend(raw)
        raise ModemError("Modem command outcome is indeterminate")

    def execute(self, command: str, *, timeout: float = 3) -> tuple[str, ...]:
        with self._lock:
            if self.closed:
                raise ModemError("Modem channel is closed")
            if command not in COMMANDS or not math.isfinite(timeout) or not 0 < timeout <= 120:
                raise ModemError("Unsupported modem command or timeout")
            try:
                data = (command + "\r").encode("ascii")
                if self._port.write(data) != len(data):
                    raise ModemError("Modem command write is incomplete")
                deadline = self._clock() + timeout
                self._bytes_read = 0
                lines: list[str] = []
                while True:
                    line = self._line(deadline)
                    if not line or line == command:
                        continue
                    if line == "OK":
                        return tuple(lines)
                    if line == "ERROR" or line.startswith(("+CME ERROR:", "+CMS ERROR:")):
                        raise CommandRejected("Modem rejected the command")
                    if line in TERMINAL:
                        self._event(line)
                        if command == "ATA":
                            raise CommandRejected("Incoming call is no longer answerable")
                    elif command == "AT+CLCC" and line.startswith("+CLCC:"):
                        lines.append(line)
                    else:
                        self._event(line)
            except CommandRejected:
                raise
            except BaseException as exc:
                try:
                    self.close()
                except Exception:
                    pass
                if not isinstance(exc, Exception):
                    raise
                raise ModemError("Modem command outcome is indeterminate; channel closed") from None


@dataclass(frozen=True)
class ObservedCall:
    """Logical call with its last known caller, distinct from optional snapshot metadata."""

    token: str
    identity: int
    state: int
    number: str | None = field(repr=False)

    def incoming(self, ingress_id: str) -> Incoming:
        # CLCC reports the caller, never the original called family number.
        return Incoming(ingress_id, self.number)


class CallControl:
    def __init__(self, channel: ATChannel):
        self.channel = channel
        self._current: ObservedCall | None = None
        self._owned = False
        with channel._lock:
            if channel._claimed:
                raise ModemError("A controller already owns this modem channel")
            channel._claimed = True
            try:
                # Avoid mixing automatic CLCC notifications with a requested snapshot.
                channel.execute("AT+CLCC=0")
            except ModemError:
                channel.close()
                raise

    def poll(self) -> ObservedCall | None:
        with self.channel._lock:
            try:
                return self._poll()
            except ModemError:
                self._current, self._owned = None, False
                raise

    def _poll(self) -> ObservedCall | None:
        calls = [parse_call(line) for line in self.channel.execute("AT+CLCC")]
        hints = self.channel.events()
        ended_hint = any(
            line in TERMINAL or (line.startswith("VOICE CALL") and "END" in line) for line in hints
        )
        if not calls:
            self._current, self._owned = None, False
            return None
        if len(calls) != 1:
            raise ModemError("Multiple calls are outside the single-channel pilot")
        call = calls[0]
        if call.direction != 1 or call.mode != 0 or call.multiparty or call.state not in {0, 4}:
            raise ModemError("Not a supported incoming voice call")
        previous = self._current
        changed_caller = bool(
            previous and previous.number and call.number and previous.number != call.number
        )
        if call.state == 0:
            if (
                previous is None
                or previous.identity != call.identity
                or not self._owned
                or changed_caller
            ):
                raise ModemError("Will not adopt an existing active call")
        elif (
            previous is None
            or previous.identity != call.identity
            or previous.state == 0
            or ended_hint
            or changed_caller
        ):
            self._current = ObservedCall(str(uuid.uuid4()), call.identity, 4, call.number)
            self._owned = False
        assert self._current is not None
        self._current = replace(
            self._current, state=call.state, number=call.number or self._current.number
        )
        return self._current

    def _check(self, token: str) -> ObservedCall:
        current = self.poll()
        if current is None or current.token != token:
            raise ModemError("Stale or ended call token")
        return current

    def answer(self, token: str) -> None:
        with self.channel._lock:
            current = self._check(token)
            if current.state != 4 or self._owned:
                raise ModemError("Call is already answered")
            self.channel.execute("ATA")
            self._owned = True

    def hangup(self, token: str) -> ObservedCall | None:
        with self.channel._lock:
            self._check(token)
            self.channel.execute("AT+CHUP")
            return self.poll()
