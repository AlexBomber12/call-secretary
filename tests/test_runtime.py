import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from test_voice import Endpoint

from call_secretary import runtime
from call_secretary.audio import FatalAudioError
from call_secretary.config import ConfigError
from call_secretary.storage import Store
from call_secretary.voice import VoiceError


@pytest.mark.parametrize(
    "outcome,expected,complete",
    [
        ("ok", "ended", True),
        ("missing_text", "ended_text_incomplete", True),
        ("timeout", "duration_limit", False),
        ("provider", "provider_or_audio_error", False),
        ("provider_billed", "provider_or_audio_error", True),
        ("open", "provider_or_audio_error", True),
        ("close", "audio_error", True),
        ("connect", "provider_or_audio_error", True),
        ("fatal_open", "audio_error", True),
        ("fatal_close", "audio_error", True),
    ],
)
def test_runtime_persists_final_state_and_keeps_unknown_cost(
    settings, monkeypatch, outcome, expected, complete
):
    endpoints, sessions, timers = [], [], []

    class Audio(Endpoint):
        def __init__(self):
            super().__init__()
            if outcome == "fatal_open":
                raise FatalAudioError("synthetic unreaped startup worker")
            if outcome == "open":
                raise RuntimeError("synthetic unavailable microphone")
            endpoints.append(self)

        async def close(self):
            if outcome == "fatal_close":
                raise FatalAudioError("synthetic unreaped cleanup worker")
            if outcome == "close":
                raise RuntimeError("synthetic close failure")

    @asynccontextmanager
    async def connect(key, model):
        if outcome == "connect":
            raise RuntimeError("synthetic connection failure")
        yield "fake"

    class Session:
        def __init__(self, *args):
            self.transcript_complete = outcome != "missing_text"
            self.usage_complete = outcome not in {"provider", "timeout"}
            self.input_bytes = 48000
            sessions.append(self)

        async def close_soon(self):
            self.closing = True

        async def run(self):
            await asyncio.sleep(0.001)
            if outcome == "timeout":
                raise TimeoutError
            if outcome in {"provider", "provider_billed"}:
                raise VoiceError("synthetic provider failure")

    class Timer:
        def __init__(self, seconds, *, started):
            assert seconds == 600 and started > 0
            timers.append(started)

        def remaining(self):
            return 0

        async def run(self, op):
            return await op

    async def open_audio():
        return Audio()

    monkeypatch.setattr(runtime.IsolatedAudio, "open", open_audio)
    monkeypatch.setattr(runtime, "connect_api", connect)
    monkeypatch.setattr(runtime, "Realtime", Session)
    monkeypatch.setattr(runtime, "Deadline", Timer)
    if outcome in {"fatal_open", "fatal_close"}:
        with pytest.raises(FatalAudioError):
            asyncio.run(runtime.run_microphone(settings, "member_a"))
    elif expected in {"provider_or_audio_error", "audio_error"}:
        with pytest.raises(VoiceError, match="pending state retained"):
            asyncio.run(runtime.run_microphone(settings, "member_a"))
    else:
        assert asyncio.run(runtime.run_microphone(settings, "member_a"))
    assert timers
    if sessions:
        assert sessions[0].closing
    with Store(settings.state_dir, mode="live") as store:
        call = store.db.execute("SELECT * FROM calls").fetchone()
        assert call["status"] == expected
        assert bool(call["voice_usage_complete"]) == complete
        assert store.counts()["summary_pending"] == 1


def test_runtime_refuses_all_disabled_profiles(settings):
    family = replace(
        settings.family, profiles=tuple(replace(p, enabled=False) for p in settings.family.profiles)
    )
    with pytest.raises(ConfigError, match="Enable a profile"):
        asyncio.run(runtime.run_microphone(replace(settings, family=family), None))
