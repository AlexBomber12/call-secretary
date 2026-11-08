"""Explicit-device diagnostics. Listing devices never opens a modem."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class ModemError(RuntimeError):
    pass


class SerialIO(Protocol):
    def write(self, data: bytes) -> int: ...
    def readline(self) -> bytes: ...
    def close(self) -> None: ...


READ_ONLY = {"AT", "AT+CGMM", "AT+CGMR", "AT+CREG?", "AT+CEREG?", "AT+CPIN?", "AT+CSQ"}


def at_query(
    port: SerialIO,
    command: str,
    *,
    timeout: float = 3,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    if command not in READ_ONLY:
        raise ModemError("Only allowlisted read-only AT queries are accepted")
    port.write((command + "\r").encode("ascii"))
    deadline = clock() + timeout
    lines: list[str] = []
    size = 0
    while clock() < deadline:
        raw = port.readline()
        size += len(raw)
        if size > 16384:
            raise ModemError("Modem response exceeds size limit")
        text = raw.decode("ascii", errors="replace").strip()
        if text == "OK":
            return lines
        if text == "ERROR" or text.startswith("+CME ERROR"):
            raise ModemError("Modem rejected diagnostic query")
        if text and text != command:
            lines.append(text)
    raise ModemError("Modem diagnostic timed out")


def inventory() -> list[dict[str, Any]]:
    from serial.tools import list_ports

    return [
        {"device": p.device, "vid": p.vid, "pid": p.pid, "description": p.description}
        for p in list_ports.comports()
    ]


def check_device(device: str) -> None:
    if os.name == "nt":
        if not device.upper().startswith("COM"):
            raise ModemError("Select an explicit COM port")
        return
    path = Path(device)
    if not str(path).startswith("/dev/serial/by-id/") or not path.is_symlink():
        raise ModemError("Select a stable /dev/serial/by-id device")
    if not path.resolve().is_char_device():
        raise ModemError("Selected device is not a character device")
    fuser = shutil.which("fuser")
    if not fuser:
        raise ModemError("Install psmisc to check port ownership before diagnostics")
    result = subprocess.run([fuser, "-s", str(path.resolve())], capture_output=True, timeout=5)
    if result.returncode == 0:
        raise ModemError("Selected port is already in use")
    if result.returncode != 1:
        raise ModemError("Cannot establish port ownership")


def open_serial(device: str, *, baudrate: int = 115200) -> Any:
    import serial

    check_device(device)
    options: dict[str, Any] = {"baudrate": baudrate, "timeout": 0.1, "write_timeout": 1}
    if os.name != "nt":
        options["exclusive"] = True
    try:
        return serial.Serial(device, **options)
    except (serial.SerialException, OSError):
        raise ModemError("Cannot open selected serial device exclusively") from None


def inspect(device: str) -> dict[str, Any]:
    port = open_serial(device)
    try:
        model = at_query(port, "AT+CGMM")
        firmware = at_query(port, "AT+CGMR")
        registration = at_query(port, "AT+CEREG?")
        return {
            "model": model,
            "firmware": firmware,
            "registration": registration,
            "H1": "not_tested",
            "H2": "not_tested",
        }
    finally:
        port.close()


def hardware_checklist() -> dict[str, Any]:
    return {
        "H1": {
            "incoming_answer_hangup": "not_tested",
            "usb_capture": "not_tested",
            "usb_playback": "not_tested",
            "simultaneous_audio": "not_tested",
            "reconnect": "not_tested",
        },
        "H2": {
            "each_family_busy": "not_tested",
            "each_family_no_answer": "not_tested",
            "same_caller_multiple_recipients": "not_tested",
            "hidden_caller": "not_tested",
            "original_recipient_metadata": "not_tested",
        },
    }
