import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from test_voice import Endpoint, Hooks, Socket

from call_secretary.voice import Realtime, ToolReply, VoiceError, connect_api


def done(rid="r", **changes):
    return {
        "type": "response.done",
        "response": {"id": rid, "status": "completed", "usage": {}, "output": [], **changes},
    }


def test_connect_uses_fixed_host_bounded_transport_and_private_logger(monkeypatch):
    import websockets.asyncio.client

    calls = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        yield "fake-socket"

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)

    async def scenario():
        async with connect_api("synthetic-key", "model&other=bad") as socket:
            assert socket == "fake-socket"

    asyncio.run(scenario())
    url, options = calls[0]
    assert url.startswith("wss://api.openai.com/v1/realtime?")
    assert "%26" in url and options["max_queue"] == 8
    assert not options["logger"].propagate
    assert options["additional_headers"]["Authorization"] == "Bearer synthetic-key"


def test_response_limits_closing_and_pending_turns(rates):
    async def scenario():
        socket = Socket()
        voice = Realtime(socket, Endpoint(), Hooks(), replace(rates, max_responses=2), "p", [], "v")
        await voice.request_response()
        await voice.request_response()
        assert voice.responses == 1 and voice.user_pending
        await voice.close_soon()
        assert socket.sent[-1]["type"] == "response.cancel"
        await voice.handle(done(status="cancelled"))
        assert voice.responses == 2 and "Termina ora" in socket.sent[-1]["response"]["instructions"]
        voice.pending = False
        await voice.close_soon()
        assert voice.finish_event.is_set()
        voice = Realtime(Socket(), Endpoint(), Hooks(), rates, "p", [], "v")
        await voice.close_soon()
        assert voice.closing and voice.pending

    asyncio.run(scenario())


@pytest.mark.parametrize("frame", [b"x", b"\0" * 48002], ids=["odd", "oversized"])
def test_capture_rejects_unbounded_or_misaligned_pcm(rates, frame):
    class Input(Endpoint):
        async def read(self):
            return frame

    async def scenario():
        voice = Realtime(Socket(), Input(), Hooks(), rates, "p", [], "v")
        with pytest.raises(VoiceError, match="frame"):
            await voice._capture()

    asyncio.run(scenario())


def test_capture_counts_bytes_and_stops_on_eof(rates):
    class Input(Endpoint):
        def __init__(self):
            self.frames = iter([b"\0\0", b""])

        async def read(self):
            return next(self.frames)

    async def scenario():
        socket = Socket()
        voice = Realtime(socket, Input(), Hooks(), rates, "p", [], "v")
        await voice._capture()
        assert voice.input_bytes == 2 and voice.finish_event.is_set()
        assert base64.b64decode(socket.sent[0]["audio"]) == b"\0\0"

    asyncio.run(scenario())


def test_provider_events_text_and_audio_validation(rates):
    async def scenario():
        hooks = Hooks()
        voice = Realtime(Socket(), Endpoint(), hooks, rates, "p", [], "v")
        for event, message in [
            ({"type": "error"}, "rejected"),
            (
                {"type": "response.output_audio.delta", "item_id": "a", "delta": "%%%%"},
                "payload",
            ),
            (
                {"type": "response.output_audio_transcript.done", "item_id": "a", "transcript": 1},
                "limit",
            ),
            (
                {
                    "type": "response.output_audio_transcript.done",
                    "item_id": "a",
                    "transcript": "x" * 16001,
                },
                "limit",
            ),
        ]:
            with pytest.raises(VoiceError, match=message):
                await voice.handle(event)
        await voice.handle({"type": "response.output_audio.delta", "item_id": "a", "delta": "AAA="})
        assert voice.playback.queued_bytes == 2
        for kind, text in [
            ("conversation.item.input_audio_transcription.completed", "caller words"),
            ("response.output_audio_transcript.done", "assistant words"),
            ("response.output_audio_transcript.done", ""),
        ]:
            await voice.handle({"type": kind, "item_id": kind, "transcript": text})
        assert [r[1] for r in hooks.texts] == ["caller", "assistant"]
        await voice.handle({"type": "input_audio_buffer.speech_stopped"})
        voice._finish_task = asyncio.create_task(asyncio.Event().wait())
        task = voice._finish_task
        await voice.handle({"type": "input_audio_buffer.speech_started"})
        await asyncio.gather(task, return_exceptions=True)
        assert voice._finish_task is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"usage": None}, "unavailable"),
        ({"status": "failed"}, "failed"),
        (
            {"output": [{"type": "function_call", "arguments": "{"}]},
            "Malformed tool",
        ),
        (
            {"output": [{"type": "function_call", "arguments": "[]"}]},
            "object",
        ),
    ],
)
def test_incomplete_usage_and_malformed_tools_fail_closed(rates, changes, message):
    async def scenario():
        voice = Realtime(Socket(), Endpoint(), Hooks(), rates, "p", [], "v")
        with pytest.raises(VoiceError, match=message):
            await voice.handle(done(**changes))

    asyncio.run(scenario())


def test_completed_tools_update_profile_and_finish_after_playback(rates):
    class Tools(Hooks):
        async def tool(self, name, args):
            assert args == {}
            return ToolReply("ok", "selected prompt", [], finish=name == "finish")

    async def scenario():
        socket, hooks = Socket(), Tools()
        voice = Realtime(socket, Endpoint(), hooks, rates, "p", [], "v")
        await voice.handle(done())
        await voice.handle(done())
        assert len(hooks.usages) == 1
        await voice.handle(
            done(
                "select",
                output=[
                    {"type": "message"},
                    {"type": "function_call", "name": "select", "call_id": "c"},
                ],
            )
        )
        assert voice.prompt == "selected prompt"
        assert any(e["type"] == "session.update" for e in socket.sent)
        await voice.handle(
            done("finish", output=[{"type": "function_call", "name": "finish", "call_id": "f"}])
        )
        await voice._finish_task
        assert voice.finish_event.is_set()

    asyncio.run(scenario())


def test_finish_waits_bounded_time_for_missing_transcription(rates):
    async def scenario():
        voice = Realtime(Socket(), Endpoint(), Hooks(), rates, "p", [], "v")
        voice.transcription_grace = 0.01
        voice.committed_items.add("missing")
        await asyncio.wait_for(voice._finish_after_playback(), 0.5)
        assert voice.finish_event.is_set()
        assert not voice.transcript_complete

    asyncio.run(scenario())


@pytest.mark.parametrize("report", ["completed", "failed", "missing"])
def test_disconnection_waits_only_for_known_committed_transcripts(rates, report):
    class InterruptSignal(Endpoint):
        def __init__(self):
            super().__init__()
            self.stopped = asyncio.Event()

        async def interrupt(self):
            self.stopped.set()

    async def scenario():
        socket, endpoint, hooks = Socket(), InterruptSignal(), Hooks()
        voice = Realtime(socket, endpoint, hooks, rates, "p", [], "v")
        voice.ready = True
        voice.transcription_grace = 0.05
        await voice.handle({"type": "input_audio_buffer.committed", "item_id": "wanted"})
        sent = list(socket.sent)
        draining = asyncio.create_task(voice.drain_after_disconnect())
        await asyncio.wait_for(endpoint.stopped.wait(), 0.5)
        await voice.close_soon()
        await voice.send({"type": "input_audio_buffer.append", "audio": "AAA="})
        await voice.handle(done("late-response"))
        assert hooks.usages == [("late-response", {})]
        await voice.handle({"type": "input_audio_buffer.committed", "item_id": "unwanted"})
        await voice.handle(
            {"type": "response.output_audio.delta", "item_id": "late", "delta": "AAA="}
        )

        async def send_transcript(item, kind):
            await voice.handle(
                {
                    "type": "conversation.item.input_audio_transcription." + kind,
                    "item_id": item,
                    "transcript": "synthetic words",
                }
            )

        await send_transcript("unwanted", "completed")
        await send_transcript("unwanted", "failed")
        assert not draining.done()
        if report != "missing":
            await send_transcript("wanted", report)
        await asyncio.wait_for(draining, 0.5)
        assert voice.transcript_complete == (report == "completed")
        assert [t[0] for t in hooks.texts] == (["wanted"] if report == "completed" else [])
        assert voice.committed_items == {"wanted"}
        assert socket.sent == sent
        assert voice.playback.queue.empty()

    asyncio.run(scenario())


def test_disconnection_before_handshake_does_not_access_playback_and_drops_capture(rates):
    class Audio(Endpoint):
        async def read(self):
            return b"\\0\\0"

    async def scenario():
        socket, endpoint = Socket(), Audio()
        voice = Realtime(socket, endpoint, Hooks(), rates, "p", [], "v")
        await voice.drain_after_disconnect()
        voice.finish_event.set()
        await asyncio.wait_for(voice._capture(), 0.5)
        assert voice.input_bytes == 0 and socket.sent == []
        assert endpoint.interrupts == 0

    asyncio.run(scenario())


def test_remote_drain_bounds_unresponsive_playback_shutdown(rates):
    class Audio(Endpoint):
        async def interrupt(self):
            await asyncio.Event().wait()

    async def scenario():
        voice = Realtime(Socket(), Audio(), Hooks(), rates, "p", [], "v")
        voice.ready = True
        voice.cleanup_timeout = 0.01
        with pytest.raises(VoiceError, match="Audio cleanup timed out"):
            await asyncio.wait_for(voice.drain_after_disconnect(), 0.5)

    asyncio.run(scenario())


def test_only_the_matching_cancel_race_is_recoverable(rates):
    async def scenario():
        socket, hooks = Socket(), Hooks()
        voice = Realtime(socket, Endpoint(), hooks, rates, "p", [], "v")
        await voice.request_response()
        await voice.handle({"type": "response.created", "response": {"id": "r1"}})
        await voice.close_soon()
        cancelled = socket.sent[-1]
        assert cancelled["type"] == "response.cancel"
        assert cancelled["response_id"] == "r1"
        await voice.close_soon()
        assert socket.sent.count(cancelled) == 1
        await voice.handle(done("r1"))
        assert voice.active_response_id is None
        assert voice.pending and voice.responses == 2
        error = {"code": "response_cancel_not_active", "event_id": cancelled["event_id"]}
        await voice.handle({"type": "error", "error": error})
        assert voice.pending and voice.usage_complete
        with pytest.raises(VoiceError, match="rejected"):
            await voice.handle({"type": "error", "error": error | {"event_id": "unrelated"}})

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw,message", [("[]", "Malformed"), ("x" * 262145, "size")], ids=["array", "oversized"]
)
def test_receive_checks_event_shape_and_size(rates, raw, message):
    class Input(Socket):
        async def recv(self):
            return raw

    async def scenario():
        voice = Realtime(Input(), Endpoint(), Hooks(), rates, "p", [], "v")
        with pytest.raises(VoiceError, match=message):
            await voice._receive()

    asyncio.run(scenario())


@pytest.mark.parametrize("finish_tool", [False, True])
def test_run_handshake_and_transport_shutdown(rates, finish_tool):
    class Input(Socket):
        def __init__(self):
            super().__init__()
            self.events = iter(
                [
                    {"type": "session.created"},
                    {"type": "session.updated"},
                    done(
                        output=[{"type": "function_call", "name": "finish", "call_id": "f"}]
                        if finish_tool
                        else [],
                    ),
                ]
            )

        async def recv(self):
            event = next(self.events, None)
            if event is None:
                await asyncio.Event().wait()
            return json.dumps(event)

    class Tools(Hooks):
        async def tool(self, name, args):
            return ToolReply("bye", finish=True)

    class InputAudio(Endpoint):
        async def read(self):
            if finish_tool:
                await asyncio.Event().wait()
            return b""

    async def scenario():
        endpoint = InputAudio()
        voice = Realtime(Input(), endpoint, Tools(), rates, "p", [], "v")
        await asyncio.wait_for(voice.run(), 1)
        assert voice.ready and endpoint.interrupts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("billing_error", [False, True])
def test_reported_recipient_conflict_precedes_finish_and_billing(rates, billing_error):
    class Tools(Hooks):
        def __init__(self):
            super().__init__()
            self.handled = []

        async def usage(self, response_id, usage):
            if billing_error:
                raise VoiceError("Synthetic billing failure")
            await super().usage(response_id, usage)

        async def tool(self, name, args):
            self.handled.append(name)
            return ToolReply("ok", finish=name == "finish_call")

    async def scenario():
        hooks = Tools()
        voice = Realtime(Socket(), Endpoint(), hooks, rates, "p", [], "v")
        event = done(
            "last",
            output=[
                {"type": "function_call", "name": "finish_call", "call_id": "finish"},
                {
                    "type": "function_call",
                    "name": "report_recipient_conflict",
                    "call_id": "hold",
                },
            ],
        )
        if billing_error:
            with pytest.raises(VoiceError, match="Synthetic billing failure"):
                await voice.handle(event)
            assert hooks.handled == ["report_recipient_conflict"]
        else:
            await voice.handle(event)
            assert hooks.handled == ["report_recipient_conflict", "finish_call"]
            await voice._finish_task

    asyncio.run(scenario())
