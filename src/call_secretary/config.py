"""Validated configuration. Error messages never interpolate secret values."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    pass


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{label}: expected a mapping")
    return value


def string(value: Any, label: str, *, empty: bool = False, maximum: int = 4000) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ConfigError(f"{label}: invalid text")
    return value.strip()


def integer(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{label}: expected integer between {low} and {high}")
    return value


def money(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"{label}: invalid amount") from None
    if not result.is_finite() or result <= 0:
        raise ConfigError(f"{label}: expected a positive finite amount")
    return result


def chat_id(value: Any, label: str) -> str | None:
    if value in (None, "") or (isinstance(value, str) and value.startswith("<PRIVATE_")):
        return None
    # Reports are private chats only; user IDs are positive decimal integers.
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ConfigError(f"{label}: expected a private numeric chat ID")
    return str(value)


@dataclass(frozen=True)
class Profile:
    id: str
    owner_name: str
    aliases: tuple[str, ...]
    greeting: str
    instructions: str
    report_language: str
    enabled: bool
    telegram_chat_id: str | None = field(repr=False)


@dataclass(frozen=True)
class IngressRoute:
    ingress_id: str
    profile_id: str
    original_called_number: str | None
    validated: bool


@dataclass(frozen=True)
class Family:
    profiles: tuple[Profile, ...]
    routes: tuple[IngressRoute, ...]
    unresolved_greeting: str
    monthly_budget_eur: Decimal
    timezone: str
    max_duration_seconds: int

    def profile(self, profile_id: str) -> Profile:
        for p in self.profiles:
            if p.id == profile_id:
                return p
        raise ConfigError("Unknown recipient profile")


def load_family(path: Path) -> Family:
    try:
        if path.stat().st_size > 131072:
            raise ConfigError("Profiles file exceeds size limit")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Cannot read profiles YAML") from None
    root = mapping(raw, "profiles file")
    if root.get("schema_version") != 1:
        raise ConfigError("Unsupported profiles schema_version")
    defaults = mapping(root.get("defaults"), "defaults")
    if defaults.get("audio_recording_enabled") is not False:
        raise ConfigError("Audio recording must be disabled")
    if defaults.get("text_retention_days") != 0:
        raise ConfigError("This release requires text_retention_days=0 (no automatic expiry)")
    if defaults.get("greeting_language") != "it":
        raise ConfigError("Greeting language must be it")
    duration = integer(defaults.get("call_max_duration_seconds"), "call duration", 30, 600)
    budget = mapping(root.get("budget"), "budget")
    if budget.get("scope") != "family":
        raise ConfigError("Budget scope must be family")
    timezone = string(budget.get("timezone"), "budget timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ConfigError("Unknown budget timezone; install tzdata") from None
    rows = root.get("profiles")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ConfigError("Expected 1..20 profiles")
    profiles = []
    for row in rows:
        r = mapping(row, "profile")
        pid = string(r.get("id"), "profile id", maximum=64)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", pid):
            raise ConfigError("Invalid profile id")
        enabled = r.get("enabled", True)
        if type(enabled) is not bool:
            raise ConfigError("Profile enabled must be boolean")
        aliases = r.get("recipient_aliases", [])
        if not isinstance(aliases, list) or not aliases:
            raise ConfigError("Profile needs recipient_aliases")
        lang = string(r.get("report_language", defaults.get("report_language")), "report language")
        if lang not in {"ru", "it", "en"}:
            raise ConfigError("Report language must be ru, it or en")
        profiles.append(
            Profile(
                pid,
                string(r.get("owner_name"), "owner name", maximum=100),
                tuple(string(a, "recipient alias", maximum=100) for a in aliases),
                string(r.get("greeting"), "greeting", maximum=500),
                string(r.get("instructions"), "instructions"),
                lang,
                enabled,
                chat_id(r.get("telegram_chat_id"), "Telegram destination"),
            )
        )
    if len({p.id for p in profiles}) != len(profiles):
        raise ConfigError("Duplicate profile id")
    chats = [p.telegram_chat_id for p in profiles if p.telegram_chat_id]
    if len(chats) != len(set(chats)):
        raise ConfigError("Profiles must have distinct private Telegram destinations")
    routing = mapping(root.get("routing"), "routing")
    if routing.get("unresolved_action") != "ask_recipient":
        raise ConfigError("Unresolved route must ask_recipient")
    if routing.get("still_unresolved_action") != "hold_unassigned":
        raise ConfigError("Unassigned calls must be held")
    route_rows = root.get("ingress_routes", [])
    if not isinstance(route_rows, list):
        raise ConfigError("ingress_routes must be a list")
    routes = []
    for row in route_rows:
        r = mapping(row, "ingress route")
        pid = string(r.get("profile_id"), "route profile")
        if pid not in {p.id for p in profiles}:
            raise ConfigError("Route references unknown profile")
        original = r.get("original_called_number")
        if original is not None and not re.fullmatch(r"\+[1-9][0-9]{5,14}", str(original)):
            raise ConfigError("Route original_called_number must use E.164")
        validated = r.get("validated", False)
        if type(validated) is not bool:
            raise ConfigError("Route validated must be boolean")
        routes.append(
            IngressRoute(string(r.get("ingress_id"), "ingress id"), pid, original, validated)
        )
    return Family(
        tuple(profiles),
        tuple(routes),
        string(routing.get("unresolved_greeting"), "unresolved greeting"),
        money(budget.get("monthly_api_budget_eur"), "monthly budget"),
        timezone,
        duration,
    )


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                    raise ConfigError("Invalid env file syntax")
                values[key.strip()] = value.strip().strip("\"'")
        except (OSError, UnicodeError):
            raise ConfigError("Cannot read env file") from None
    return values | dict(os.environ)


@dataclass(frozen=True)
class Settings:
    family: Family
    state_dir: Path
    env: dict[str, str] = field(repr=False)

    @classmethod
    def load(cls, profiles: Path, env_file: Path = Path(".env")) -> Settings:
        env = load_env(env_file)
        return cls(load_family(profiles), Path(env.get("STATE_DIR", "./data")), env)

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not self.env.get(k)]
        if missing:
            raise ConfigError("Missing settings: " + ", ".join(missing))

    def validate_live(self, *, modem: bool = False) -> None:
        self.require("OPENAI_API_KEY", "REALTIME_MODEL", "REALTIME_VOICE")
        if self.env.get("AUDIO_RECORDING_ENABLED", "false").lower() != "false":
            raise ConfigError("Audio recording must be disabled")
        if modem:
            self.require("TELEGRAM_BOT_TOKEN", "MODEM_CONTROL_DEVICE", "MODEM_AUDIO_DEVICE")
            if not all(p.enabled and p.telegram_chat_id for p in self.family.profiles):
                raise ConfigError("Enable and enroll every configured profile before phone service")
