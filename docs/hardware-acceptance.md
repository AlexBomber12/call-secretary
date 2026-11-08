# Hardware acceptance (not yet performed)

`call-secretary modem-inspect` lists serial devices without opening any port. Only after identifying the dedicated new modem, use `modem-inspect --device /dev/serial/by-id/…` to issue bounded read-only queries. Linux requires `fuser` (psmisc) and an unclaimed, stable device path. Never select the existing SMS modem.

`call-secretary hardware-checklist` emits H1/H2 with every item explicitly untested. The inventory is private operational output, not something to paste into a public issue.

H1 requires actual incoming answer/hangup, simultaneous USB capture/playback and reconnection with the intended SIM/firmware. H2 compares all family forwarding paths, same/hidden caller ID, and evidence for the original called recipient. Missing original-recipient metadata selects the ask-recipient mode; a generic forwarding notification does not select a person.

The read-only probe does not answer a call or prove USB voice. The [phone runtime](server-operations.md) now uses the injected-port control core, exclusive hardware opener and isolated audio endpoints, but refuses device access without explicit H1 configuration. Real control/audio/forwarding acceptance remains pending. The USB rule is an uninstalled narrow template; no global ModemManager changes or USB resets are included.
