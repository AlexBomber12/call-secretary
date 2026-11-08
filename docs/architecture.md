# Architecture

## Proposed components

- **Python 3.12 / asyncio** service on Ubuntu 24.04 x86_64.
- **Modem adapter:** dedicated SIM7600 control and audio interfaces. Resolve stable device identities; do not assume `/dev/ttyUSB0` belongs to this application.
- **Family routing:** resolve an approved recipient profile before a personalized greeting. Separate caller identity, original called/redirecting number and local modem/line identity. Prefer a dedicated ingress-to-profile mapping or original called/redirecting metadata validated for the actual carrier path. On a shared unresolved line ask the caller for the intended recipient; never infer the recipient from caller ID. See [family routing](family-routing.md).
- **Voice adapter:** a configurable realtime speech API over an outbound connection. OpenAI Realtime is the initial candidate; select the actual model/voice after listening and measuring cost. Confirm the current API before implementing its events.
- **Call controller:** one active call, explicit state transitions, interruption/cancellation, independent duration and budget limits, local failure prompt.
- **SQLite:** call text, structured summaries and a durable notification outbox. Each call has a recipient profile (nullable until resolved), route evidence and route source; every outbox entry stores its resolved recipient and destination. Persist call/session identity before attempting delivery. Keep text indefinitely unless explicitly deleted. Never store raw call audio or raw provider event dumps.
- **Telegram client:** one bot can send to multiple private chats via per-profile `chat_id`; each recipient must initiate contact first. Enroll only owner-approved profiles. Retry/backoff includes rate-limit responses. Unresolved calls do not broadcast their contents; send only a content-free operator alert and keep the call unassigned for explicit review. Do not discover or reuse another application's secrets automatically.
- **Budget controller:** EUR 50 per calendar month across the family, Europe/Rome month boundaries, persisted usage and reservations before paid sessions. Account for concurrent channels if added later; do not reset usage on service restart. Verify provider prices and currency conversion before the pilot; reconcile estimates with provider billing.
- **Deployment:** dedicated systemd phone/worker services, private persistent state and narrowly scoped USB permissions after H1. Native audio lives in killable child processes; all services remain inactive until explicit activation. Docker is used only for disposable CI installation tests, not the selected production deployment. No public HTTP endpoint is required. See [SERVER operations](server-operations.md).

## Hardware acceptance gate

The selected USB dongle is a candidate, not a validated voice gateway. Record its exact model and firmware, then prove on the actual operator/SIM:

1. Network registration and incoming voice availability, including the working radio/voice mode.
2. Answer and hangup control, caller ID and call-ended events.
3. Simultaneous capture and playback of call audio over USB; measure the actual format, levels and latency. Resample only after confirming both device and API requirements.
4. Caller ID and original called/redirecting metadata after conditional forwarding from every family carrier path. Validate routing with the same external caller calling two family numbers, hidden caller ID and absent metadata. A forwarding notification alone does not establish the intended family member.
5. Device removal/reconnection without resetting or claiming unrelated modems.

The target host already has a separate modem workload. Inspect ownership of the new device before assigning it. Prefer a dedicated modem for calls. Any ModemManager exclusion must be scoped to the new device and verified against the existing workload. Do not disable ModemManager globally, reuse another application's modem, or issue broad USB resets.

## Data and operation

Audio lives in bounded memory buffers during calls and is streamed to the configured voice provider. No application audio files or audio payload logs are retained. Text and summaries remain private server state with no automatic expiry; the summary goes only to the selected profile's Telegram chat in its configured language (Russian by default). Profile-scoped access prevents family members from reading another profile's reports through the bot. Provider-side handling is separate from local retention and must be reviewed when choosing the API.

The Italian greeting says only "assistente": `Pronto, sono l'assistente di [nome]. Cosa posso riferire?` The unresolved-route greeting is `Pronto, sono l'assistente. Per chi chiama?` Both are configurable. If asked about its nature, the assistant answers truthfully and does not claim to be human.

Keep runtime `.env`, API credentials, real phone numbers, transcripts and host-specific deployment inventory outside Git. Public examples use synthetic data. Start with outbound network access only. Monitor modem registration, voice connection and outbox separately: a running process alone does not prove call readiness.

## References for implementation

- [Waveshare SIM7600 voice-call guide](https://www.waveshare.com/wiki/SIM7600X_Raspberry_Pi_Raspbian_Voice_Call)
- [SIMCom USB audio application note (Waveshare mirror)](https://files.waveshare.com/upload/8/8e/SIM7100_SIM7500_SIM7600_Series_USB_AUDIO_Application_Note_V1.03.pdf)
- [SIMCom AT manual V3.00: CLIP and CSSN (Waveshare mirror)](https://files.waveshare.com/wiki/SIM7600G-H/SIM7500_SIM7600_Series_AT_Command_Manual_V3.00.pdf)
- [OpenAI realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Telegram Bot API](https://core.telegram.org/bots/api)
