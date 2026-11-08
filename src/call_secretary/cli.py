from __future__ import annotations

import argparse
import asyncio
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
    modem = sub.add_parser("modem-inspect", help="List ports, or inspect one explicit device")
    modem.add_argument("--device")
    sub.add_parser("hardware-checklist", help="Print unverified H1/H2 acceptance checklist")
    prototype = sub.add_parser("voice-prototype", help="Paid microphone test, no phone modem")
    prototype.add_argument("--profile", help="Omit to ask the caller for a family recipient")
    sub.add_parser("audio-devices", help="List local microphone/speaker devices")
    simulation = sub.add_parser("simulate", help="Run a synthetic call without network or audio")
    simulation.add_argument("--recipient")
    simulation.add_argument("--message", default="Please call me back tomorrow.")
    simulation.add_argument("--state-dir", type=Path, default=Path("data-simulation"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "audio-devices":
            from call_secretary.audio import devices

            print(devices())
            return 0
        if args.command in {"modem-inspect", "hardware-checklist"}:
            from call_secretary.modem import hardware_checklist, inspect, inventory

            if args.command == "hardware-checklist":
                result = hardware_checklist()
            else:
                result = inspect(args.device) if args.device else {"ports": inventory()}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        settings = Settings.load(args.profiles, args.env_file)
        if args.command == "simulate":
            from call_secretary.calls import simulate
            from call_secretary.locking import InstanceLock
            from call_secretary.storage import Store

            with Store(args.state_dir) as store, InstanceLock(store.path.parent / "runtime.lock"):
                store.recover_calls()
                cid = asyncio.run(simulate(store, settings.family, args.recipient, args.message))
                print(json.dumps({"call_id": cid, "status": "simulated", **store.counts()}))
            return 0
        if args.command == "voice-prototype":
            from call_secretary.runtime import run_microphone

            cid = asyncio.run(run_microphone(settings, args.profile))
            print(json.dumps({"call_id": cid, "status": "finished"}))
            return 0
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
    except Exception:
        print(json.dumps({"error": "Operation failed; check configuration and retained state"}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
