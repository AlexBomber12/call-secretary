#!/usr/bin/env bash
# Stage reviewed source as an immutable release; do not activate or start services.
set -euo pipefail
if [[ $# != 2 || ! $2 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo 'Usage: install-server.sh PUBLIC_SOURCE_DIR RELEASE_ID' >&2; exit 2
fi
[[ $(id -u) == 0 ]] || { echo 'Run as root on the target Linux server' >&2; exit 2; }
source_dir=$(realpath -- "$1")
release="/opt/call-secretary/releases/$2"
[[ ! -e $release ]] || { echo 'Release already exists; use a new ID' >&2; exit 2; }
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
for command in espeak-ng fuser systemctl; do command -v "$command" >/dev/null; done
for file in pyproject.toml requirements.lock README.md; do test -f "$source_dir/$file"; done
test -f "$source_dir/src/call_secretary/phone.py"
if ! id call-secretary >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/call-secretary --shell /usr/sbin/nologin call-secretary
fi
install -d -m 0755 "$release/src/call_secretary"
install -m 0644 "$source_dir"/{pyproject.toml,requirements.lock,README.md} "$release/"
install -m 0644 "$source_dir"/src/call_secretary/*.py "$release/src/call_secretary/"
python3.12 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install -r "$release/requirements.lock" setuptools==84.1.0
"$release/venv/bin/python" -m pip install --no-deps --no-build-isolation "$release"
install -d -o root -g call-secretary -m 0750 /etc/call-secretary
install -d -o call-secretary -g call-secretary -m 0700 /var/lib/call-secretary
install -m 0644 "$source_dir"/deploy/call-secretary-{phone,worker}.service /etc/systemd/system/
systemctl daemon-reload
echo "Staged $release. Services remain inactive until explicit activation; configure private files and H1 first."
