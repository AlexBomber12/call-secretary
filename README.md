# call-secretary

Self-hosted family voice secretary for calls forwarded to dedicated mobile SIM/USB modem endpoints. It takes a message in the caller's language and sends a private Telegram summary to the selected family member.

**Status: project specification. The application is not implemented or deployed yet.**

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

- [Goal and specification (Russian)](docs/spec.md)
- [Implementation plan (Russian)](docs/plan.md)
- [Planned PR scope and acceptance criteria (Russian)](docs/pr-breakdown.md)
- [Architecture and hardware acceptance](docs/architecture.md)
- [Family routing and private reports (Russian)](docs/family-routing.md)
- [Synthetic family configuration](config/family.example.yaml)
- [Configuration template](.env.example) — proposed settings; no executable currently reads them.

Production credentials, phone numbers, transcripts and host configuration stay outside this public repository. Public visibility does not select an open-source license; licensing is still to be decided.
