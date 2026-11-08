import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from call_secretary import cli, phone
from call_secretary.audio import AudioError
from call_secretary.config import ConfigError, IngressRoute
from call_secretary.health import snapshot
from call_secretary.isolated_audio import AudioSpec
from call_secretary.modem import ModemError
from call_secretary.sim7600 import ATChannel, CallControl, ObservedCall
from call_secretary.storage import Store
from call_secretary.summaries import SummaryWorker
from call_secretary.telegram import CapturedTelegram, DeliveryWorker, Enrollment

OPTIONS = phone.PhoneOptions("synthetic-port", "shared", AudioSpec("capture", "playback", 8000))
OBSERVED = ObservedCall("call-token", 1, 4, "+39000000123")
USAGE = {
    "input_tokens": 5,
    "output_tokens": 5,
    "input_token_details": {"audio_tokens": 0},
    "output_token_details": {"audio_tokens": 0},
}


def accepted(settings):
    return replace(
        settings,
        env=settings.env
        | {
            "PHONE_H1_CONFIRMED": "true",
            "MODEM_AUDIO_DEVICE": "dedicated",
            "MODEM_AUDIO_RATE": "8000",
            "PHONE_INGRESS_ID": "shared",
        },
    )


@pytest.mark.parametrize(
    "change",
    [
        {"PHONE_H1_CONFIRMED": "false"},
        {"MODEM_AUDIO_DEVICE": ""},
        {"MODEM_AUDIO_RATE": "oops"},
        {"MODEM_AUDIO_RATE": "44100"},
    ],
)
def test_phone_gate_fails_before_devices_or_external_requests(settings, change, monkeypatch):
    settings = replace(accepted(settings), env=accepted(settings).env | change)
    opener = Mock(side_effect=AssertionError("must not open"))
    monkeypatch.setattr(phone, "open_control", opener)
    with pytest.raises(ConfigError):
        asyncio.run(phone.run_phone(settings))
    opener.assert_not_called()


def test_phone_options_support_distinct_explicit_capture_and_playback(settings):
    settings = accepted(settings)
    options = phone.PhoneOptions.load(
        replace(
            settings,
            env=settings.env
            | {"MODEM_AUDIO_INPUT": "dedicated input", "MODEM_AUDIO_OUTPUT": "dedicated output"},
        )
    )
    assert options.audio == AudioSpec("dedicated input", "dedicated output", 8000)


@pytest.mark.parametrize("model", [["SIMCOM_SIM7600E-H"], ["Quectel EC25"]])
def test_opener_probes_model_before_call_commands_and_closes_rejected_hardware(monkeypatch, model):
    port, control = Mock(), Mock()
    monkeypatch.setattr(phone, "open_serial", lambda device: port)
    monkeypatch.setattr(phone, "at_query", lambda p, cmd: model)
    monkeypatch.setattr(phone, "CallControl", lambda ch: control)
    if "SIM7600" in model[0]:
        assert phone.open_control("synthetic") is control
        port.close.assert_not_called()
    else:
        with pytest.raises(ModemError):
            phone.open_control("synthetic")
        port.close.assert_called_once()


class Endpoint:
    latency_ms = 0

    def __init__(self):
        self.output, self.closed, self.interrupted = [], False, False

    async def read(self):
        await asyncio.Event().wait()

    async def write(self, pcm):
        self.output.append(pcm)

    async def interrupt(self):
        self.interrupted = True

    async def close(self):
        self.closed = True


class Control:
    def __init__(self):
        self.current = OBSERVED
        self.answered, self.hung = [], []

    def answer(self, token):
        self.answered.append(token)
        self.current = replace(self.current, state=0)

    def poll(self):
        return self.current

    def hangup(self, token):
        if self.current is None or self.current.token != token:
            raise ModemError("stale call")
        self.hung.append(token)
        self.current = None


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("ok", "ended"),
        ("missing", "ended_text_incomplete"),
        ("connect", "provider_or_audio_error"),
        ("api", "provider_or_audio_error"),
        ("key", "provider_or_audio_error"),
        ("budget", "budget_refused"),
        ("audio", "provider_or_audio_error"),
        ("hangup", "caller_hangup"),
        ("hangup_late", "caller_hangup"),
        ("replacement", "caller_hangup"),
        ("usb", "provider_or_audio_error"),
        ("deadline", "duration_limit"),
        ("closing", "ended"),
        ("answer_delayed", "ended"),
        ("answer_unconfirmed", "provider_or_audio_error"),
        ("answer_lost", "provider_or_audio_error"),
    ],
)
def test_call_lifecycle_limits_fallback_hangup_usb_and_billing(
    settings, tmp_path, monkeypatch, outcome, expected
):
    control, endpoint, connects = Control(), Endpoint(), []
    if outcome == "hangup_late":
        control.poll = Mock(side_effect=[replace(OBSERVED, state=0), OBSERVED, None])
    if outcome == "answer_delayed":
        control.poll = Mock(side_effect=[OBSERVED, replace(OBSERVED, state=0)])
    if outcome == "answer_unconfirmed":
        control.poll = lambda: OBSERVED
    if outcome == "answer_lost":
        control.poll = lambda: None
    family = replace(settings.family, routes=(IngressRoute("shared", "member_a", None, True),))
    if outcome == "deadline":
        family = replace(family, max_duration_seconds=0.2)
    if outcome == "closing":
        family = replace(family, max_duration_seconds=1)
    if outcome == "budget":
        family = replace(family, monthly_budget_eur=Decimal("1"))
    settings = replace(settings, family=family)
    if outcome == "key":
        settings = replace(settings, env=settings.env | {"OPENAI_API_KEY": ""})

    async def open_audio(spec):
        if outcome == "audio":
            raise RuntimeError("synthetic audio failure")
        return endpoint

    @asynccontextmanager
    async def connect(key, model):
        connects.append(model)
        if outcome == "connect":
            raise OSError("private network error")
        yield "synthetic socket"

    class Session:
        def __init__(self, socket, ep, call, *args):
            self.call = call
            self.input_bytes = 48000
            self.usage_complete = True
            self.committed_items = {"caller"}
            self.transcribed_items = set() if outcome == "missing" else {"caller"}

        async def close_soon(self):
            self.closing = True

        async def run(self):
            await self.call.text("caller", "caller", "Synthetic request", False, 1)
            if outcome in {"hangup", "replacement"}:
                control.current = (
                    None if outcome == "hangup" else replace(OBSERVED, token="replacement")
                )
            if outcome == "usb":
                control.poll = Mock(side_effect=ModemError("USB disconnected"))
                control.hangup = Mock(side_effect=ModemError("USB disconnected"))
            if outcome in {"hangup", "hangup_late", "replacement", "usb", "deadline"}:
                await asyncio.Event().wait()
            if outcome == "api":
                raise OSError("API unavailable")
            await asyncio.sleep(0.01)
            if outcome == "closing":
                assert self.closing

    monkeypatch.setattr(phone, "Realtime", Session)

    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await phone.handle_call(
                settings,
                store,
                OPTIONS,
                control,
                OBSERVED,
                b"\0" * 1920,
                audio_factory=open_audio,
                connector=connect,
            )
            call = store.call(cid)
            assert call["status"] == expected
            assert call["profile_id"] == "member_a"
            assert store.counts()["summary_pending"] == 1
            if outcome in {"key", "budget", "connect", "api"}:
                assert b"".join(endpoint.output) == b"\0" * 1920 and endpoint.interrupted
            if outcome in {"key", "budget", "audio", "answer_unconfirmed", "answer_lost"}:
                assert connects == []
            if outcome in {"hangup", "replacement", "usb"}:
                assert control.hung == []  # Never send a global hangup to a replacement.
            elif outcome != "audio":
                assert control.hung == [OBSERVED.token]
            if outcome != "audio":
                assert endpoint.closed
            if outcome in {
                "connect",
                "api",
                "usb",
                "hangup",
                "hangup_late",
                "replacement",
            }:
                assert call["voice_usage_complete"] == 0
            if outcome == "deadline":
                # Startup I/O can consume the deadline before any API attempt;
                # only an actual attempt may leave usage unknown.
                assert call["voice_usage_complete"] == int(not connects)
            await SummaryWorker(store, family).process_one()

    asyncio.run(scenario())


def test_fatal_audio_cleanup_failure_keeps_text_job_and_marks_health(settings, monkeypatch):
    class BrokenClose(Endpoint):
        async def close(self):
            raise RuntimeError("synthetic native cleanup failure")

    async def open_audio(spec):
        return BrokenClose()

    settings = replace(settings, env=settings.env | {"OPENAI_API_KEY": ""})

    async def scenario():
        with Store(settings.state_dir) as store:
            with pytest.raises(RuntimeError, match="cleanup"):
                await phone.handle_call(
                    settings, store, OPTIONS, Control(), OBSERVED, b"\0\0", audio_factory=open_audio
                )
            assert store.db.execute("SELECT status FROM calls").fetchone()[0] == "audio_error"
            assert store.counts()["summary_pending"] == 1
            assert snapshot(store)["audio"]["state"] == "unavailable"

    asyncio.run(scenario())


def test_provider_failure_during_output_write_reopens_audio_for_local_phrase(settings):
    """Realtime cleanup cancels playback; a cancelled native lane cannot be reused."""

    async def scenario():
        writing = asyncio.Event()
        old, new = Endpoint(), Endpoint()

        async def blocked_write(pcm):
            writing.set()
            try:
                await asyncio.Event().wait()
            finally:
                old.closed = True  # AudioLane.exchange kills its lane on cancellation.

        async def old_interrupt():
            if old.closed:
                raise AudioError("Audio worker closed")

        old.write, old.interrupt = blocked_write, old_interrupt
        endpoints = []

        async def open_audio(spec):
            result = old if not endpoints else new
            endpoints.append(result)
            return result

        class Socket:
            def __init__(self):
                self.step = 0

            async def send(self, message):
                pass

            async def recv(self):
                self.step += 1
                if self.step == 1:
                    return json.dumps({"type": "session.updated"})
                if self.step == 2:
                    return json.dumps(
                        {
                            "type": "response.output_audio.delta",
                            "item_id": "greeting",
                            "delta": base64.b64encode(b"\0" * 960).decode(),
                        }
                    )
                await writing.wait()
                return json.dumps({"type": "error"})

        @asynccontextmanager
        async def connect(key, model):
            yield Socket()

        with Store(settings.state_dir) as store:
            cid = await phone.handle_call(
                settings,
                store,
                OPTIONS,
                Control(),
                OBSERVED,
                b"\1\0" * 960,
                audio_factory=open_audio,
                connector=connect,
            )
            assert endpoints == [old, new]
            assert b"".join(new.output) == b"\1\0" * 960
            assert old.closed and new.closed
            assert store.call(cid)["status"] == "provider_or_audio_error"
            assert store.call(cid)["voice_usage_complete"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("spoken", [False, True])
def test_real_at_controller_realtime_and_workers_form_one_offline_phone_journey(
    settings, monkeypatch, spoken
):
    """Phone orchestrator is real; only serial, audio and network boundaries are scripted."""

    class Port:
        def __init__(self):
            self.state, self.pending, self.writes = 4, b"", []

        def write(self, data):
            self.writes.append(data)
            if data == b"ATA\r":
                self.state = 0
            if data == b"AT+CHUP\r":
                self.state = None
            if data == b"AT+CLCC\r" and self.state is not None:
                self.pending = f'+CLCC: 1,1,{self.state},0,0,"39000000123",145\r\n'.encode()
            self.pending += b"OK\r\n"
            return len(data)

        def read(self, size):
            data, self.pending = self.pending, b""
            return data

        def close(self):
            pass

    class Socket:
        def __init__(self):
            self.events = asyncio.Queue()
            self.responses = 0

        async def send(self, raw):
            event = json.loads(raw)
            if event["type"] == "session.update":
                await self.events.put({"type": "session.updated"})
            if event["type"] == "response.create":
                self.responses += 1
                rid = str(self.responses)
                await self.events.put({"type": "response.created", "response": {"id": rid}})
                if self.responses == 1:
                    await self.events.put(
                        {
                            "type": "response.output_audio.delta",
                            "response_id": rid,
                            "item_id": "greeting",
                            "delta": base64.b64encode(b"\0" * 960).decode(),
                        }
                    )
                    await self.events.put(
                        {
                            "type": "response.done",
                            "response": {
                                "id": rid,
                                "status": "completed",
                                "usage": USAGE,
                                "output": [],
                            },
                        }
                    )
                    await self.events.put(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "caller",
                            "transcript": "Per Persona B. Richiamami domani, messaggio di prova.",
                        }
                    )
                    await self.events.put(
                        {"type": "input_audio_buffer.committed", "item_id": "caller"}
                    )
                else:
                    await self.events.put(
                        {
                            "type": "response.done",
                            "response": {
                                "id": rid,
                                "status": "completed",
                                "usage": USAGE,
                                "output": [
                                    {
                                        "type": "function_call",
                                        "name": "select_recipient"
                                        if spoken and self.responses == 2
                                        else "finish_call",
                                        "arguments": '{"profile_id":"member_b"}'
                                        if spoken and self.responses == 2
                                        else "{}",
                                        "call_id": "finish",
                                    }
                                ],
                            },
                        }
                    )

        async def recv(self):
            return json.dumps(await self.events.get())

    @asynccontextmanager
    async def connect(key, model):
        yield Socket()

    endpoint = Endpoint()

    async def open_audio(spec):
        return endpoint

    family = replace(
        settings.family, routes=() if spoken else (IngressRoute("shared", "member_b", None, True),)
    )
    settings = replace(settings, family=family)

    async def scenario():
        port = Port()
        control = CallControl(ATChannel(port))
        observed = control.poll()
        with Store(settings.state_dir, mode="live") as store:
            enrollment = Enrollment(store, family)
            enrollment.claim(enrollment.invite("member_b"), "102")
            cid = await phone.handle_call(
                settings,
                store,
                OPTIONS,
                control,
                observed,
                b"\0" * 960,
                audio_factory=open_audio,
                connector=connect,
            )
            assert store.call(cid)["status"] == "ended" and endpoint.closed
            assert port.writes.count(b"ATA\r") == port.writes.count(b"AT+CHUP\r") == 1
            await SummaryWorker(store, family).process_one()
            api = CapturedTelegram()
            await DeliveryWorker(store, family, api).process_one()
            assert len(api.receipts) == 1 and api.receipts[0]["chat_id"] == "102"
            assert "Richiamami domani" in api.receipts[0]["text"] and cid in api.receipts[0]["text"]
            assert store.db.execute("SELECT closed FROM budget_reservations").fetchone()[0] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["idle", "call", "stop_call", "cancel", "reconnect", "not_enrolled", "disabled"]
)
def test_service_sigterm_reconnect_and_safe_startup(settings, monkeypatch, mode):
    settings = accepted(settings)
    if mode == "disabled":
        settings = replace(
            settings,
            family=replace(
                settings.family,
                profiles=tuple(replace(p, enabled=False) for p in settings.family.profiles),
            ),
        )
    if mode not in {"not_enrolled", "disabled"}:
        with Store(settings.state_dir, mode="live") as store:
            enrollment = Enrollment(store, settings.family)
            for n, profile in enumerate(settings.family.profiles, start=101):
                enrollment.claim(enrollment.invite(profile.id), str(n))
    monkeypatch.setattr(phone, "local_phrase", lambda: b"\0" * 960)
    controls, callbacks = [], []

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.append(cb))
        removed = Mock()
        monkeypatch.setattr(loop, "remove_signal_handler", removed)
        actual_wait = asyncio.wait_for

        async def short_wait(awaitable, **options):
            return await actual_wait(awaitable, timeout=min(options["timeout"], 0.01))

        proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
        proxy.wait_for = short_wait
        monkeypatch.setattr(phone, "asyncio", proxy)
        attempts = []

        class Link:
            def __init__(self):
                self.channel = Mock()
                self.polls = 0
                controls.append(self)

            def poll(self):
                self.polls += 1
                if mode in {"call", "stop_call", "cancel"} and self.polls == 1:
                    return OBSERVED
                if self.polls > 1:
                    callbacks[0]()
                return None

        def opener(device):
            attempts.append(device)
            if mode == "reconnect" and len(attempts) == 1:
                raise ModemError("synthetic disconnection")
            return Link()

        async def handle(*args):
            if mode == "cancel":
                raise asyncio.CancelledError
            if mode == "stop_call":
                callbacks[0]()
                await asyncio.Event().wait()
            return "synthetic-call-id"

        monkeypatch.setattr(phone, "open_control", opener)
        monkeypatch.setattr(phone, "handle_call", handle)
        if mode in {"not_enrolled", "disabled"}:
            with pytest.raises(ConfigError):
                await phone.run_phone(settings)
            assert not attempts
        elif mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await phone.run_phone(settings)
        else:
            await asyncio.wait_for(phone.run_phone(settings), 2)
            assert len(attempts) == (2 if mode == "reconnect" else 1)
            with Store(settings.state_dir) as store:
                assert snapshot(store)["modem"]["state"] == "stopped"
        removed.assert_called_once()
        assert all(c.channel.close.call_count == 1 for c in controls)

    asyncio.run(scenario())


def test_phone_cli_dispatches_and_hardware_gate_is_not_implicit(settings, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda *args: settings)
    assert cli.main(["phone-run"]) == 2
    called = []

    async def run(value):
        called.append(value)

    monkeypatch.setattr(phone, "run_phone", run)
    assert cli.main(["phone-run"]) == 0 and called == [settings]


def test_invalid_paid_policy_does_not_disable_free_error_reports_or_enrollment(
    settings, monkeypatch
):
    from call_secretary import telegram

    settings = replace(settings, env=settings.env | {"BILLING_REVIEWED_ON": ""})
    monkeypatch.setattr(cli.Settings, "load", lambda *args: settings)

    class Client(CapturedTelegram):
        def __init__(self, token):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    clients = []

    def client(token):
        value = Client(token)
        clients.append(value)
        return value

    async def workers(store, family, api, summary):
        assert summary.provider is None and summary.rates is None
        enrollment = Enrollment(store, family)
        enrollment.claim(enrollment.invite("member_a"), "101")
        cid = store.create_call("synthetic")
        store.route_call(cid, "member_a", "caller_selected")
        store.end_call(cid, "provider_or_audio_error", voice_usage_complete=True)
        assert await summary.process_one()
        assert await DeliveryWorker(store, family, api).process_one()
        assert len(api.receipts) == 1 and cid in api.receipts[0]["text"]

    monkeypatch.setattr(telegram, "TelegramClient", client)
    monkeypatch.setattr(telegram, "run_workers", workers)
    assert cli.main(["worker"]) == 0
    assert clients[0].closed
