from __future__ import annotations

import argparse
import json
from pathlib import Path

from call_secretary.config import ConfigError, Settings


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Family call secretary")
    p.add_argument("--profiles", type=Path, default=Path("config/family.yaml"))
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="Validate settings without network requests")
    check.add_argument("--live", action="store_true")
    check.add_argument("--modem", action="store_true")
    sub.add_parser("status", help="Show readiness, never secrets or transcript contents")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings.load(args.profiles, args.env_file)
        if args.command == "check-config" and (args.live or args.modem):
            settings.validate_live(modem=args.modem)
        print(
            json.dumps(
                {
                    "configuration": "valid",
                    "profiles": len(settings.family.profiles),
                    "mode": "simulation",
                    "phone_service": "not_started",
                    "audio_recording": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
