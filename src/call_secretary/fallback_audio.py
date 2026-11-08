"""Synthesize a fixed Italian service prompt locally; never record call audio."""

from __future__ import annotations

import audioop
import io
import shutil
import subprocess
import wave

from call_secretary.audio import AudioError

PHRASE = "Mi dispiace, al momento non posso prendere un messaggio. Riprovi più tardi."


def local_phrase() -> bytes:
    executable = shutil.which("espeak-ng")
    if not executable:
        raise AudioError("Install espeak-ng for the local Italian service prompt")
    try:
        result = subprocess.run(
            [executable, "--stdout", "-v", "it", PHRASE], capture_output=True, timeout=5, check=True
        )
        if len(result.stdout) > 2_000_000:
            raise ValueError
        with wave.open(io.BytesIO(result.stdout)) as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise ValueError
            rate = reader.getframerate()
            if not 8000 <= rate <= 48000:
                raise ValueError
            pcm = reader.readframes(rate * 16 + 1)
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 24000, None)
        if not 2 <= len(pcm) <= 24000 * 2 * 16:
            raise ValueError
        return pcm
    except (OSError, subprocess.SubprocessError, ValueError, wave.Error):
        raise AudioError("Cannot prepare local service prompt") from None
