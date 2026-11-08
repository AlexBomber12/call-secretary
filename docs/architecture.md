# Architecture

## Proposed components

- **Python 3.12 / asyncio** service on Ubuntu 24.04 x86_64.
- **Modem adapter:** dedicated SIM7600 control and audio interfaces. Resolve stable device identities; do not assume `/dev/ttyUSB0` belongs to this application.
- **Voice adapter:** a configurable realtime speech API over an outbound connection. OpenAI Realtime is the initial candidate; select the actual model/voice after listening and measuring cost. Confirm the current API before implementing its events.
- **Call controller:** one active call, explicit state transitions, interruption/cancellation, independent duration and budget limits, local failure prompt.
- **SQLite:** call text, structured summaries and a durable notification outbox. Persist call/session identity before attempting delivery. Never store raw call audio or raw provider event dumps.
- **Telegram client:** outbound Bot API requests; retry/backoff including rate-limit responses. Use an explicit destination; do not discover or reuse another application's secrets automatically.
- **Deployment:** plan for an isolated Docker Compose service with a persistent state directory, restart policy and only the required device access. Validate USB hotplug/reconnect behavior before selecting final container device mappings. No public HTTP endpoint is required for the MVP.

## Hardware acceptance gate

The selected USB dongle is a candidate, not a validated voice gateway. Record its exact model and firmware, then prove on the actual operator/SIM:

1. Network registration and incoming voice availability, including the working radio/voice mode.
2. Answer and hangup control, caller ID and call-ended events.
3. Simultaneous capture and playback of call audio over USB; measure the actual format, levels and latency. Resample only after confirming both device and API requirements.
4. Caller ID and call behavior after conditional forwarding from the main number.
5. Device removal/reconnection without resetting or claiming unrelated modems.

The target host already has a separate modem workload. Inspect ownership of the new device before assigning it. Prefer a dedicated modem for calls. Any ModemManager exclusion must be scoped to the new device and verified against the existing workload. Do not disable ModemManager globally, reuse another application's modem, or issue broad USB resets.

## Data and operation

Audio lives in bounded memory buffers during calls and is streamed to the configured voice provider. No application audio files or audio payload logs are retained. Text and summaries remain private server state; the Russian summary goes to the selected Telegram chat. Provider-side handling is separate from local retention and must be reviewed when choosing the API.

Keep runtime `.env`, API credentials, real phone numbers, transcripts and host-specific deployment inventory outside Git. Public examples use synthetic data. Start with outbound network access only. Monitor modem registration, voice connection and outbox separately: a running process alone does not prove call readiness.

## References for implementation

- [Waveshare SIM7600 voice-call guide](https://www.waveshare.com/wiki/SIM7600X_Raspberry_Pi_Raspbian_Voice_Call)
- [SIMCom USB audio application note (Waveshare mirror)](https://files.waveshare.com/upload/8/8e/SIM7100_SIM7500_SIM7600_Series_USB_AUDIO_Application_Note_V1.03.pdf)
- [OpenAI realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Telegram Bot API](https://core.telegram.org/bots/api)
