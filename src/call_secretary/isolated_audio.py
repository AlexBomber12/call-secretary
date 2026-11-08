"""Killable native audio workers. PCM crosses bounded, private in-memory pipes only."""

from __future__ import annotations

import asyncio
import audioop
import multiprocessing as mp
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from call_secretary.audio import RATE, AudioError


@dataclass(frozen=True)
class AudioSpec:
    input_name: str | None = None
    output_name: str | None = None
    rate: int = RATE


DEFAULT_SPEC = AudioSpec()


def device_index(sd: Any, name: str | None, direction: str) -> int | None:
    if name is None:
        return None  # Microphone prototype only; phone configuration requires names.
    matches = [
        i
        for i, d in enumerate(sd.query_devices())
        if d["name"] == name and d["max_" + direction + "_channels"] >= 1
    ]
    if len(matches) != 1:
        raise AudioError("Audio device name must identify exactly one usable endpoint")
    return matches[0]


def audio_worker(pipe: Connection, direction: str, spec: AudioSpec) -> None:
    """One native stream per process; even open/abort/close may be killed by the parent."""
    stream: Any = None
    try:
        import sounddevice as sd

        factory = sd.RawInputStream if direction == "input" else sd.RawOutputStream
        name = spec.input_name if direction == "input" else spec.output_name
        stream = factory(
            samplerate=spec.rate,
            channels=1,
            dtype="int16",
            device=device_index(sd, name, direction),
            blocksize=spec.rate // 50,
        )
        pipe.send_bytes(str(min(2000, max(0, int(stream.latency * 1000)))).encode("ascii"))
        state: Any = None
        started = False
        while True:
            command = pipe.recv_bytes(1024)
            if command == b"close":
                return
            if command == b"interrupt":
                stream.abort()
                started, state = False, None
                pipe.send_bytes(b"ok")
                continue
            if not started:
                stream.start()
                started = True
            if direction == "input" and command == b"read":
                data, overflow = stream.read(spec.rate // 50)
                if overflow:
                    raise AudioError("Capture overflow")
                pcm = bytes(data)
                if len(pcm) != spec.rate // 50 * 2:
                    raise AudioError("Unexpected capture frame")
                pcm, state = audioop.ratecv(pcm, 2, 1, spec.rate, RATE, state)
                pipe.send_bytes(pcm)
            elif direction == "output" and len(command) <= 960 and len(command) % 2 == 0:
                pcm, state = audioop.ratecv(command, 2, 1, RATE, spec.rate, state)
                stream.write(pcm)
                pipe.send_bytes(b"ok")
            else:
                raise AudioError("Invalid audio worker command")
    except (Exception, KeyboardInterrupt):
        try:
            pipe.send_bytes(b"error")
        except OSError:
            pass
    finally:
        # Close IPC before native cleanup, which may hang indefinitely.
        pipe.close()
        if stream is not None:
            stream.close()


class AudioLane:
    def __init__(self, direction: str, spec: AudioSpec):
        context = mp.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(
            target=audio_worker, args=(child, direction, spec), daemon=True
        )
        self.lock = asyncio.Lock()
        self.closed = False
        self.unreaped = False
        try:
            self.process.start()
        except Exception:
            self.pipe.close()
            raise AudioError("Cannot start isolated audio worker") from None
        finally:
            child.close()

    async def receive(self, seconds: float) -> bytes:
        deadline = time.monotonic() + seconds
        try:
            while not self.pipe.poll():
                if not self.process.is_alive() or time.monotonic() >= deadline:
                    raise AudioError("Audio worker unavailable or timed out")
                await asyncio.sleep(0.005)
            result = self.pipe.recv_bytes(4096)
            if result == b"error":
                raise AudioError("Isolated audio operation failed")
            return result
        except (OSError, EOFError):
            raise AudioError("Audio worker disconnected") from None

    async def exchange(self, data: bytes) -> bytes:
        async with self.lock:
            if self.closed:
                raise AudioError("Audio worker closed")
            try:
                # One request per lane, <=960 bytes; never a queued audio stream.
                self.pipe.send_bytes(data)
                return await self.receive(2)
            except BaseException:
                self.stop()
                raise

    def stop(self) -> None:
        if self.unreaped:
            raise AudioError("Audio worker remains unreaped; supervisor must restart service")
        if not self.closed:
            self.closed = True
            self.unreaped = True
            self.pipe.close()
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(0.3)
                if self.process.is_alive():
                    self.process.kill()
            self.process.join(0.3)
            if self.process.is_alive():
                raise AudioError(
                    "Audio process could not be reaped; supervisor must restart service"
                )
            self.process.close()
            self.unreaped = False


class IsolatedAudio:
    def __init__(self) -> None:
        self.lanes: list[AudioLane] = []
        self.latency_ms = 0

    @classmethod
    async def open(cls, spec: AudioSpec = DEFAULT_SPEC) -> IsolatedAudio:
        if spec.rate not in {8000, 16000, 24000, 48000}:
            raise AudioError("Unsupported mono PCM16 sample rate")
        self = cls()
        try:
            for direction in ("input", "output"):
                lane = AudioLane(direction, spec)
                self.lanes.append(lane)
                reply = await lane.receive(8)
                self.latency_ms = int(reply)
            return self
        except BaseException:
            await self.close()
            raise

    async def read(self) -> bytes:
        return await self.lanes[0].exchange(b"read")

    async def write(self, pcm: bytes) -> None:
        if not pcm or len(pcm) > 960 or len(pcm) % 2:
            raise AudioError("Invalid PCM16 output frame")
        await self.lanes[1].exchange(pcm)

    async def interrupt(self) -> None:
        await self.lanes[1].exchange(b"interrupt")

    async def close(self) -> None:
        errors = []
        for lane in self.lanes:
            try:
                lane.stop()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise AudioError("Audio workers could not be stopped")
