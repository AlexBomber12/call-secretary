#!/usr/bin/env bash
# Validate a candidate before stopping services and restore the previous release on failure.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ || ${2:-} != '' && ${2:-} != '--phone' ]]; then
  echo 'Usage: activate-release.sh RELEASE_ID [--phone]' >&2; exit 2
fi
[[ $(id -u) == 0 ]] || { echo 'Run as root on the target Linux server' >&2; exit 2; }

root=/opt/call-secretary
release="$root/releases/$1"
current="$root/current"
pending="$root/.current-$$"
state=/var/lib/call-secretary
units=(call-secretary-worker.service call-secretary-phone.service)
[[ $(realpath -- "$release") == "$release" && -x "$release/venv/bin/call-secretary" ]]
exec 9>"$root/.activation.lock"
flock -n 9 || { echo 'Another Call Secretary activation is in progress' >&2; exit 2; }

previous=
if [[ -L $current ]]; then
  previous=$(readlink -- "$current")
  [[ $previous == "$root/releases/"* && $(realpath -- "$previous") == "$previous" &&
     -x $previous/venv/bin/call-secretary ]]
elif [[ -e $current ]]; then
  echo 'Current release must be a symbolic link' >&2; exit 2
fi

cd /etc/call-secretary
[[ -f secretary.env && -f family.yaml ]]
args=(--profiles /etc/call-secretary/family.yaml --env-file /etc/call-secretary/secretary.env)
as_service=(runuser -u call-secretary -- /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin)
checks=(--worker --expect-state-dir "$state")
if [[ ${2:-} == '--phone' ]]; then
  checks+=(--modem)
fi
"${as_service[@]}" "$release/venv/bin/call-secretary" "${args[@]}" check-config "${checks[@]}"

active=()
enabled=()
for unit in "${units[@]}"; do
  if systemctl is-active --quiet "$unit"; then active+=("$unit"); fi
  if systemctl is-enabled --quiet "$unit"; then enabled+=("$unit"); fi
done

select_release() {
  ln -s -- "$1" "$pending" && mv -Tf -- "$pending" "$current"
}

restore_previous() {
  local status=$1 failed=0 unit
  trap - EXIT INT TERM
  (( status != 0 )) || return 0
  rm -f -- "$pending" || failed=1
  systemctl stop "${units[@]}" || failed=1
  if [[ -n $previous ]]; then
    select_release "$previous" || failed=1
  elif [[ -L $current && $(readlink -- "$current") == "$release" ]]; then
    rm -f -- "$current" || failed=1
  fi
  if (( failed == 0 )); then
    for unit in "${units[@]}"; do
      if [[ " ${enabled[*]} " == *" $unit "* ]]; then
        systemctl enable "$unit" || failed=1
      else
        systemctl disable "$unit" || failed=1
      fi
    done
    for unit in "${active[@]}"; do
      systemctl start "$unit" || failed=1
    done
  fi
  if (( failed )); then
    echo 'Activation failed and restoration was incomplete. Inspect the Call Secretary units.' >&2
  else
    echo 'Activation failed. The previous release and service state were restored.' >&2
  fi
  exit "$status"
}

trap 'restore_previous "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

systemctl stop "${units[@]}"
if [[ -n $previous && -f $state/secretary.sqlite3 ]]; then
  "${as_service[@]}" "$previous/venv/bin/call-secretary" "${args[@]}" backup \
    --destination "$state/backups/pre-$1-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite3"
fi
select_release "$release"
systemctl enable --now "${units[0]}"
if [[ ${2:-} == '--phone' ]]; then
  systemctl enable --now "${units[1]}"
else
  systemctl disable "${units[1]}"
fi

sleep 1
systemctl is-active --quiet "${units[0]}"
if [[ ${2:-} == '--phone' ]]; then
  systemctl is-active --quiet "${units[1]}"
fi
trap - EXIT INT TERM
echo "Activated $1. Check status and the journal for runtime readiness."
