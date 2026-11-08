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
    assert command("budget-reconcile", "--call-id", cid, "--amount-eur", "0.25") == 0
    with Store(settings.state_dir) as store:
        assert Ledger(store, settings.family.monthly_budget_eur).status()["accounted_eur"] == "0.25"


def test_cli_phone_readiness_checks_every_private_destination(command, settings, capsys):
    assert command("check-config", "--live") == 0
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


def test_cli_status_without_state_and_process_without_jobs(command, settings):
    assert command("status") == 0
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

    async def work(store, family, client, worker):
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
