from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from decimal import Decimal
from pathlib import Path

from call_secretary.budget import BudgetError
from call_secretary.config import ConfigError, Settings


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Family call secretary")
    p.add_argument(
        "--profiles", type=Path, help="Defaults to PROFILES_CONFIG or config/family.yaml"
    )
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="Validate settings without network requests")
    check.add_argument("--live", action="store_true")
    check.add_argument("--modem", action="store_true")
    sub.add_parser("status", help="Show readiness, never secrets or transcript contents")
    sub.add_parser("phone-run", help="Run accepted dedicated hardware; requires explicit H1 gate")
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
    simulation.add_argument("--show-report", action="store_true")
    process = sub.add_parser("process-once", help="Process one durable summary job")
    process.add_argument(
        "--offline", action="store_true", help="Use a labelled original-text fallback"
    )
    invite = sub.add_parser("invite", help="Create a private one-time Telegram enrollment link")
    invite.add_argument("--profile", required=True)
    invite.add_argument("--bot-username", required=True)
    invite.add_argument("--ttl-seconds", type=int, default=3600)
    unbind = sub.add_parser("unbind", help="Revoke a profile binding and block unsent old reports")
    unbind.add_argument("--profile", required=True)
    worker = sub.add_parser("worker", help="Run Telegram enrollment, summaries and delivery")
    worker.add_argument("--offline-summaries", action="store_true")
    reconcile = sub.add_parser(
        "budget-reconcile", help="Reconcile one call against provider billing"
    )
    reconcile.add_argument("--call-id", required=True)
    reconcile.add_argument("--amount-eur", required=True, type=Decimal)
    backup = sub.add_parser("backup", help="Create a consistent private SQLite backup")
    backup.add_argument("--destination", required=True, type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
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
        if args.command == "phone-run":
            from call_secretary.phone import run_phone

            asyncio.run(run_phone(settings))
            return 0
        if args.command in {"invite", "unbind", "budget-reconcile", "backup"}:
            import re

            from call_secretary.budget import Ledger
            from call_secretary.storage import Store
            from call_secretary.telegram import Enrollment

            with Store(
                settings.state_dir, mode="live" if args.command in {"invite", "unbind"} else None
            ) as store:
                if args.command == "invite":
                    username = args.bot_username.lstrip("@")
                    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                        raise ConfigError("Invalid bot username")
                    token = Enrollment(store, settings.family).invite(
                        args.profile, ttl_seconds=args.ttl_seconds
                    )
                    print(
                        json.dumps(
                            {
                                "private_invitation": f"https://t.me/{username}?start={token}",
                                "expires_in_seconds": args.ttl_seconds,
                            }
                        )
                    )
                elif args.command == "unbind":
                    Enrollment(store, settings.family).unbind(args.profile)
                    print(json.dumps({"binding": "revoked"}))
                elif args.command == "backup":
                    store.backup(args.destination)
                    print(json.dumps({"backup": "created"}))
                else:
                    Ledger(
                        store, settings.family.monthly_budget_eur, settings.family.timezone
                    ).reconcile(args.call_id, args.amount_eur)
                    print(json.dumps({"budget": "reconciled"}))
            return 0
        if args.command == "worker":
            from call_secretary.budget import RateCard
            from call_secretary.locking import InstanceLock
            from call_secretary.storage import Store
            from call_secretary.summaries import OpenAISummary, SummaryWorker
            from call_secretary.telegram import TelegramClient, run_workers

            settings.require("TELEGRAM_BOT_TOKEN")
            worker_provider, worker_rates = None, None
            if not args.offline_summaries and settings.env.get("OPENAI_API_KEY"):
                worker_rates = RateCard.load(settings.env)
                worker_provider = OpenAISummary(settings.env["OPENAI_API_KEY"])

            async def work() -> None:
                client = TelegramClient(settings.env["TELEGRAM_BOT_TOKEN"])
                try:
                    with (
                        Store(settings.state_dir, mode="live") as store,
                        InstanceLock(store.path.parent / "worker.lock"),
                    ):
                        await run_workers(
                            store,
                            settings.family,
                            client,
                            SummaryWorker(
                                store, settings.family, provider=worker_provider, rates=worker_rates
                            ),
                        )
                finally:
                    await client.close()

            asyncio.run(work())
            return 0
        if args.command == "simulate":
            from call_secretary.calls import simulate
            from call_secretary.locking import InstanceLock
            from call_secretary.storage import Store
            from call_secretary.summaries import SummaryWorker
            from call_secretary.telegram import CapturedTelegram, DeliveryWorker, Enrollment

            with (
                Store(args.state_dir, mode="simulation") as store,
                InstanceLock(store.path.parent / "runtime.lock"),
            ):
                store.recover_calls()
                enrollment = Enrollment(store, settings.family)
                for index, profile in enumerate(settings.family.profiles, start=1000001):
                    bound = store.db.execute(
                        "SELECT 1 FROM bindings WHERE profile_id=?", (profile.id,)
                    ).fetchone()
                    if not bound:
                        enrollment.claim(enrollment.invite(profile.id), str(index))
                captured = CapturedTelegram()

                async def scenario() -> str:
                    cid = await simulate(store, settings.family, args.recipient, args.message)
                    worker = SummaryWorker(store, settings.family)
                    for _ in range(100):
                        if not await worker.process_one():
                            break
                    delivery = DeliveryWorker(store, settings.family, captured)
                    for _ in range(100):
                        if not await delivery.process_one():
                            break
                    return cid

                cid = asyncio.run(scenario())
                print(
                    json.dumps(
                        {
                            "call_id": cid,
                            "status": "simulated",
                            "network_requests": 0,
                            "captured_reports": len(captured.receipts),
                            **store.counts(),
                        }
                    )
                )
                if args.show_report:
                    row = store.db.execute(
                        "SELECT content FROM summaries WHERE call_id=?", (cid,)
                    ).fetchone()
                    if row:
                        print(json.loads(row[0])["report"])
            return 0
        if args.command == "process-once":
            from call_secretary.budget import RateCard
            from call_secretary.storage import Store
            from call_secretary.summaries import OpenAISummary, SummaryWorker

            provider, rates = None, None
            if not args.offline:
                settings.require("OPENAI_API_KEY")
                rates = RateCard.load(settings.env)
                provider = OpenAISummary(settings.env["OPENAI_API_KEY"])
            with Store(settings.state_dir, mode=None if args.offline else "live") as store:
                worker = SummaryWorker(store, settings.family, provider=provider, rates=rates)
                processed = asyncio.run(worker.process_one())
                print(json.dumps({"processed_summary": processed, **store.counts()}))
            return 0
        if args.command == "voice-prototype":
            from call_secretary.runtime import run_microphone

            cid = asyncio.run(run_microphone(settings, args.profile))
            print(json.dumps({"call_id": cid, "status": "finished"}))
            return 0
        if args.command == "check-config" and (args.live or args.modem):
            settings.validate_live(modem=args.modem)
            if args.modem:
                from call_secretary.storage import Store
                from call_secretary.summaries import destination

                if not settings.state_dir.exists():
                    raise ConfigError("Enroll Telegram recipients before phone service")
                with Store(settings.state_dir) as store:
                    if not all(destination(store, p) for p in settings.family.profiles):
                        raise ConfigError(
                            "Enroll every enabled family profile before phone service"
                        )
        if args.command == "status":
            from call_secretary.budget import Ledger
            from call_secretary.health import snapshot
            from call_secretary.storage import Store
            from call_secretary.summaries import destination

            state = {"calls": 0, "summary_pending": 0, "delivery_pending": 0}
            enrolled = 0
            budget_status = None
            health_status = None
            if settings.state_dir.exists():
                with Store(settings.state_dir) as store:
                    state = store.counts()
                    enrolled = sum(bool(destination(store, p)) for p in settings.family.profiles)
                    budget_status = Ledger(
                        store, settings.family.monthly_budget_eur, settings.family.timezone
                    ).status()
                    health_status = snapshot(store)
            print(
                json.dumps(
                    {
                        "phone_service": "hardware_gated",
                        "hardware_accepted_by_operator": settings.env.get("PHONE_H1_CONFIRMED")
                        == "true",
                        **state,
                        "profiles": len(settings.family.profiles),
                        "profiles_enabled": sum(p.enabled for p in settings.family.profiles),
                        "profiles_enrolled": enrolled,
                        "budget": budget_status,
                        "health": health_status,
                        "voice_key_configured": bool(settings.env.get("OPENAI_API_KEY")),
                        "telegram_token_configured": bool(settings.env.get("TELEGRAM_BOT_TOKEN")),
                    }
                )
            )
            return 0
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
    except (ConfigError, BudgetError) as exc:
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
