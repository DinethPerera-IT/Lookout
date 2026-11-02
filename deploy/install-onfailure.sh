#!/usr/bin/env bash
# Add (or remove) Lookout's OnFailure= hook on important units. Run with sudo:
#   sudo deploy/install-onfailure.sh caddy.service discordbot.service ...
#   sudo deploy/install-onfailure.sh --remove caddy.service ...
# Each unit gets /etc/systemd/system/<unit>.d/lookout-onfailure.conf (a drop-in that only adds
# OnFailure=lookout-investigate@<unit>.service). Units are NOT restarted: daemon-reload is enough.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
remove=0
[[ "${1:-}" == "--remove" ]] && { remove=1; shift; }
[[ $# -gt 0 ]] || { sed -n '2,6p' "$0"; exit 1; }
[[ -f /etc/systemd/system/lookout-investigate@.service ]] || { echo "run deploy/install.sh first" >&2; exit 1; }

for unit in "$@"; do
  [[ "$unit" =~ ^[A-Za-z0-9@_.:-]+\.service$ ]] || { echo "skip: '$unit' isn't a service name" >&2; continue; }
  systemctl cat "$unit" &>/dev/null || { echo "skip: $unit doesn't exist" >&2; continue; }
  dir="/etc/systemd/system/$unit.d"
  if (( remove )); then
    rm -f "$dir/lookout-onfailure.conf" && rmdir --ignore-fail-on-non-empty "$dir" 2>/dev/null || true
    echo "removed hook from $unit"
  else
    install -d -m 755 "$dir"
    install -m 644 -o root -g root "$REPO/deploy/systemd/onfailure.conf" "$dir/lookout-onfailure.conf"
    echo "hooked $unit"
  fi
done
systemctl daemon-reload
for unit in "$@"; do
  systemctl show "$unit" -p OnFailure 2>/dev/null | sed "s/^/$unit: /"
done
