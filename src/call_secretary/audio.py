"""Bounded in-memory PCM16 audio. No recording or audio payload logging."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Protocol

RATE = 24000
FRAME_BYTES = 960  # 20 ms, mono signed PCM16 little-endian.


class AudioError(RuntimeError):
    pass


class AudioEndpoint(Protocol):
    latency_ms: int

    async def read(self) -> bytes: ...
    async def write(self, pcm: bytes) -> None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


class Microphone:
    latency_ms: int

    def __init__(self, input_device: int | None = None, output_device: int | None = None):
        import sounddevice as sd

        self._lock = threading.RLock()
        self._input: Any = None
        self._output: Any = None
        try:
            self._input = sd.RawInputStream(
                samplerate=RATE, channels=1, dtype="int16", device=input_device, blocksize=480
            )
            self._output = sd.RawOutputStream(
                samplerate=RATE, channels=1, dtype="int16", device=output_device, blocksize=480
            )
            self._input.start()
            self._output.start()
            self.latency_ms = int(float(self._output.latency) * 1000)
        except Exception:
            for stream in (self._output, self._input):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            raise AudioError("Cannot open microphone and speaker") from None

    async def read(self) -> bytes:
        data, overflow = await asyncio.to_thread(self._input.read, 480)
        if overflow:
            raise AudioError("Microphone buffer overflow")
        return bytes(data)

    async def write(self, pcm: bytes) -> None:
        def write() -> None:
            with self._lock:
                self._output.write(pcm)

        await asyncio.to_thread(write)

    async def interrupt(self) -> None:
        def abort() -> None:
            with self._lock:
                self._output.abort()
                self._output.start()

        await asyncio.to_thread(abort)

    async def close(self) -> None:
        def close() -> None:
            failed = False
            with self._lock:
                for stream in (self._input, self._output):
                    for operation in (stream.abort, stream.close):
                        try:
                            operation()
                        except Exception:
                            failed = True
            if failed:
                raise AudioError("Audio device cleanup failed")

        await asyncio.to_thread(close)


@dataclass(frozen=True)
class AudioChunk:
    item_id: str
    pcm: bytes
    generation: int


class Playback:
    def __init__(self, endpoint: AudioEndpoint, max_bytes: int = 1_048_576):
        self.endpoint = endpoint
        self.queue: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self.max_bytes = max_bytes
        self.queued_bytes = 0
        self.generation = 0
        self.current_item: str | None = None
        self.written: dict[str, int] = {}
        self.pending_bytes: dict[str, int] = {}

    def enqueue(self, item_id: str, pcm: bytes) -> None:
        if len(pcm) % 2 or len(pcm) > self.max_bytes - self.queued_bytes:
            raise AudioError("Invalid audio frame or bounded playback buffer exceeded")
        self.queued_bytes += len(pcm)
        self.pending_bytes[item_id] = self.pending_bytes.get(item_id, 0) + len(pcm)
        self.queue.put_nowait(AudioChunk(item_id, pcm, self.generation))

    async def run(self) -> None:
        while True:
            chunk = await self.queue.get()
            try:
                for offset in range(0, len(chunk.pcm), FRAME_BYTES):
                    part = chunk.pcm[offset : offset + FRAME_BYTES]
                    if chunk.generation == self.generation:
                        self.current_item = chunk.item_id
                        await self.endpoint.write(part)
                        # A concurrent interruption invalidates the just-completed write.
                        if chunk.generation == self.generation:
                            self.written[chunk.item_id] = self.written.get(chunk.item_id, 0) + len(
                                part
                            )
                    self.queued_bytes -= len(part)
                    self.pending_bytes[chunk.item_id] -= len(part)
            finally:
                self.queue.task_done()

    async def interrupt(self) -> list[tuple[str, int]]:
        items = {iid for iid, count in self.pending_bytes.items() if count > 0}
        if self.current_item:
            items.add(self.current_item)
        result = [
            (iid, max(0, self.written.get(iid, 0) * 1000 // (RATE * 2) - self.endpoint.latency_ms))
            for iid in items
        ]
        self.generation += 1
        while not self.queue.empty():
            chunk = self.queue.get_nowait()
            self.queued_bytes -= len(chunk.pcm)
            self.pending_bytes[chunk.item_id] -= len(chunk.pcm)
            self.queue.task_done()
        await self.endpoint.interrupt()
        self.current_item = None
        return result


def devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())
