import os
import subprocess
import sys


def test_report_cli_handles_russian_in_an_ascii_pipe(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "call_secretary",
            "--profiles",
            "config/family.example.yaml",
            "simulate",
            "--recipient",
            "member_b",
            "--state-dir",
            str(tmp_path / "simulation"),
            "--show-report",
        ],
        env=os.environ | {"PYTHONIOENCODING": "ascii"},
        capture_output=True,
    )
    assert result.returncode == 0
    assert "Звонок для: Persona B" in result.stdout.decode("utf-8")
