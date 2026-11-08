import asyncio
import multiprocessing as mp
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from call_secretary import isolated_audio as audio
from call_secretary.audio import AudioError


class Pipe:
    def __init__(self, commands=(), *, broken=False):
        self.commands = deque(commands)
        self.sent = []
        self.closed = False
        self.broken = broken

    def send_bytes(self, data):
        if self.broken:
            raise OSError("synthetic private backend error")
        self.sent.append(data)

    def recv_bytes(self, size):
        return self.commands.popleft()

    def close(self):
        self.closed = True


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
def test_worker_duplex_pcm_rate_conversion_and_interrupt(monkeypatch, direction, rate):
    import sounddevice as sd

    stream = Mock(latency=0.02)
    stream.read.return_value = (b"\x01\x00" * (rate // 50), False)
    factory = Mock(return_value=stream)
    monkeypatch.setattr(sd, "RawInputStream", factory)
    monkeypatch.setattr(sd, "RawOutputStream", factory)
    monkeypatch.setattr(
        sd,
        "query_devices",
        lambda: [{"name": "dedicated", "max_input_channels": 1, "max_output_channels": 1}],
    )
    command = b"read" if direction == "input" else b"\x02\x00" * 480
    pipe = Pipe([command, command, b"interrupt", command, b"close"])
    audio.audio_worker(pipe, direction, audio.AudioSpec("dedicated", "dedicated", rate))
    assert pipe.closed and len(pipe.sent) == 5
    assert pipe.sent[0] == b"20" and pipe.sent[3] == b"ok"
    assert stream.start.call_count == 2 and stream.close.call_count == 1
    if direction == "input":
        assert 950 <= len(pipe.sent[1]) <= 960
        assert pipe.sent[2] == b"\x01\x00" * 480
    else:
        assert all(len(c.args[0]) <= rate // 50 * 2 for c in stream.write.call_args_list)
    assert factory.call_args.kwargs["device"] == 0


@pytest.mark.parametrize("outcome", ["open", "overflow", "short", "command", "broken"])
def test_worker_errors_are_reduced_and_ipc_closes_before_native_cleanup(monkeypatch, outcome):
    import sounddevice as sd

    stream = Mock(latency=0)
    stream.read.return_value = (b"x" if outcome == "short" else b"\0" * 960, outcome == "overflow")
    monkeypatch.setattr(
        sd,
        "RawInputStream",
        Mock(side_effect=RuntimeError("private"))
        if outcome == "open"
        else Mock(return_value=stream),
    )
    pipe = Pipe([b"bad" if outcome == "command" else b"read", b"close"], broken=outcome == "broken")
    audio.audio_worker(pipe, "input", audio.AudioSpec())
    assert pipe.closed
    assert all(b"private" not in data for data in pipe.sent)
    if outcome != "broken":
        assert pipe.sent[-1] == b"error"


def test_device_selection_never_falls_back_to_default_or_ambiguous_hardware():
    sd = SimpleNamespace(query_devices=lambda: [])
    assert audio.device_index(sd, None, "input") is None
    with pytest.raises(AudioError):
        audio.device_index(sd, "missing", "input")
    sd.query_devices = lambda: [{"name": "duplicate", "max_input_channels": 1}] * 2
    with pytest.raises(AudioError):
        audio.device_index(sd, "duplicate", "input")


def fake_lane(monkeypatch, *, results=(), alive=(), start_error=False):
    parent, child = Mock(), Mock()
    parent.recv_bytes.side_effect = list(results)
    process = Mock()
    process.is_alive.side_effect = list(alive)
    if start_error:
        process.start.side_effect = OSError("private")
    context = SimpleNamespace(Pipe=lambda: (parent, child), Process=lambda **kw: process)
    monkeypatch.setattr(audio.mp, "get_context", lambda method: context)
    return parent, child, process


@pytest.mark.parametrize("stage", ["exit", "timeout", "reply", "eof", "io", "ok"])
def test_lane_failure_has_deadline_and_sanitized_error(monkeypatch, stage):
    async def scenario():
        parent, child, proc = fake_lane(
            monkeypatch, results=[b"error" if stage == "reply" else b"ok"], alive=[stage != "exit"]
        )
        lane = audio.AudioLane("input", audio.AudioSpec())
        parent.poll.return_value = stage not in {"exit", "timeout"}
        if stage in {"eof", "io"}:
            parent.recv_bytes.side_effect = EOFError if stage == "eof" else OSError
        if stage == "ok":
            assert await lane.receive(0) == b"ok"
        else:
            with pytest.raises(AudioError) as err:
                await lane.receive(0)
            assert "private" not in str(err.value)
        child.close.assert_called_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "alive", [[False, False], [True, False, False], [True, True, False], [True, True, True]]
)
def test_stop_escalates_native_hang_from_terminate_to_kill(monkeypatch, alive):
    parent, _, process = fake_lane(monkeypatch, alive=alive)
    lane = audio.AudioLane("output", audio.AudioSpec())
    if alive[-1]:
        with pytest.raises(AudioError, match="reaped"):
            lane.stop()
    else:
        lane.stop()
        process.close.assert_called_once()
    lane.stop()
    assert parent.close.call_count == 1
    assert process.kill.call_count == int(alive[0] and alive[1])


def test_lane_start_failure_and_cancelled_request_close_owned_resources(monkeypatch):
    parent, child, _ = fake_lane(monkeypatch, start_error=True)
    with pytest.raises(AudioError, match="start"):
        audio.AudioLane("input", audio.AudioSpec())
    parent.close.assert_called_once()
    child.close.assert_called_once()

    async def scenario():
        parent, _, _ = fake_lane(monkeypatch, alive=[False, False])
        lane = audio.AudioLane("input", audio.AudioSpec())
        parent.send_bytes.side_effect = OSError("private")
        with pytest.raises(OSError):
            await lane.exchange(b"read")
        assert lane.closed
        with pytest.raises(AudioError, match="closed"):
            await lane.exchange(b"read")

    asyncio.run(scenario())


def sleeping_native_worker(pipe, direction, spec):
    pipe.send_bytes(b"0")
    pipe.recv_bytes()
    time.sleep(120)  # Real child blocks; parent must kill it, not wait for the thread.


def test_real_spawned_audio_hang_is_killed_without_a_device(monkeypatch):
    context = mp.get_context("spawn")
    fake = SimpleNamespace(
        Pipe=context.Pipe,
        Process=lambda **kw: context.Process(
            target=sleeping_native_worker, args=kw["args"], daemon=True
        ),
    )
    monkeypatch.setattr(audio.mp, "get_context", lambda method: fake)

    async def scenario():
        lane = audio.AudioLane("input", audio.AudioSpec())
        try:
            assert await lane.receive(10) == b"0"
            pid = lane.process.pid
            started = time.monotonic()
            with pytest.raises(AudioError, match="timed out"):
                await lane.exchange(b"read")
            assert time.monotonic() - started < 5
            assert lane.closed and all(p.pid != pid for p in mp.active_children())
        finally:
            lane.stop()

    asyncio.run(scenario())


def test_endpoint_open_frames_interrupt_and_cleanup_all_lanes(monkeypatch):
    made = []

    class Lane:
        def __init__(self, direction, spec):
            self.direction = direction
            made.append(self)

        async def receive(self, seconds):
            return b"10"

        async def exchange(self, data):
            self.last = data
            return b"\0\0"

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(audio, "AudioLane", Lane)

    async def scenario():
        with pytest.raises(AudioError):
            await audio.IsolatedAudio.open(audio.AudioSpec(rate=1))
        endpoint = await audio.IsolatedAudio.open()
        assert endpoint.latency_ms == 10 and await endpoint.read() == b"\0\0"
        await endpoint.write(b"\0\0")
        for bad in (b"", b"x", b"x" * 962):
            with pytest.raises(AudioError):
                await endpoint.write(bad)
        await endpoint.interrupt()
        assert made[1].last == b"interrupt"
        await endpoint.close()
        assert all(lane.stopped for lane in made)
        made[0].stop = Mock(side_effect=RuntimeError())
        with pytest.raises(AudioError, match="stopped"):
            await endpoint.close()
        made.clear()
        Lane.receive = Mock(side_effect=AudioError("open failure"))
        with pytest.raises(AudioError):
            await audio.IsolatedAudio.open()
        assert made[0].stopped

    asyncio.run(scenario())
