from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from call_secretary.config import (
    ConfigError,
    Settings,
    chat_id,
    load_env,
    load_family,
    mapping,
    money,
    string,
)


@pytest.mark.parametrize("value", [None, [], {1: "bad"}])
def test_non_string_keyed_config_is_rejected(value):
    with pytest.raises(ConfigError, match="mapping"):
        mapping(value, "configuration")


@pytest.mark.parametrize("value", [None, " ", "x" * 4001])
def test_missing_or_unbounded_text_is_rejected(value):
    with pytest.raises(ConfigError, match="text"):
        string(value, "prompt")


@pytest.mark.parametrize("value", ["not-money", "-1", "Infinity"])
def test_invalid_spending_limit_is_rejected(value):
    with pytest.raises(ConfigError):
        money(value, "budget")


@pytest.mark.parametrize("value", [True, -1, "0", "@group"])
def test_only_private_numeric_destinations_are_accepted(value):
    with pytest.raises(ConfigError):
        chat_id(value, "destination")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(schema_version=2), "schema"),
        (lambda r: r["defaults"].update(text_retention_days=1), "retention"),
        (lambda r: r["defaults"].update(greeting_language="en"), "Greeting"),
        (lambda r: r["budget"].update(scope="person"), "scope"),
        (lambda r: r["budget"].update(timezone="Missing/Zone"), "timezone"),
        (lambda r: r.update(profiles=[]), "1..20"),
        (lambda r: r.update(profiles={}), "1..20"),
        (lambda r: r["profiles"][0].update(id="Wrong ID"), "profile id"),
        (lambda r: r["profiles"][0].update(enabled=1), "boolean"),
        (lambda r: r["profiles"][0].update(recipient_aliases=[]), "aliases"),
        (lambda r: r["profiles"][0].update(report_language="xx"), "language"),
        (
            lambda r: [p.update(telegram_chat_id="123") for p in r["profiles"]],
            "distinct",
        ),
        (lambda r: r["routing"].update(unresolved_action="guess"), "ask_recipient"),
        (lambda r: r["routing"].update(still_unresolved_action="send_all"), "held"),
        (lambda r: r.update(ingress_routes={}), "list"),
        (
            lambda r: r.update(
                ingress_routes=[
                    {"ingress_id": "usb", "profile_id": "member_a", "original_called_number": "123"}
                ]
            ),
            "E.164",
        ),
        (
            lambda r: r.update(
                ingress_routes=[{"ingress_id": "usb", "profile_id": "member_a", "validated": "yes"}]
            ),
            "boolean",
        ),
    ],
)
def test_unsafe_family_policies_fail_before_runtime(tmp_path, mutate, message):
    raw = yaml.safe_load(Path("config/family.example.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "family.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_family(path)


def test_file_size_parse_and_encoding_errors_are_sanitized(tmp_path):
    path = tmp_path / "broken"
    for data in (b"x" * 131073, b"\xff", b"defaults: ["):
        path.write_bytes(data)
        with pytest.raises(ConfigError):
            load_family(path)
    with pytest.raises(ConfigError, match="Cannot read"):
        load_family(tmp_path / "absent")


def test_env_syntax_encoding_and_quoting(tmp_path):
    path = tmp_path / "env"
    path.write_text('# comment\n\nREALTIME_VOICE="marin"\n', encoding="utf-8")
    assert load_env(path)["REALTIME_VOICE"] == "marin"
    for data in (b"not valid", b"lower=value", b"\xff"):
        path.write_bytes(data)
        with pytest.raises(ConfigError):
            load_env(path)


def test_live_policy_checks_modem_profiles_and_audio(settings):
    settings.validate_live(modem=True)
    with pytest.raises(ConfigError, match="Unknown"):
        settings.family.profile("absent")
    disabled = replace(settings.family.profiles[0], enabled=False)
    family = replace(settings.family, profiles=(disabled, *settings.family.profiles[1:]))
    with pytest.raises(ConfigError, match="Enable every"):
        replace(settings, family=family).validate_live(modem=True)
    with pytest.raises(ConfigError, match="disabled"):
        replace(settings, env=settings.env | {"AUDIO_RECORDING_ENABLED": "true"}).validate_live()


@pytest.mark.parametrize("value", ["bad", "NaN"])
def test_malformed_numeric_env_is_a_policy_conflict(tmp_path, value):
    env = tmp_path / "env"
    env.write_text(f"MONTHLY_API_BUDGET_EUR={value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="conflicts"):
        Settings.load(Path("config/family.example.yaml"), env)


def test_string_env_policies_can_match(tmp_path):
    env = tmp_path / "env"
    env.write_text("GREETING_LANGUAGE=IT\nAUDIO_RECORDING_ENABLED=FALSE\n", encoding="utf-8")
    assert Settings.load(Path("config/family.example.yaml"), env).family.max_duration_seconds == 600


def test_valid_ingress_metadata_is_loaded_without_assuming_validation(tmp_path):
    raw = yaml.safe_load(Path("config/family.example.yaml").read_text(encoding="utf-8"))
    raw["ingress_routes"] = [
        {"ingress_id": "usb", "profile_id": "member_a"},
        {
            "ingress_id": "usb",
            "profile_id": "member_b",
            "original_called_number": "+39000000123",
            "validated": True,
        },
    ]
    path = tmp_path / "family.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    family = load_family(path)
    assert not family.routes[0].validated and family.routes[1].validated
