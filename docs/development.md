# Development

Use Python 3.12. Dependencies are fixed in `requirements.lock`; audio/modem imports are lazy, so configuration and simulation do not need audio hardware or a serial port.

```sh
python -m venv .venv
# Linux: . .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
call-secretary --profiles config/family.example.yaml check-config
call-secretary --profiles config/family.example.yaml simulate --recipient member_b --show-report
python scripts/ci.py
# Equivalent on Linux/Git Bash: bash scripts/ci.sh
```

The example uses synthetic profiles and has no Telegram destinations. Runtime configuration is `config/family.yaml`, ignored by Git; credentials belong in a private `.env` or process environment. `check-config --live` checks required voice settings without making a request. `--modem` additionally checks phone-service configuration; configuration validity alone does not prove hardware readiness.

Never put real transcripts, caller data, audio, keys or bot invitation links into a public issue, PR, test fixture or CI log. A clean install and automated tests use synthetic data and fake transports only. Live acceptance is recorded separately from unit/integration test results.

The complete gate runs Ruff lint/format, strict mypy, pytest with **100% line and branch coverage across every production Python module**, and offline configuration validation. GitHub Actions runs the same gate on Windows and Linux. Audio, serial, WebSocket and HTTP paths are exercised with isolated fakes; no tests answer calls, use API credentials or send messages.

Follow the root [AGENTS.md](../AGENTS.md), adapted from Almost Reasonable and Infrastructure Inspector. After publication, inspect Actions and Codex Review for the current PR HEAD, fix actionable P1/P2 feedback on the same branch, and repeat validation after every push. A generic completed status is not review approval. Merge the exact validated HEAD without a separate confirmation, then verify main and continue the approved work. An explicit user instruction to stop before merge takes precedence; there is no external merge daemon.

API responses are streamed with decoded-body limits and a 30-second overall deadline. Budget reconciliation requires the call and runnable summary work to have finished. Temporarily disabled destinations wait for re-enablement; explicit revocation remains blocked. See the [full review report](review-2026-11-08.md) for reproduced failures and acceptance limits.
