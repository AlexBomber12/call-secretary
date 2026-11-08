import asyncio
import sys
from types import SimpleNamespace

import pytest
from test_voice import Endpoint

from call_secretary.audio import AudioChunk, AudioError, Microphone, Playback, devices


class Stream:
    latency = 0.04

    def __init__(self):
        self.writes, self.closed, self.aborted, self.starts = [], 0, 0, 0
        self.overflow = False

    def start(self):
        self.starts += 1

    def read(self, count):
        assert count == 480
        return b"\0" * 960, self.overflow

    def write(self, pcm):
        self.writes.append(pcm)

    def close(self):
        self.closed += 1

    def abort(self):
        self.aborted += 1


def test_microphone_pcm_io_overflow_and_lifetime(monkeypatch):
    inp, out = Stream(), Stream()
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            RawInputStream=lambda **kw: inp,
            RawOutputStream=lambda **kw: out,
            query_devices=lambda: "synthetic devices",
        ),
    )

    async def scenario():
        mic = Microphone()
        assert mic.latency_ms == 40
        assert len(await mic.read()) == 960
        inp.overflow = True
        with pytest.raises(AudioError, match="overflow"):
            await mic.read()
        await mic.write(b"\0\0")
        await mic.interrupt()
        await mic.close()
        assert out.writes == [b"\0\0"]
        assert out.aborted == 2 and inp.closed == out.closed == 1
        assert devices() == "synthetic devices"

    asyncio.run(scenario())


def test_open_failure_is_sanitized_even_if_partial_cleanup_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private backend details")

    for fail_input in (True, False):
        monkeypatch.setitem(
            sys.modules,
            "sounddevice",
            SimpleNamespace(
                RawInputStream=fail if fail_input else lambda **kw: SimpleNamespace(close=fail),
                RawOutputStream=fail,
            ),
        )
        with pytest.raises(AudioError, match="Cannot open") as err:
            Microphone()
        assert "private" not in str(err.value)


def test_playback_frame_accounting_and_inflight_cancellation():
    async def scenario():
        endpoint = Endpoint()
        playback = Playback(endpoint)
        playback.enqueue("a", b"\0" * 1920)
        task = asyncio.create_task(playback.run())
        await playback.queue.join()
        assert playback.written["a"] == 1920
        assert playback.pending_bytes["a"] == 0
        assert await playback.interrupt() == [("a", 40)]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(AudioError):
            playback.enqueue("a", b"x")

        class InterruptDuringWrite(Endpoint):
            async def write(self, pcm):
                await playback.interrupt()

        playback = Playback(InterruptDuringWrite())
        playback.enqueue("b", b"\0" * 1920)
        task = asyncio.create_task(playback.run())
        await playback.queue.join()
        assert playback.written == {} and playback.queued_bytes == 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # A stale chunk already removed by a consumer cannot resume playback.
        playback.queue.put_nowait(AudioChunk("stale", b"\0\0", 0))
        playback.pending_bytes["stale"] = 2
        playback.queued_bytes = 2
        task = asyncio.create_task(playback.run())
        await playback.queue.join()
        assert playback.written == {}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
