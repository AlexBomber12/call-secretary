"""The same offline CI gate on Windows and Linux."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        ["mypy"],
        ["pytest", "-q", "--cov", "--cov-report=term-missing", "--cov-report=xml"],
        ["call_secretary", "--profiles", "config/family.example.yaml", "check-config"],
    ]
    for command in commands:
        print("+ python -m " + " ".join(command), flush=True)
        result = subprocess.run([sys.executable, "-m", *command], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
