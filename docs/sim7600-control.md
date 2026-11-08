# SIM7600 control core: offline preparation

`sim7600.py` accepts an injected serial object. It does **not** open a device, start phone service, connect Realtime, or demonstrate H1. The existing read-only modem inspector is unchanged.

The command contract follows the manufacturer's [SIM7500/SIM7600 AT manual v3.00](https://files.waveshare.com/wiki/SIM7600G-H/SIM7500_SIM7600_Series_AT_Command_Manual_V3.00.pdf): sections 2.2.6 (answer), 5.2.2 (voice hangup), and 5.2.7 (current-call snapshots). Call control disables automatic CLCC notifications before requesting snapshots. It supports one incoming, non-conference voice call; existing active, outgoing, data, held, waiting or multiple calls are refused.

The channel reads bounded blocks and separates response lines from unsolicited events. A definite rejection preserves synchronization. A timeout, disconnect, malformed encoding, short write or buffer overflow closes the channel; an uncertain answer/hangup is never retried on it. Reconnection requires a new channel/controller and a fresh snapshot, without adopting an existing active call or resetting USB.

Each observed incoming call has a token. Answer/hangup recheck the snapshot and token; repeated RING preserves it, while an observed replacement invalidates it. An end hint alone does not end a call that the snapshot still shows active. Caller metadata is kept out of representations and is never interpreted as the original family recipient.

The future hardware opener must verify the dedicated SIM7600, claim its stable port exclusively, configure bounded read/write timeouts (at most 0.1/1 seconds), and ensure a clean command stream. Run this blocking adapter outside the asyncio event loop. The full runtime must confirm the post-answer snapshot before starting a fresh conversation. These pieces and the USB voice path remain pending.

Snapshots and global modem commands are not an atomic compare-and-swap. Identical reused call IDs with missing transition events cannot prove a call boundary. H1 must inspect actual firmware events, timing, hangup and reconnection behavior before automatic answering is enabled. A passing fake-port test does not establish firmware voice support, Vodafone routing, USB audio format, or H2 metadata.
