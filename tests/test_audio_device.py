import sys
from types import SimpleNamespace

import pytest

from call_secretary.audio import AudioError, Microphone


def test_partially_opened_microphone_is_closed_when_speaker_open_fails(monkeypatch):
    closed = []
    input_stream = SimpleNamespace(close=lambda: closed.append(True))

    def fail(**kwargs):
        raise RuntimeError("synthetic unavailable device")

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=lambda **kwargs: input_stream, RawOutputStream=fail),
    )
    with pytest.raises(AudioError):
        Microphone()
    assert closed == [True]
