#!/usr/bin/env bash
# Destructive ONLY inside an explicitly opted-in disposable CI container.
set -euo pipefail
[[ ${CALL_SECRETARY_CONTAINER_TEST:-} == 1 && -f /.dockerenv ]] || { echo 'Disposable CI container required' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12-venv libportaudio2 espeak-ng psmisc systemd passwd util-linux
# Exercise real files, accounts, venvs, installation, backups and links. Systemd
# command dispatch is captured because there is no init/service/device in CI.
cat >/usr/local/bin/systemctl <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> /tmp/secretary-service-actions
SH
chmod 0755 /usr/local/bin/systemctl
bash /source/deploy/install-server.sh /source ci-one
cp /source/config/family.example.yaml /etc/call-secretary/family.yaml
cp /source/.env.example /etc/call-secretary/secretary.env
sed -i 's|^STATE_DIR=.*|STATE_DIR=/var/lib/call-secretary|' /etc/call-secretary/secretary.env
chown root:call-secretary /etc/call-secretary/{family.yaml,secretary.env}
chmod 0640 /etc/call-secretary/{family.yaml,secretary.env}
! test -e /opt/call-secretary/releases/ci-one/.env
test "$(stat -c %a /var/lib/call-secretary)" = 700
test "$(stat -c %U /var/lib/call-secretary)" = call-secretary
args=(--profiles /etc/call-secretary/family.yaml --env-file /etc/call-secretary/secretary.env)
exe=/opt/call-secretary/releases/ci-one/venv/bin/call-secretary
runuser -u call-secretary -- "$exe" "${args[@]}" check-config
# Real espeak binary, fixed local phrase, no hardware and no saved audio.
runuser -u call-secretary -- /opt/call-secretary/releases/ci-one/venv/bin/python -c \
  'from call_secretary.fallback_audio import local_phrase; pcm=local_phrase(); assert 48000 < len(pcm) < 768000'
if runuser -u call-secretary -- "$exe" "${args[@]}" phone-run; then exit 1; fi
bash /source/deploy/activate-release.sh ci-one
runuser -u call-secretary -- /opt/call-secretary/current/venv/bin/python -c \
  'from pathlib import Path; from call_secretary.storage import Store; s=Store(Path("/var/lib/call-secretary")); c=s.create_call("synthetic"); s.end_call(c); s.close()'
bash /source/deploy/install-server.sh /source ci-two
bash /source/deploy/activate-release.sh ci-two
test "$(readlink /opt/call-secretary/current)" = /opt/call-secretary/releases/ci-two
test "$(find /var/lib/call-secretary/backups -name '*.sqlite3' | wc -l)" = 1
bash /source/deploy/activate-release.sh ci-one
test "$(readlink /opt/call-secretary/current)" = /opt/call-secretary/releases/ci-one
test "$(find /var/lib/call-secretary/backups -name '*.sqlite3' | wc -l)" = 2
if bash /source/deploy/activate-release.sh ci-one --phone; then exit 1; fi
! grep -E 'enable.*phone|ModemManager|sms-gateway' /tmp/secretary-service-actions
systemd-analyze verify --man=no /etc/systemd/system/call-secretary-{phone,worker}.service
runuser -u call-secretary -- "$exe" "${args[@]}" status
echo 'DEPLOYMENT_SMOKE_OK: install, gate, real local prompt, upgrade, backup, rollback and unit verification'
