# Telephone runtime and SERVER operations

The software path is implemented and tested offline. **H1/H2 and deployment on the real SERVER are not accepted.** Nothing in this change installs services, opens a device, contacts Telegram or spends API credit automatically.

## The seven formerly missing scenarios

| Scenario | Implemented now | Still requires the actual machine/modem |
|---|---|---|
| US-O10 | Immutable release staging, systemd phone/worker units, private directories, explicit activation, consistent backup and release rollback | Target package install, USB permissions, reboot/update/rollback acceptance |
| US-O11 | Content-free modem/audio/voice/Telegram observations with timestamps, stale detection and queues grouped by state in `status` | Observe real device/network failures; idle is not an API connectivity probe |
| US-O12 | Spawned, independently killable capture/playback workers; bounded IPC; timeout/cancellation kills and reaps hung native work; systemd kills the entire cgroup on stop | Native PortAudio/device stress and reconnect checks. The real OS-process kill path is tested without audio hardware |
| US-H09 | Explicit unique capture/playback names, mono PCM16 at 8/16/24/48 kHz, streaming conversion to/from Realtime 24 kHz, interruptable output | Verify actual USB interfaces, rate, simultaneous streams, latency and sound quality |
| US-H10 | `phone-run`: SIM7600 opener, call polling/ownership, Realtime/controller, private history and durable summary/outbox workers | A forwarded call on the actual Vodafone SIM; shared-line original-called metadata remains unproven |
| US-H11 | Fixed Italian local prompt via espeak-ng, prepared in memory before modem use; API/key/rate/budget failure uses the prompt and hangup | Listen to prompt on the phone and verify network-side disconnection |
| US-H12 | Remote hangup/replacement watcher, cancellation, ownership recheck before hangup, partial-text persistence, retained unknown budget, modem reopen with backoff and SIGTERM cleanup | USB loss/return, caller hangup, network outage and process restart on real equipment |

## Configuration gate

Until H1 is recorded, keep `PHONE_H1_CONFIRMED=false`. `phone-run` refuses before opening hardware or generating speech. The flag is an operator declaration, not an automatic hardware test or proof of H1. It never starts the service on its own.

After H1, set these in the private environment file:

```dotenv
STATE_DIR=/var/lib/call-secretary
PHONE_H1_CONFIRMED=true
PHONE_INGRESS_ID=dedicated-family-modem
MODEM_CONTROL_DEVICE=/dev/serial/by-id/REPLACE_WITH_ACCEPTED_INTERFACE
MODEM_AUDIO_DEVICE=REPLACE_WITH_EXACT_UNIQUE_SOUNDDEVICE_NAME
MODEM_AUDIO_INPUT=
MODEM_AUDIO_OUTPUT=
MODEM_AUDIO_RATE=8000
```

The rate is an **example**, not an assumed SIM7600 capability. Record the actual mono signed PCM16 rate in H1. Optional input/output names override the common name. Matching is exact and must identify one input/output endpoint; there is no default-device fallback for phone service. Separate capture/playback streams must work simultaneously. Serial access uses the existing stable-device/ownership check and exclusive opening. A model probe rejects a non-SIM7600 before any call-control command.

No modem-specific USB-audio activation/reset commands are guessed or sent. If firmware needs activation or different audio transport, establish it during H1 and update the driver before enabling service. This may require a hardware-driven change. Original called numbers are never fabricated from CLCC. Leave shared-line automatic routes absent until H2; the assistant asks who the call is for.

Phone service requires an enabled/enrolled recipient and a configured Telegram bot. Missing/stale voice configuration or exhausted budget uses the local prompt without a paid request. At most one call is accepted. Voice capture starts only after the Realtime session acknowledgement; no call audio is saved.

## Audio isolation and local prompt

Each native direction runs in a spawned process, not the service thread pool. IPC carries at most one small request per lane. Normal operations time out after 2 seconds, opening after 8 seconds. Failure/cancellation terminates the lane, escalates to kill and reaps it. A process that cannot be reaped is fatal so the service supervisor restarts the cgroup. Full cleanup is attempted for both directions. The microphone prototype also uses these workers.

The converter uses Python 3.12 `audioop.ratecv` state across frames and resets output state on interruption. Python is pinned below 3.13 because audioop is removed there. This is a transport implementation, not a claim of measured acoustic quality. [Python audioop documentation](https://docs.python.org/3.12/library/audioop.html), [sounddevice raw-stream API](https://python-sounddevice.readthedocs.io/en/0.5.3/api/raw-streams.html), [multiprocessing lifecycle](https://docs.python.org/3.12/library/multiprocessing.html).

`espeak-ng --stdout -v it` renders only a fixed phrase: “Mi dispiace, al momento non posso prendere un messaggio. Riprovi più tardi.” Its WAV/PCM stays in memory, is validated/bounded and is prepared before opening the modem. No conversation text is passed to a local speech executable. Core dumps are disabled in the units; audio workers do not receive credentials as arguments. They use byte packets, not unpickled external events.

The ten-minute timer includes audio opening, answering, API connection and dialogue. The caller/replacement watcher remains active during the local phrase. Cleanup/hangup adds bounded control/worker latency; measure the actual network-side cutoff during H1. A crash/power loss cannot guarantee an immediate physical hangup: after restart, an already active unowned call is refused until it ends, rather than adopted or globally disconnected. Summary recovery retains its partial text and unknown bill.

## Stage installation, without activating phone service

The scripts are for Ubuntu with systemd, **not for execution on the current Windows development host**. Use a reviewed public source checkout/sdist containing `deploy/`. Nothing reads private vault material or the SMS gateway configuration.

On the target, install Python 3.12/venv, libportaudio2, espeak-ng and psmisc using the target's package manager. Then run the reviewed script as root:

```sh
bash deploy/install-server.sh /path/to/public-source RELEASE_ID
```

It stages `/opt/call-secretary/releases/RELEASE_ID`, installs the pinned dependencies and package, creates a dedicated service account/private directories and installs only this application's two systemd units. It does not start or enable either unit, install udev rules or change existing modem services. It does not copy source `.env`, family YAML, recordings, state or arbitrary files. Release IDs cannot include paths and existing releases are not overwritten. Review source before running any installer as root.

Create `/etc/call-secretary/family.yaml` and `/etc/call-secretary/secretary.env` from public examples, owned by root:call-secretary with mode 0640; directory mode 0750. Explicitly use `STATE_DIR=/var/lib/call-secretary`. Keep real credentials and family data out of release directories. Use the staged release executable as the service user for invitations; see [Telegram setup](telegram-setup.md). Fill all `.env` key fields explicitly, even blank, to avoid inheriting a different global project key.

The udev file is an **uninstalled template**. During H1, verify a unique parent USB serial across every relevant interface before substituting it. Match only the dedicated modem, grant its tty/sound nodes to the service group, and set ModemManager ignore only for that device. Reload rules and replug only that modem. Do not disable ModemManager globally or change/reset the existing SMS device. If the serial is absent/nonunique, design and verify a different narrow match before installing a rule.

## Activation, upgrade and rollback

After private configuration and enrollment, activate only the worker if desired:

```sh
bash deploy/activate-release.sh RELEASE_ID
```

After H1, explicitly include the phone service:

```sh
bash deploy/activate-release.sh RELEASE_ID --phone
```

Activation validates configuration, stops only this application's services, backs up an existing state database as its service user, switches the `current` release symlink and enables/starts the requested units. Never enable family forwarding before actual acceptance. The local gate does not constitute H1.

For rollback, invoke the same activation script with the previous release ID and the desired `--phone` mode. Retain previous releases and backups. This release keeps SQLite schema version 1; future incompatible migrations need a separately reviewed restore procedure. Do not restore a backup over a running service. A restore can repeat notifications/lose post-backup updates; review fixed destinations and usage before resuming. Unknown costs require billing reconciliation, not automatic zeroing.

```sh
systemctl status call-secretary-phone call-secretary-worker
journalctl -u call-secretary-phone -u call-secretary-worker --since today
sudo -u call-secretary /opt/call-secretary/current/venv/bin/call-secretary \
  --profiles /etc/call-secretary/family.yaml --env-file /etc/call-secretary/secretary.env status
```

`health` is a last-observed snapshot, not a live probe: unknown means never observed, stale means older than 30 seconds or a clock jump, idle means no active operation, ready/active describe a recent operation, unavailable records a failure. Telegram success/failure refreshes its observation. Queue counts distinguish waiting_destination/blocked/leased/pending/complete; blocked items are not silently reassigned. No tokens, device paths, names or transcript content appear in these health entries.

The units use a dedicated account, 0700 state, 0077 umask, no core dumps, restricted writable paths, restart limits and `KillMode=control-group`. [systemd service lifecycle documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html). Service restart does not reset USB or modify other services. Run reboot, update/rollback, permissions and failure acceptance on SERVER only after the modem arrives.
