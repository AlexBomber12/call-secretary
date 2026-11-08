from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from call_secretary.cli import main
from call_secretary.config import ConfigError, Settings, load_family

EXAMPLE = Path("config/family.example.yaml")


def write_config(tmp_path, mutate):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "family.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def test_example_is_offline_and_does_not_print_private_config(capsys):
    assert main(["--profiles", str(EXAMPLE), "check-config"]) == 0
    assert "Persona" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["defaults"].update(audio_recording_enabled=True),
        lambda r: r["defaults"].update(call_max_duration_seconds=601),
        lambda r: r["defaults"].update(call_max_duration_seconds=True),
        lambda r: r["budget"].update(monthly_api_budget_eur="NaN"),
        lambda r: r["profiles"].append(r["profiles"][0]),
        lambda r: r["profiles"][0].update(telegram_chat_id="@public_group"),
        lambda r: r.update(ingress_routes=[{"ingress_id": "a", "profile_id": "missing"}]),
    ],
)
def test_invalid_configuration_fails_closed(tmp_path, change):
    with pytest.raises(ConfigError):
        load_family(write_config(tmp_path, change))


def test_live_mode_requires_credentials_and_does_not_expose_values():
    settings = Settings(load_family(EXAMPLE), Path("unused"), {})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings.validate_live()
    settings = replace(settings, env={"OPENAI_API_KEY": "private-test-token"})
    assert "private-test-token" not in repr(settings)
    with pytest.raises(ConfigError) as err:
        settings.validate_live()
    assert "private-test-token" not in str(err.value)
