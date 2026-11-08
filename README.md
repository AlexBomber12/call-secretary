# call-secretary

Self-hosted family voice secretary for calls forwarded to dedicated mobile SIM/USB modem endpoints. It takes a message in the caller's language and sends a private Telegram summary to the selected family member.

**Status: the software path is implemented, including a hardware-gated phone runtime, isolated USB audio, local fallback, health and deployment/rollback scripts. Offline tests do not certify the modem: real API/audio/Telegram acceptance, H1/H2 and installation on SERVER are still pending.**

After the [development install](docs/development.md), try the complete offline scenario:

```sh
call-secretary --profiles config/family.example.yaml simulate --recipient member_b --show-report
```

This makes no network requests: it uses a scripted transcript, an explicitly labelled original-text fallback and captured Telegram deliveries in a separate simulation database. It does not demonstrate live speech recognition or translation.

```text
Family mobile numbers -- busy / no answer --> SIM + USB modem
                                                      |
                                                Linux server
                                             voice API <--> caller
                                                      |
                                             recipient profile
                                                      |
                                           private Telegram summary
```

## MVP

- Answer forwarded calls, ask who is calling and what to pass on.
- Select a family profile by a validated ingress route or ask who the caller wants. Customize the Italian greeting, owner name and Telegram recipient per profile. Caller ID alone cannot identify the family member being called.
- Detect the caller's language; validate Italian, Russian and English first.
- Send caller ID when available, self-reported name, purpose, urgency and callback details only to the selected recipient. Russian is the default report language and can be customized per profile.
- Keep text and summaries without automatic expiry; do not persist call audio. Limit a call to 10 minutes.
- Share a EUR 50 monthly voice/API budget across the family; persist usage and reserve budget before opening a paid session.
- Recover from application/network restarts and retry pending notifications.

A shared single-modem pilot has one active voice call across the whole family. Automatic personalized greetings on a shared SIM depend on proving that the original called/redirecting number reaches the application; a generic forwarding flag is insufficient.

The target hardware is an original Waveshare SIM7600E-H 4G DONGLE. Voice service and bidirectional USB audio must be proven with the actual firmware, SIM and operator before the modem integration is accepted. The existing SMS modem remains independently managed.

## Documents

- [Contributor workflow, CI and review rules](AGENTS.md)
- [Development and offline checks](docs/development.md)
- [Implementation status](docs/progress.md)
- [Full code review, fixes and validation (Russian)](docs/review-2026-11-08.md)
- [User scenarios, verification matrix and remaining acceptance (Russian)](docs/user-scenarios.md)
- [Telephone runtime, hardware gate, SERVER installation and rollback](docs/server-operations.md)
- [Operator next steps (Russian)](docs/operator-next-steps.md)
- [Voice prototype](docs/voice-prototype.md)
- [SIM7600 control preparation and remaining H1 checks](docs/sim7600-control.md)
- [Telegram enrollment and delivery](docs/telegram-setup.md)
- [Goal and specification (Russian)](docs/spec.md)
- [Implementation plan (Russian)](docs/plan.md)
- [Planned PR scope and acceptance criteria (Russian)](docs/pr-breakdown.md)
- [Architecture and hardware acceptance](docs/architecture.md)
- [Family routing and private reports (Russian)](docs/family-routing.md)
- [Synthetic family configuration](config/family.example.yaml)
- [Configuration template](.env.example) — copy to a private `.env`; never commit credentials.

Production credentials, phone numbers, transcripts and host configuration stay outside this public repository. Public visibility does not select an open-source license; licensing is still to be decided.
