# Development

Use Python 3.12. Dependencies are fixed in `requirements.lock`; audio/modem imports are lazy, so configuration and simulation do not need audio hardware or a serial port.

```sh
python -m venv .venv
# Linux: . .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
call-secretary --profiles config/family.example.yaml check-config
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

The example uses synthetic profiles and has no Telegram destinations. Runtime configuration is `config/family.yaml`, ignored by Git; credentials belong in a private `.env` or process environment. `check-config --live` checks required voice settings without making a request. `--modem` additionally checks phone-service configuration; configuration validity alone does not prove hardware readiness.

Never put real transcripts, caller data, audio, keys or bot invitation links into a public issue, PR, test fixture or CI log. A clean install and automated tests use synthetic data and fake transports only. Live acceptance is recorded separately from unit/integration test results.
