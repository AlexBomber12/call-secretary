"""Exclusive SIM7600 USB PCM transport, with audio kept in bounded memory."""

from __future__ import annotations

import asyncio
import audioop
import os
import re
import subprocess
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from call_secretary.audio import FRAME_BYTES, RATE, AudioError, FatalAudioError
from call_secretary.modem import ModemError, open_serial
from call_secretary.sim7600 import SerialPort

MODEM_RATE = 8000
MODEM_FRAME_BYTES = 320
FRAME_SECONDS = 0.02
QUEUE_FRAMES = 3


def probe_usb(device: str) -> str:
    path = Path(device)
    if (
        str(path.parent) not in {"/dev/serial/by-id", "/dev/serial/by-path"}
        or not path.is_symlink()
    ):
        raise ModemError("Select an explicit stable USB serial device")
    try:
        return subprocess.run(
            ["udevadm", "info", "--query=property", "--name", device],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ModemError("Cannot verify USB modem interface") from None


def usb_parent(device: str, interface: str) -> str:
    properties = dict(line.split("=", 1) for line in probe_usb(device).splitlines() if "=" in line)
    path = re.fullmatch(
        r"(.+-usb-.+):[0-9]+\.[0-9]+(?:-port[0-9]+)?", properties.get("ID_PATH", "")
    )
    if (
        properties.get("ID_VENDOR_ID"),
        properties.get("ID_MODEL_ID"),
        properties.get("ID_USB_INTERFACE_NUM"),
    ) != ("1e0e", "9001", interface) or path is None:
        raise ModemError("Wrong SIM7600 USB interface")
    return path[1]


@dataclass(frozen=True)
class VoicePorts:
    control: str
    audio: str

    def validate(self) -> str:
        control = usb_parent(self.control, "02")
        audio = usb_parent(self.audio, "04")
        if control != audio:
            raise ModemError("Control and PCM must be on the same physical SIM7600")
        return control

    def open(self, *, audio: bool = False) -> SerialPort:
        parent = self.validate()
        device = self.audio if audio else self.control
        port = open_serial(device, timeout=FRAME_SECONDS if audio else 0.1)
        try:
            if not os.path.samestat(os.fstat(port.fileno()), os.stat(device)):
                raise ModemError("USB device changed during open")
            if self.validate() != parent:
                raise ModemError("USB device changed during open")
        except (OSError, ModemError):
            with suppress(OSError):
                port.close()
            raise ModemError("Cannot establish the verified USB port") from None
        return cast(SerialPort, port)


class SerialPcm:
    latency_ms = (QUEUE_FRAMES + 1) * 20

    def __init__(self, port: SerialPort):
        self._port = port
        self._playback = bytearray()
        self._capture: deque[bytes] = deque(maxlen=QUEUE_FRAMES)
        self._changed = asyncio.Event()
        self._stop = asyncio.Event()
        self._writing = False
        self._output_state: Any = None
        self._closing: asyncio.Task[None] | None = None
        self._task = asyncio.create_task(self._run())

    def _exchange(self, frame: bytes, read_size: int) -> bytes:
        try:
            if self._port.write(frame) != MODEM_FRAME_BYTES:
                raise AudioError("Incomplete SIM7600 PCM write")
            result = self._port.read(read_size)
            if len(result) > read_size:
                raise AudioError("Invalid SIM7600 PCM read size")
            return result
        except OSError:
            raise AudioError("SIM7600 audio transport disconnected") from None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        pending = bytearray()
        input_state: Any = None
        try:
            while not self._stop.is_set():
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    if self._stop.is_set():
                        break
                playback = bytes(self._playback[:FRAME_BYTES])
                del self._playback[:FRAME_BYTES]
                self._writing = bool(playback)
                frame, self._output_state = audioop.ratecv(
                    playback.ljust(FRAME_BYTES, b"\0"), 2, 1, RATE, MODEM_RATE, self._output_state
                )
                pending.extend(
                    await asyncio.to_thread(self._exchange, frame, MODEM_FRAME_BYTES - len(pending))
                )
                self._writing = False
                if len(pending) == MODEM_FRAME_BYTES:
                    # Seed the resampler with the first sample so the initial frame
                    # has the same duration as every subsequent frame.
                    first = input_state is None
                    source = pending[:2] + pending if first else pending
                    converted, input_state = audioop.ratecv(
                        source, 2, 1, MODEM_RATE, RATE, input_state
                    )
                    self._capture.append(converted[2:] if first else converted)
                    pending.clear()
                self._changed.set()
                deadline = max(deadline + FRAME_SECONDS, loop.time())
        finally:
            self._changed.set()

    def _check(self) -> None:
        if self._task.done():
            self._task.result()
        if self._stop.is_set():
            raise AudioError("SIM7600 audio is closed")

    async def _wait(self) -> None:
        self._check()
        self._changed.clear()
        await self._changed.wait()

    async def read(self) -> bytes:
        while not self._capture:
            await self._wait()
        self._check()
        return self._capture.popleft()

    async def write(self, pcm: bytes) -> None:
        if not 0 < len(pcm) <= FRAME_BYTES or len(pcm) % 2:
            raise AudioError("Invalid PCM16 playback frame")
        while len(self._playback) + len(pcm) > QUEUE_FRAMES * FRAME_BYTES:
            await self._wait()
        self._check()
        self._playback.extend(pcm)

    async def interrupt(self) -> None:
        self._playback.clear()
        self._output_state = None
        self._changed.set()

    async def drain(self) -> None:
        while self._playback or self._writing:
            await self._wait()
        self._check()

    async def _close(self) -> None:
        self._stop.set()
        try:
            with suppress(AudioError):
                await asyncio.wait_for(asyncio.shield(self._task), 1)
            await asyncio.wait_for(asyncio.to_thread(self._port.close), 1)
        except (OSError, TimeoutError):
            raise FatalAudioError("Cannot safely stop SIM7600 audio") from None
        finally:
            self._playback.clear()
            self._capture.clear()
            self._changed.set()

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)


async def open_pcm_audio(ports: VoicePorts) -> SerialPcm:
    opening = asyncio.create_task(asyncio.to_thread(ports.open, audio=True))
    try:
        port = await asyncio.shield(opening)
    except asyncio.CancelledError:
        try:
            port = await opening
        except (ModemError, OSError):
            raise asyncio.CancelledError from None
        try:
            await asyncio.wait_for(asyncio.to_thread(port.close), 1)
        except (OSError, TimeoutError):
            raise FatalAudioError("Cannot close cancelled SIM7600 audio open") from None
        raise
    return SerialPcm(port)
