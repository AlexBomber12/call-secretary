#!/usr/bin/env bash
# Upgrade/rollback only this application. Retain old releases and the private backup.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ || ${2:-} != '' && ${2:-} != '--phone' ]]; then
  echo 'Usage: activate-release.sh RELEASE_ID [--phone]' >&2; exit 2
fi
[[ $(id -u) == 0 ]] || { echo 'Run as root on the target Linux server' >&2; exit 2; }
root=/opt/call-secretary
release="$root/releases/$1"
[[ $(realpath -- "$release") == "$release" && -x "$release/venv/bin/call-secretary" ]]
grep -qx 'STATE_DIR=/var/lib/call-secretary' /etc/call-secretary/secretary.env
args=(--profiles /etc/call-secretary/family.yaml --env-file /etc/call-secretary/secretary.env)
runuser -u call-secretary -- "$release/venv/bin/call-secretary" "${args[@]}" check-config
if [[ ${2:-} == '--phone' ]]; then
  grep -qx 'PHONE_H1_CONFIRMED=true' /etc/call-secretary/secretary.env
fi
systemctl stop call-secretary-phone.service call-secretary-worker.service
if [[ -x $root/current/venv/bin/call-secretary && -f /var/lib/call-secretary/secretary.sqlite3 ]]; then
  runuser -u call-secretary -- "$root/current/venv/bin/call-secretary" "${args[@]}" backup \
    --destination "/var/lib/call-secretary/backups/pre-$1-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
fi
ln -s -- "$release" "$root/.current-$$"
mv -Tf -- "$root/.current-$$" "$root/current"
systemctl enable --now call-secretary-worker.service
if [[ ${2:-} == '--phone' ]]; then
  systemctl enable --now call-secretary-phone.service
else
  systemctl disable call-secretary-phone.service
fi
echo "Activated $1. Check status/journal; use this script with the previous release ID for rollback."
