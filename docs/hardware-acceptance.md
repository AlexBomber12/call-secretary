# Hardware acceptance

## H1 bench results, 23 November 2026

The dedicated SIM7600E-H (`LE11B14SIM7600M22_211104`) registered on the intended SIM. The test used the verified USB AT interface `02` and PCM interface `04` at 8 kHz. The isolated test environment included the control fixes from [PR #24](https://github.com/AlexBomber12/call-secretary/pull/24) and [PR #25](https://github.com/AlexBomber12/call-secretary/pull/25).

| Check | Observed result |
|---|---|
| Incoming answer, recording, playback and release | Call Secretary answered the dedicated SIM, captured 250 frames from five seconds of speech and replayed an intelligible phrase. The application released the call. |
| Simultaneous capture and playback | On a repeat, the operator heard both test phrases clearly, including the second phrase spoken over playback of the first. In the previous run the second phrase was inaudible despite receiving the expected frame count. Keep this intermittent result in the pilot audio evaluation. |
| Local fallback and release | Missing API settings triggered the Italian eSpeak NG phrase. The operator heard it in full and reported synthetic-sounding speech. The phone disconnected automatically, the modem returned to idle and the test made zero provider connections. |
| USB reconnect | After physical removal the old AT channel closed. After plugging the modem into the same port, both USB interfaces returned and the SIM registered again. A fresh invocation of `modem-audio-test` recorded and replayed a phrase that the operator heard. |
| Second incoming and indexed release | The second handset received busy on a two-phone attempt. The first handset stayed connected after Call Secretary lost its active snapshot. A later trace kept showing the active call and did not show a waiting row. The network call-waiting query did not finish, so provisioning is unknown. Selective release with `AT+CHLD=1x` remains unverified on this firmware and SIM. |

**H1 remains open.** Verify that the network presents a waiting voice call on the dedicated line and that indexed release disconnects the owned active call while the waiting call remains unanswered. H2, live Realtime and Telegram delivery also remain open.

## Test procedure

`call-secretary modem-inspect` lists serial devices without opening any port. Only after identifying the dedicated new modem, use `modem-inspect --device /dev/serial/by-id/…` to issue bounded read-only queries. Linux requires `fuser` (psmisc) and an unclaimed, stable device path. Never select the existing SMS modem.

`call-secretary hardware-checklist` emits H1/H2 with every item explicitly untested. The inventory is private operational output, not something to paste into a public issue.

H1 requires actual incoming answer/hangup, simultaneous USB capture/playback and reconnection with the intended SIM/firmware. H2 compares all family forwarding paths, same/hidden caller ID, and evidence for the original called recipient. Missing original-recipient metadata selects the ask-recipient mode; a generic forwarding notification does not select a person.

The read-only modem inspector does not answer calls. On Ubuntu, use the active audio test with the phone service stopped and only the dedicated modem available for the test. Supply its stable AT and PCM device paths.

```sh
call-secretary modem-audio-test --control /dev/serial/by-path/AT_PORT --audio /dev/serial/by-path/PCM_PORT
```

The command verifies that AT interface 02 and PCM interface 04 belong to the same SIM7600. Call the dedicated SIM within 60 seconds, speak for five seconds when prompted and listen for the recorded phrase. Let the command end the call, and record whether the phrase was intelligible. Audio stays in memory. The command uses neither OpenAI nor Telegram and leaves the H1 setting to the operator.

Repeat on the intended firmware and check simultaneous speech, waiting-call handling, hangup, the local failure prompt and reconnection. Record the exact modem variant, firmware and paths. The [phone runtime](server-operations.md) requires explicit H1 configuration. The USB rule stays an uninstalled template targeting only the dedicated modem.
