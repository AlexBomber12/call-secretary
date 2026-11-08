# call-secretary

Self-hosted voice secretary for calls forwarded to a dedicated mobile SIM and USB modem. It takes a message in the caller's language and sends a Russian summary to Telegram.

**Status: project specification. The application is not implemented or deployed yet.**

```text
Main mobile number -- busy / no answer --> spare SIM + USB modem
                                                      |
                                                Linux server
                                             voice API <--> caller
                                                      |
                                               Telegram summary
```

## MVP

- Answer forwarded calls, ask who is calling and what to pass on.
- Detect the caller's language; validate Italian, Russian and English first.
- Send caller ID when available, self-reported name, purpose, urgency and callback details to Telegram in Russian.
- Keep text and summaries; do not persist call audio. Limit a call to 10 minutes.
- Recover from application/network restarts and retry pending notifications.

The target hardware is an original Waveshare SIM7600E-H 4G DONGLE. Voice service and bidirectional USB audio must be proven with the actual firmware, SIM and operator before the modem integration is accepted. The existing SMS modem remains independently managed.

## Documents

- [Goal and specification (Russian)](docs/spec.md)
- [Implementation plan (Russian)](docs/plan.md)
- [Architecture and hardware acceptance](docs/architecture.md)
- [Configuration template](.env.example) — proposed settings; no executable currently reads them.

Production credentials, phone numbers, transcripts and host configuration stay outside this public repository. Public visibility does not select an open-source license; licensing is still to be decided.
