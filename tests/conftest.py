from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_secretary.budget import RateCard
from call_secretary.config import Settings, load_family


@pytest.fixture
def family():
    return load_family(Path("config/family.example.yaml"))


@pytest.fixture
def live_env():
    return {
        "OPENAI_API_KEY": "synthetic-key",
        "REALTIME_MODEL": "gpt-realtime-2.1",
        "REALTIME_VOICE": "marin",
        "BILLING_REVIEWED_ON": datetime.now(UTC).date().isoformat(),
        "USD_TO_EUR_UPPER_BOUND": "1.5",
        "TELEGRAM_BOT_TOKEN": "synthetic-token",
        "MODEM_CONTROL_DEVICE": "synthetic-port",
        "MODEM_AUDIO_DEVICE": "synthetic-audio",
    }


@pytest.fixture
def rates(live_env):
    return RateCard.load(live_env)


@pytest.fixture
def settings(family, live_env, tmp_path):
    return Settings(family, tmp_path / "state", live_env)
