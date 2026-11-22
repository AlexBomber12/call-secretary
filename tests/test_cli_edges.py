import asyncio
import io
import json
import runpy
import sys

import pytest

from call_secretary import cli, modem, runtime, telegram
from call_secretary.budget import Ledger
from call_secretary.calls import simulate
from call_secretary.storage import Store
from call_secretary.telegram import Enrollment


@pytest.fixture
def command(settings, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda *args: settings)
    return lambda *args: cli.main(list(args))


def test_cli_device_commands_do_not_need_family_or_open_devices(command, monkeypatch, capsys):
    from call_secretary import audio

    monkeypatch.setattr(audio, "devices", lambda: "synthetic-device")
    monkeypatch.setattr(modem, "inventory", lambda: [])
    monkeypatch.setattr(modem, "inspect", lambda device: {"model": "synthetic"})
    assert command("audio-devices") == 0
    assert command("modem-inspect") == 0
    assert command("modem-inspect", "--device", "synthetic") == 0
    assert command("hardware-checklist") == 0
    assert "not_tested" in capsys.readouterr().out


def test_cli_enrollment_revocation_backup_and_reconciliation(command, settings, tmp_path, capsys):
    assert command("invite", "--profile", "member_a", "--bot-username", "@family_test_bot") == 0
    link = json.loads(capsys.readouterr().out)["private_invitation"]
    assert link.startswith("https://t.me/family_test_bot?start=v1_")
    assert command("invite", "--profile", "member_a", "--bot-username", "bad!") == 2
    assert command("unbind", "--profile", "member_a") == 0
    assert command("backup", "--destination", str(tmp_path / "backup.sqlite3")) == 0
    with Store(settings.state_dir) as store:
        cid = store.create_call("synthetic")
        from decimal import Decimal

        Ledger(store, settings.family.monthly_budget_eur).reserve(cid, Decimal(1))
        store.end_call(cid)
        job = store.claim_job("summary_jobs")
        store.complete_summary(cid, job["lease_token"], {"report": "synthetic final report"}, None)
    assert command("budget-reconcile", "--call-id", cid, "--amount-eur", "0.25") == 0
    with Store(settings.state_dir) as store:
        assert Ledger(store, settings.family.monthly_budget_eur).status()["accounted_eur"] == "0.25"


def test_cli_phone_readiness_checks_every_private_destination(command, settings, capsys):
    assert command("check-config", "--live") == 0
    assert command("check-config", "--modem") == 2
    settings.env.update(
        {
            "PHONE_H1_CONFIRMED": "true",
            "MODEM_AUDIO_RATE": "8000",
            "PHONE_INGRESS_ID": "dedicated",
        }
    )
    assert command("check-config", "--modem") == 2
    with Store(settings.state_dir):
        pass
    assert command("check-config", "--modem") == 2
    with Store(settings.state_dir) as store:
        enrollment = Enrollment(store, settings.family)
        for i, p in enumerate(settings.family.profiles, 101):
            enrollment.claim(enrollment.invite(p.id), str(i))
    assert command("check-config", "--modem") == 0
    assert command("status") == 0
    assert "synthetic-key" not in capsys.readouterr().out


def test_worker_preflight_checks_effective_state_path_and_local_token_format(
    command,
    settings,
    tmp_path,
):
    root = str(settings.state_dir)
    assert command("check-config", "--worker", "--expect-state-dir", root) == 0
    assert command("check-config", "--worker", "--expect-state-dir", str(tmp_path / "other")) == 2
    settings.env["TELEGRAM_BOT_TOKEN"] = "unsafe/path"
    assert command("check-config", "--worker", "--expect-state-dir", root) == 2
    settings.env["TELEGRAM_BOT_TOKEN"] = "synthetic-token"
    settings.env["ADMIN_TELEGRAM_CHAT_ID"] = "-100000"
    assert command("check-config", "--worker") == 2


def test_worker_preflight_rejects_simulation_state_without_relabelling(command, settings):
    with Store(settings.state_dir, mode="simulation"):
        pass
    assert command("check-config", "--worker") == 2
    with Store(settings.state_dir, mode="simulation"):
        pass


def test_worker_preflight_keeps_existing_unregistered_live_state_unmodified(
    command,
    settings,
):
    with Store(settings.state_dir) as store:
        store.create_call("synthetic")
    assert command("check-config", "--worker") == 0
    with Store(settings.state_dir) as store:
        assert not store.db.execute("SELECT value FROM metadata WHERE key='state_mode'").fetchone()


def test_worker_preflight_rejects_a_state_database_symlink(command, monkeypatch):
    from pathlib import Path

    is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda value: value.name == "secretary.sqlite3" or is_symlink(value)
    )
    assert command("check-config", "--worker") == 2


def test_worker_preflight_refuses_a_conflicting_explicit_chat_without_changing_binding(
    settings,
    monkeypatch,
):
    from dataclasses import replace

    with Store(settings.state_dir, mode="live") as store:
        enrollment = Enrollment(store, settings.family)
        enrollment.claim(enrollment.invite("member_a"), "101")
    first, *rest = settings.family.profiles
    family = replace(settings.family, profiles=(replace(first, telegram_chat_id="102"), *rest))
    monkeypatch.setattr(cli.Settings, "load", lambda *args: replace(settings, family=family))
    assert cli.main(["check-config", "--worker"]) == 2
    with Store(settings.state_dir, mode="live") as store:
        assert store.db.execute("SELECT chat_id FROM bindings").fetchone()[0] == "101"


def test_cli_status_without_state_and_process_without_jobs(command, settings, capsys):
    assert command("status") == 0
    assert json.loads(capsys.readouterr().out)["billing_review"]["state"] == "current"
    assert command("process-once", "--offline") == 0
    assert command("process-once") == 0


@pytest.mark.parametrize("offline", [True, False])
def test_cli_worker_always_closes_its_client(command, settings, monkeypatch, offline):
    clients = []

    class Client:
        def __init__(self, token):
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    async def work(store, family, client, worker, *, operator_chat):
        assert bool(worker.provider) is not offline

    monkeypatch.setattr(telegram, "TelegramClient", Client)
    monkeypatch.setattr(telegram, "run_workers", work)
    assert command("worker", *(["--offline-summaries"] if offline else [])) == 0
    assert clients[0].closed


def test_cli_voice_and_interruption_return_codes(command, monkeypatch):
    async def voice(settings, profile):
        assert profile == "member_a"
        return "synthetic-call"

    monkeypatch.setattr(runtime, "run_microphone", voice)
    assert command("voice-prototype", "--profile", "member_a") == 0

    async def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "run_microphone", interrupt)
    assert command("voice-prototype") == 130
    monkeypatch.setattr(modem, "inventory", lambda: 1 / 0)
    assert command("modem-inspect") == 2


def test_simulation_is_bounded_with_backlog_and_can_be_repeated(command, settings, tmp_path):
    path = tmp_path / "simulation"

    async def populate():
        with Store(path, mode="simulation") as store:
            for _ in range(101):
                await simulate(store, settings.family, "member_a", "synthetic backlog")

    asyncio.run(populate())
    # New call remains after the bounded 100-job drain; missing report is expected.
    assert (
        command("simulate", "--recipient", "member_a", "--state-dir", str(path), "--show-report")
        == 0
    )
    assert (
        command("simulate", "--recipient", "member_a", "--state-dir", str(path), "--show-report")
        == 0
    )
    with Store(path) as store:
        assert store.counts()["summary_pending"] == 0
    assert command("simulate", "--recipient", "member_a", "--state-dir", str(path)) == 0


def test_cli_entrypoint_accepts_non_file_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "call-secretary",
            "--profiles",
            "config/family.example.yaml",
            "--env-file",
            str(tmp_path / "absent"),
            "check-config",
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_path("src/call_secretary/cli.py", run_name="__main__")
    assert result.value.code == 0
