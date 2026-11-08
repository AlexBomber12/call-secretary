import io
import subprocess
import wave
from types import SimpleNamespace

import pytest

from call_secretary import fallback_audio, health
from call_secretary.audio import AudioError
from call_secretary.storage import Store


def wav(*, channels=1, width=2, rate=22050, frames=22050):
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setparams((channels, width, rate, frames, "NONE", "not compressed"))
        writer.writeframes(b"\0" * frames * width * channels)
    return target.getvalue()


@pytest.mark.parametrize(
    "bad",
    [None, "missing", "process", "size", "channels", "width", "rate", "long", "empty", "invalid"],
)
def test_fixed_local_prompt_is_memory_only_bounded_and_never_uses_cloud(monkeypatch, bad):
    output = {
        "channels": lambda: wav(channels=2),
        "width": lambda: wav(width=1),
        "rate": lambda: wav(rate=1000),
        "long": lambda: wav(frames=22050 * 17),
        "empty": lambda: wav(frames=0),
        "size": lambda: b"x" * 2_000_001,
        "invalid": lambda: b"not a wav",
    }.get(bad, wav)()
    monkeypatch.setattr(
        fallback_audio.shutil, "which", lambda exe: None if bad == "missing" else "synthetic-espeak"
    )

    def run(args, **kwargs):
        assert args == ["synthetic-espeak", "--stdout", "-v", "it", fallback_audio.PHRASE]
        assert kwargs == {"capture_output": True, "timeout": 5, "check": True}
        if bad == "process":
            raise subprocess.TimeoutExpired(args, 5)
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(fallback_audio.subprocess, "run", run)
    if bad:
        with pytest.raises(AudioError):
            fallback_audio.local_phrase()
    else:
        pcm = fallback_audio.local_phrase()
        assert 47990 < len(pcm) <= 48000


def test_health_distinguishes_unknown_recent_stale_and_future_without_content(tmp_path):
    with Store(tmp_path / "state") as store:
        assert health.snapshot(store, now=100)["modem"]["state"] == "unknown"
        health.update(store, "modem", "ready", now=100)
        assert health.snapshot(store, now=120)["modem"]["state"] == "ready"
        assert health.snapshot(store, now=131)["modem"]["state"] == "stale"
        assert health.snapshot(store, now=90)["modem"]["state"] == "stale"
        health.update(store, "voice", "idle")
        assert health.snapshot(store)["voice"]["state"] == "idle"
        for component, state in [("secret", "ready"), ("voice", "secret")]:
            with pytest.raises(ValueError):
                health.update(store, component, state)
