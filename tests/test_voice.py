import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from call_secretary.audio import AudioError, Playback
from call_secretary.budget import RateCard
from call_secretary.voice import Realtime, ToolReply, VoiceError, session_update


class Endpoint:
    latency_ms = 0

    def __init__(self):
        self.output = []
        self.interrupts = 0

    async def read(self):
        await asyncio.Event().wait()

    async def write(self, pcm):
        self.output.append(pcm)
        await asyncio.sleep(0)

    async def interrupt(self):
        self.interrupts += 1

    async def close(self):
        pass


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class Hooks:
    async def activity(self, speaking=None):
        pass

    def __init__(self):
        self.texts = []
        self.usages = []
        self.interrupts = []

    async def text(self, *args):
        self.texts.append(args)

    async def usage(self, *args):
        self.usages.append(args)

    async def tool(self, name, args):
        return ToolReply("ok")

    async def interrupted(self, item):
        self.interrupts.append(item)


def rates():
    return RateCard(
        "gpt-realtime-2.1",
        Decimal("1.5"),
        Decimal("32"),
        Decimal("64"),
        Decimal("4"),
        Decimal("24"),
        reviewed_on=datetime.now(UTC).date(),
    )


def test_configuration_uses_current_nested_audio_and_client_controls_responses():
    reviewed = replace(rates(), transcription_model="gpt-4o-mini-transcribe")
    session = session_update("prompt", [], "marin", reviewed)["session"]
    assert session["audio"]["input"]["transcription"]["model"] == reviewed.transcription_model
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["turn_detection"]["create_response"] is False
    assert session["truncation"]["token_limits"]["post_instructions"] == 4096


def test_playback_is_bounded_without_blocking_event_reader():
    async def scenario():
        playback = Playback(Endpoint(), max_bytes=960)
        playback.enqueue("item", b"\0" * 960)
        with pytest.raises(AudioError):
            playback.enqueue("next", b"\0\0")
        await playback.interrupt()
        assert playback.queued_bytes == 0

    asyncio.run(scenario())


def test_barge_in_discards_late_audio_and_truncates_only_played_duration():
    async def scenario():
        endpoint, socket, hooks = Endpoint(), Socket(), Hooks()
        voice = Realtime(socket, endpoint, hooks, rates(), "prompt", [], "marin")
        voice.playback.current_item = "assistant-1"
        voice.playback.written["assistant-1"] = 4800  # 100ms
        voice.playback.enqueue("assistant-1", b"\0" * 960)
        await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "caller-1"})
        assert socket.sent[-1]["audio_end_ms"] == 100
        assert hooks.interrupts == ["assistant-1"]
        await voice.handle(
            {
                "type": "response.output_audio.delta",
                "item_id": "assistant-1",
                "delta": base64.b64encode(b"\0" * 960).decode(),
            }
        )
        assert voice.playback.queued_bytes == 0

    asyncio.run(scenario())


def test_every_response_is_capped_and_duplicate_commits_do_not_generate_twice():
    async def scenario():
        socket = Socket()
        voice = Realtime(socket, Endpoint(), Hooks(), rates(), "prompt", [], "marin")
        event = {"type": "input_audio_buffer.committed", "item_id": "caller-1"}
        await voice.handle(event)
        await voice.handle(event)
        assert len(socket.sent) == 1
        assert socket.sent[0]["response"]["max_output_tokens"] == 512

    asyncio.run(scenario())


def test_interruption_before_first_playback_discards_future_cancelled_response_audio():
    async def scenario():
        socket, hooks = Socket(), Hooks()
        voice = Realtime(socket, Endpoint(), hooks, rates(), "prompt", [], "marin")
        await voice.handle({"type": "response.created", "response": {"id": "r1"}})
        await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "caller"})
        await voice.handle(
            {
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "item_id": "late",
                "delta": base64.b64encode(b"\0" * 960).decode(),
            }
        )
        assert voice.playback.queued_bytes == 0
        assert socket.sent[-1]["audio_end_ms"] == 0

    asyncio.run(scenario())


def test_turn_interrupted_before_response_created_drops_that_responses_audio():
    async def scenario():
        socket = Socket()
        voice = Realtime(socket, Endpoint(), Hooks(), rates(), "prompt", [], "marin")
        await voice.request_response()
        await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "caller"})
        await voice.handle({"type": "response.created", "response": {"id": "r1"}})
        await voice.handle(
            {
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "item_id": "late",
                "delta": "AAA=",
            }
        )
        assert voice.playback.queue.empty()
        assert socket.sent[-1]["audio_end_ms"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed, still_generating", [(0.0, False), (1.0, False), (1.0, True)])
def test_barge_in_truncates_current_audio_only_when_queued_or_still_generating(
    elapsed, still_generating, monkeypatch
):
    from call_secretary import audio

    now = 50.0

    async def scenario():
        nonlocal now
        endpoint, socket, hooks = Endpoint(), Socket(), Hooks()
        endpoint.latency_ms = 10
        voice = Realtime(socket, endpoint, hooks, rates(), "prompt", [], "marin")
        await voice.handle({"type": "response.created", "response": {"id": "r1"}})
        await voice.handle(
            {
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "item_id": "a",
                "delta": base64.b64encode(bytes(960)).decode(),
            }
        )
        playback = asyncio.create_task(voice.playback.run())
        try:
            await voice.playback.queue.join()
            if not still_generating:
                await voice.handle(
                    {
                        "type": "response.done",
                        "response": {"id": "r1", "usage": {}, "status": "completed"},
                    }
                )
            now += elapsed
            await voice.handle({"type": "input_audio_buffer.speech_started", "item_id": "caller"})
            durations = [
                e["audio_end_ms"] for e in socket.sent if e["type"] == "conversation.item.truncate"
            ]
            assert durations == ([10] if not elapsed else ([20] if still_generating else []))
        finally:
            playback.cancel()
            await asyncio.gather(playback, return_exceptions=True)

    monkeypatch.setattr(audio, "monotonic", lambda: now, raising=False)
    asyncio.run(scenario())


def test_cancelled_partial_function_is_not_executed_or_parsed():
    async def scenario():
        hooks = Hooks()
        voice = Realtime(Socket(), Endpoint(), hooks, rates(), "prompt", [], "marin")
        await voice.handle(
            {
                "type": "response.done",
                "response": {
                    "id": "r1",
                    "status": "cancelled",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "output": [
                        {
                            "type": "function_call",
                            "name": "select_recipient",
                            "arguments": '{"broken',
                        }
                    ],
                },
            }
        )
        assert len(hooks.usages) == 1

    asyncio.run(scenario())


def test_shutdown_does_not_wait_forever_for_unresponsive_audio_cleanup():
    class HandshakeSocket(Socket):
        def __init__(self):
            super().__init__()
            self.initial = True

        async def recv(self):
            if self.initial:
                self.initial = False
                return json.dumps({"type": "session.updated"})
            await asyncio.Event().wait()

    class StuckCleanup(Endpoint):
        async def interrupt(self):
            await asyncio.Event().wait()

    async def scenario():
        voice = Realtime(HandshakeSocket(), StuckCleanup(), Hooks(), rates(), "prompt", [], "marin")
        voice.cleanup_timeout = 0.02
        voice.finish_event.set()
        with pytest.raises(VoiceError, match="cleanup timed out"):
            await asyncio.wait_for(voice.run(), 0.5)

    asyncio.run(scenario())
