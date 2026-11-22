# Hardware acceptance (not yet performed)

`call-secretary modem-inspect` lists serial devices without opening any port. Only after identifying the dedicated new modem, use `modem-inspect --device /dev/serial/by-id/…` to issue bounded read-only queries. Linux requires `fuser` (psmisc) and an unclaimed, stable device path. Never select the existing SMS modem.

`call-secretary hardware-checklist` emits H1/H2 with every item explicitly untested. The inventory is private operational output, not something to paste into a public issue.

H1 requires actual incoming answer/hangup, simultaneous USB capture/playback and reconnection with the intended SIM/firmware. H2 compares all family forwarding paths, same/hidden caller ID, and evidence for the original called recipient. Missing original-recipient metadata selects the ask-recipient mode; a generic forwarding notification does not select a person.

The read-only modem inspector does not answer calls. On Ubuntu, use the active audio test with the phone service stopped and only the dedicated modem available for the test. Supply its stable AT and PCM device paths.

```sh
call-secretary modem-audio-test --control /dev/serial/by-path/AT_PORT --audio /dev/serial/by-path/PCM_PORT
```

The command verifies that AT interface 02 and PCM interface 04 belong to the same SIM7600. Call the dedicated SIM within 60 seconds, speak for five seconds when prompted and listen for the recorded phrase. Let the command end the call, and record whether the phrase was intelligible. Audio stays in memory. The command uses neither OpenAI nor Telegram and leaves the H1 setting to the operator.

Repeat on the intended firmware and check simultaneous speech, waiting-call handling, hangup, the local failure prompt and reconnection. Record the exact modem variant, firmware and paths. The [phone runtime](server-operations.md) requires explicit H1 configuration. The USB rule stays an uninstalled template targeting only the dedicated modem.
