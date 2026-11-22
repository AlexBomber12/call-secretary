import asyncio
import struct
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_sim7600 import Port, snapshot

from call_secretary import cli, phone, serial_pcm
from call_secretary.audio import FRAME_BYTES, AudioError, FatalAudioError
from call_secretary.modem import ModemError
from call_secretary.serial_pcm import MODEM_FRAME_BYTES, SerialPcm, VoicePorts, open_pcm_audio
from call_secretary.sim7600 import ATChannel, CallControl


class AudioPort:
    def __init__(self, *, bad=None):
        self.writes = []
        self.read_sizes = []
        self.closed = False
        self.bad = bad

    @property
    def in_waiting(self):
        return 20 * MODEM_FRAME_BYTES if self.bad == "backlog" else 0

    def write(self, data):
        self.writes.append(data)
        if self.bad == "write":
            return 0
        return len(data)

    def read(self, size):
        self.read_sizes.append(size)
        if self.bad == "read":
            raise OSError("private USB detail")
        if self.bad == "oversize":
            return bytes(size + 1)
        return b"\x00\x01" * (size // 2)

    def close(self):
        self.closed = True
        if self.bad == "close":
            raise OSError("private close detail")


def test_exchange_is_paced_and_uses_silence_and_exact_frames():
    async def scenario():
        port = AudioPort()
        endpoint = SerialPcm(port)
        try:
            frame = await asyncio.wait_for(endpoint.read(), 1)
            assert len(frame) == FRAME_BYTES
            assert struct.unpack_from("<h", frame)[0] == 256
            assert port.writes[0] == bytes(MODEM_FRAME_BYTES)
            pcm = struct.pack("<h", 1024) * (FRAME_BYTES // 2)
            await endpoint.write(pcm)
            await asyncio.wait_for(endpoint.read(), 1)
            assert any(frame != bytes(MODEM_FRAME_BYTES) for frame in port.writes)
            assert all(len(frame) == MODEM_FRAME_BYTES for frame in port.writes)
            await endpoint.interrupt()
            await asyncio.wait_for(endpoint.read(), 1)
            assert port.writes[-1] == bytes(MODEM_FRAME_BYTES)
        finally:
            await endpoint.close()
            await endpoint.close()
        assert port.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["write", "read", "oversize", "backlog"])
def test_transport_failure_surfaces_and_closes_port(failure):
    async def scenario():
        port = AudioPort(bad=failure)
        endpoint = SerialPcm(port)
        with pytest.raises(AudioError) as error:
            await asyncio.wait_for(endpoint.read(), 1)
        assert "private" not in str(error.value)
        await endpoint.close()
        assert port.closed

    asyncio.run(scenario())


def test_usb_pair_accepts_only_same_physical_sim7600_interfaces(monkeypatch):
    base = "pci-0000:00:14.0-usb-0:5"
    control = "/dev/serial/by-path/synthetic-control"
    audio = "/dev/serial/by-path/synthetic-audio"
    vendor = "ID_VENDOR_ID=1e0e\nID_MODEL_ID=9001\n"
    snapshots = {
        control: vendor + f"ID_USB_INTERFACE_NUM=02\nID_PATH={base}:1.2\n",
        audio: vendor + f"ID_USB_INTERFACE_NUM=04\nID_PATH={base}:1.4\n",
    }
    monkeypatch.setattr(serial_pcm, "probe_usb", lambda path: snapshots[path])
    ports = VoicePorts(control, audio)
    assert ports.validate() == base
    snapshots[audio] = snapshots[audio].replace("usb-0:5", "usb-0:6")
    with pytest.raises(ModemError, match="same"):
        ports.validate()
    snapshots[audio] = snapshots[audio].replace("1e0e", "2c7c")
    with pytest.raises(ModemError, match="interface"):
        ports.validate()


@pytest.mark.parametrize("symlink, outcome", [(True, "ok"), (True, "failure"), (False, "failure")])
def test_usb_probe_is_bounded_and_requires_stable_symlink(monkeypatch, symlink, outcome):
    path = SimpleNamespace(parent="/dev/serial/by-id", is_symlink=lambda: symlink)
    monkeypatch.setattr(serial_pcm, "Path", lambda device: path)
    run = Mock(return_value=SimpleNamespace(stdout="ID_PATH=synthetic"))
    monkeypatch.setattr(serial_pcm.subprocess, "run", run)
    if outcome == "failure":
        run.side_effect = subprocess.TimeoutExpired("private", 2)
        with pytest.raises(ModemError):
            serial_pcm.probe_usb("synthetic")
    else:
        assert serial_pcm.probe_usb("synthetic") == "ID_PATH=synthetic"
        assert run.call_args.kwargs["timeout"] == 2
    assert run.call_count == int(symlink)


@pytest.mark.parametrize("problem", [None, "changed_fd", "changed_parent", "stat_error"])
@pytest.mark.parametrize("audio", [False, True])
def test_verified_port_open_rechecks_identity_and_closes_on_change(monkeypatch, problem, audio):
    ports = VoicePorts("control", "audio")
    validate = Mock(side_effect=["usb-1", "usb-2" if problem == "changed_parent" else "usb-1"])
    monkeypatch.setattr(VoicePorts, "validate", validate)
    port = Mock()
    channel = Mock() if audio else None
    unopened = Mock(return_value=port)
    monkeypatch.setattr(serial_pcm, "open_serial", unopened)
    stat = Mock(
        side_effect=lambda fd: "same AT" if audio and fd == channel.fileno.return_value else "PCM"
    )
    lookup = Mock(side_effect=lambda path: "same AT" if path == "control" and audio else "PCM")
    same = Mock(side_effect=lambda a, b: a == b and (a == "same AT" or problem != "changed_fd"))
    if problem == "stat_error":

        def missing(fd):
            if fd == port.fileno.return_value:
                raise OSError("private")
            return "same AT"

        stat.side_effect = missing
    monkeypatch.setattr(
        serial_pcm,
        "os",
        SimpleNamespace(
            fstat=stat,
            stat=lookup,
            path=SimpleNamespace(samestat=same),
        ),
    )
    if problem:
        with pytest.raises(ModemError, match="verified USB"):
            ports.open(control=channel)
        port.close.assert_called_once()
    else:
        assert ports.open(control=channel) is port
        port.close.assert_not_called()
    unopened.assert_called_once_with(
        "audio" if audio else "control", timeout=0.02 if audio else 0.1
    )


@pytest.mark.parametrize("swap", ["already_replaced", "during_open"])
def test_pcm_does_not_open_on_a_replacement_for_the_live_at_port(monkeypatch, swap):
    ports = VoicePorts("control", "audio")
    monkeypatch.setattr(VoicePorts, "validate", lambda self: "same physical path")
    port, at_port = Mock(), Mock()
    at_port.fileno.return_value = 12
    channel = ATChannel(at_port)
    port.fileno.return_value = 14
    opened = Mock(return_value=port)
    monkeypatch.setattr(serial_pcm, "open_serial", opened)

    def stat(path):
        if path == "audio":
            return "pcm"
        if swap == "already_replaced" or opened.called:
            return "new control"
        return "old control"

    monkeypatch.setattr(
        serial_pcm,
        "os",
        SimpleNamespace(
            fstat={12: "old control", 14: "pcm"}.__getitem__,
            stat=stat,
            path=SimpleNamespace(samestat=lambda a, b: a == b),
        ),
    )
    with pytest.raises(ModemError, match="verified USB"):
        ports.open(control=channel)
    assert opened.called == (swap == "during_open")
    assert port.close.called == (swap == "during_open")


def test_partial_capture_and_backpressure_remain_bounded():
    class ShortPort(AudioPort):
        waiting = 0

        @property
        def in_waiting(self):
            return self.waiting

        def write(self, data):
            self.waiting += MODEM_FRAME_BYTES
            return super().write(data)

        def read(self, size):
            result = super().read(min(size, self.waiting, 40) if not self.read_sizes else size)
            self.waiting -= len(result)
            return result

    async def scenario():
        port = ShortPort()
        endpoint = SerialPcm(port)
        try:
            frame = await asyncio.wait_for(endpoint.read(), 1)
            assert len(frame) == FRAME_BYTES and port.read_sizes[:2] == [40, 600]
            assert port.waiting == 0
            assert len(await endpoint.read()) == FRAME_BYTES
            for invalid in (b"", b"x", bytes(FRAME_BYTES + 2)):
                with pytest.raises(AudioError, match="PCM16"):
                    await endpoint.write(invalid)
            await asyncio.wait_for(
                asyncio.gather(*(endpoint.write(bytes(FRAME_BYTES)) for _ in range(6))), 1
            )
            await asyncio.wait_for(endpoint.drain(), 1)
            assert len(endpoint._capture) <= serial_pcm.QUEUE_FRAMES
        finally:
            await endpoint.close()
        with pytest.raises(AudioError, match="closed"):
            await endpoint.write(bytes(FRAME_BYTES))

    asyncio.run(scenario())


def test_close_does_not_write_another_frame_after_pacing_sleep(monkeypatch):
    monkeypatch.setattr(serial_pcm, "FRAME_SECONDS", 0.2)

    async def scenario():
        port = AudioPort()
        endpoint = SerialPcm(port)
        await asyncio.wait_for(endpoint.read(), 1)
        await endpoint.close()
        assert len(port.writes) == 1

    asyncio.run(scenario())


def test_cleanup_failure_is_fatal_and_not_reported_as_success():
    async def scenario():
        endpoint = SerialPcm(AudioPort(bad="close"))
        with pytest.raises(FatalAudioError, match="stop"):
            await endpoint.close()

    asyncio.run(scenario())


def test_open_audio_returns_ready_serial_pcm_transport(monkeypatch):
    async def scenario():
        port = AudioPort()
        opened = Mock(return_value=port)
        monkeypatch.setattr(VoicePorts, "open", opened)
        channel = Mock()
        endpoint = await open_pcm_audio(VoicePorts("control", "audio"), channel)
        try:
            assert len(await asyncio.wait_for(endpoint.read(), 1)) == FRAME_BYTES
        finally:
            await endpoint.close()
        opened.assert_called_once_with(control=channel)
        assert port.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["ok", "open_error", "close_error"])
def test_cancelling_open_closes_the_eventual_port(monkeypatch, outcome):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        port = AudioPort(bad="close" if outcome == "close_error" else None)

        channel = Mock()

        def open_port(self, *, control):
            assert control is channel
            entered.set()
            assert release.wait(1)
            if outcome == "open_error":
                raise ModemError("disconnected during open")
            return port

        monkeypatch.setattr(VoicePorts, "open", open_port)
        task = asyncio.create_task(open_pcm_audio(VoicePorts("control", "audio"), channel))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(FatalAudioError if outcome == "close_error" else asyncio.CancelledError):
            await task
        assert port.closed == (outcome != "open_error")

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["ok", "not_active", "wrong_rate", "no_confirmation"])
def test_pcm_control_requires_owned_active_call_format_and_confirmation(monkeypatch, outcome):
    monkeypatch.setattr("call_secretary.sim7600.time.sleep", lambda seconds: None)
    alive = [snapshot(0), b"OK\r\n"]
    ring = [snapshot(), b"OK\r\n"]
    responses = [[b"OK\r\n"], ring, ring, [b"OK\r\n"]]
    responses.append(ring if outcome == "not_active" else alive)
    if outcome in {"ok", "wrong_rate", "no_confirmation"}:
        mode = 1 if outcome == "wrong_rate" else 0
        responses.append([f"+CPCMFRM: {mode}\r\nOK\r\n".encode()])
    if outcome in {"ok", "no_confirmation"}:
        responses.append([b"OK\r\n"])
        responses.extend([[b"+CPCMREG:\r\nOK\r\n"]] * (6 if outcome == "no_confirmation" else 1))
    if outcome == "ok":
        responses.extend([[b"+CPCMREG:1\r\nOK\r\n"], alive, alive, [b"OK\r\n"], [b"OK\r\n"]])
    port = Port(responses)
    control = CallControl(ATChannel(port))
    token = control.poll().token
    control.answer(token)
    if outcome == "ok":
        control.enable_pcm(token)
        with pytest.raises(ModemError, match="idle"):
            control.disable_pcm()
        control.disable_pcm()
        assert port.writes[-1] == b"AT+CPCMREG=0\r"
        assert port.writes.count(b"AT+CPCMREG?\r") == 2
    else:
        with pytest.raises(ModemError):
            control.enable_pcm(token)
        assert (b"AT+CPCMREG=1\r" in port.writes) == (outcome == "no_confirmation")


@pytest.mark.parametrize(
    "outcome",
    ["ok", "activation", "no_call", "answer", "control_close", "fatal_audio", "fatal_and_control"],
)
def test_h1_probe_uses_no_live_settings_and_cleans_owned_call(monkeypatch, outcome, capsys):
    from test_phone import OBSERVED, Control

    activate_failure = outcome == "activation"
    fatal = outcome.startswith("fatal")

    control = Control()
    initial_poll = control.poll
    polls = 0

    def poll():
        nonlocal polls
        polls += 1
        return None if polls == 1 or outcome == "no_call" else initial_poll()

    class NoCallTimeout:
        async def __aenter__(self):
            raise TimeoutError("no call received")

        async def __aexit__(self, *args):
            pass

    if outcome == "no_call":
        monkeypatch.setattr(phone.asyncio, "timeout", lambda seconds: NoCallTimeout())
    if outcome == "answer":
        control.answer = Mock(side_effect=ModemError("synthetic answer failure"))
    control.poll = poll
    control.enable_pcm = Mock(side_effect=ModemError("synthetic") if activate_failure else None)
    control.disable_pcm = Mock()
    control.channel = Mock()
    if outcome == "fatal_and_control":
        control.disable_pcm.side_effect = ModemError("synthetic disable failure")
    if outcome in {"control_close", "fatal_and_control"}:
        control.channel.close.side_effect = ModemError("synthetic close failure")
    opener = Mock(return_value=control)
    monkeypatch.setattr(phone, "open_pcm_control", opener)

    class Endpoint:
        def __init__(self):
            self.writes = []
            self.closed = False
            self.drained = False

        async def read(self):
            return bytes(FRAME_BYTES)

        async def write(self, pcm):
            self.writes.append(pcm)

        async def drain(self):
            self.drained = True

        async def close(self):
            self.closed = True
            if fatal:
                raise FatalAudioError("synthetic PCM close failure")

    endpoint = Endpoint()

    async def open_audio(ports, channel):
        assert channel is control.channel
        return endpoint

    monkeypatch.setattr(phone, "open_pcm_audio", open_audio)
    monkeypatch.setattr(
        cli.Settings, "load", Mock(side_effect=AssertionError("not a live command"))
    )
    if fatal:
        with pytest.raises(FatalAudioError, match="PCM close failure"):
            asyncio.run(phone.modem_audio_test(VoicePorts("control", "audio")))
    else:
        result = cli.main(["modem-audio-test", "--control", "control", "--audio", "audio"])
        assert result == (0 if outcome == "ok" else 2)
    assert control.hung == ([] if outcome == "no_call" else [OBSERVED.token])
    assert control.disable_pcm.call_count == int(outcome not in {"answer", "no_call"})
    control.channel.close.assert_called_once()
    completed = outcome not in {"activation", "answer", "no_call"}
    assert endpoint.closed == endpoint.drained == completed
    if completed:
        assert len(endpoint.writes) == 250
        if outcome == "ok":
            assert "operator_check_required" in capsys.readouterr().out
