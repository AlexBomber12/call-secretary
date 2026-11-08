# Voice prototype

The microphone command is implemented but live API/audio acceptance has not been performed. It uses the nested Realtime API configuration, PCM16 mono 24 kHz, client-controlled bounded responses, playback cancellation and explicit truncation after interruption.

1. Install [development dependencies](development.md). On Linux a physical audio device needs PortAudio (`libportaudio2`).
2. In private `.env`, set `OPENAI_API_KEY`, `REALTIME_MODEL` (`gpt-realtime-2.1` or `gpt-realtime-2.1-mini`), `REALTIME_VOICE` (initial candidate `marin`), `BILLING_REVIEWED_ON` after checking current tariffs, and a conservative `USD_TO_EUR_UPPER_BOUND` accounting multiplier. Keys never belong in chat or Git.
3. Enable one profile in private `config/family.yaml`. The microphone prototype does not require a Telegram destination.
4. Run `call-secretary check-config --live`, then `call-secretary voice-prototype --profile <profile-id>`. Ctrl+C stops the session. This command incurs API charges; CI does not run it.

The 10-minute timeout is independent of the model. Every response has a token limit; the conversation has a bounded context and response count. Audio is kept in bounded memory only. Text and unfinished summary tasks remain in the private database. The ledger reserves before connection and retains unknown usage after interruption; operator reconciliation is needed if billing is incomplete. Transcription uses a conservative accounting allowance, not an exact billed amount.

Before accepting live voice, check IT/RU/EN, the voice, actual playback/truncation latency, device errors and usage. Automated tests do not establish microphone or phone quality.

Native capture/playback now run in independently killable spawned processes. Timeout/cancellation reaps a hung worker instead of leaving a PortAudio thread behind. See [isolation and operational limits](server-operations.md). This changes the failure boundary; it does not certify a real microphone or USB endpoint.

Implementation sources reviewed 2026-11-08: [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations), [client events](https://developers.openai.com/api/reference/resources/realtime/client-events), [pricing](https://developers.openai.com/api/docs/pricing), [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
