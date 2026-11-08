import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from test_voice import Endpoint, Hooks, Socket

from call_secretary import voice as module
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


def test_finish_waits_bounded_time_for_missing_transcription(rates, monkeypatch):
    async def scenario():
        voice = Realtime(Socket(), Endpoint(), Hooks(), rates, "p", [], "v")
        voice.committed_items.add("missing")
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        await voice._finish_after_playback()
        assert sleeps.count(0.1) == 20
        assert voice.finish_event.is_set()

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
