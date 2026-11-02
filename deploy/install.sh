#!/usr/bin/env bash
# Install or update Lookout as a systemd service. Run with sudo from the repo:
#   sudo deploy/install.sh
# It creates the `lookout` user if missing, copies the code to /opt/lookout (root-owned,
# read-only for the service) and installs the unit files. It does NOT start or enable
# anything: you do that yourself afterwards (see README, Phase 8).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/lookout
UNITS=/etc/systemd/system

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "need /usr/bin/python3 >= 3.11 (tomllib)" >&2; exit 1; }

# 1. Dedicated system user: no login shell, no home, member of systemd-journal only.
if ! id lookout &>/dev/null; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
          --shell /usr/sbin/nologin --comment "Lookout ops summary" lookout
  echo "created user lookout"
fi

# 2. Code: replace /opt/lookout atomically with a fresh copy (package + config only).
tmp="$(mktemp -d /opt/.lookout-new.XXXXXX)"
cp -r "$REPO/lookout" "$REPO/config.toml" "$tmp/"
[[ -f "$REPO/config.local.toml" ]] && cp "$REPO/config.local.toml" "$tmp/"   # machine-specific settings
find "$tmp" -name __pycache__ -prune -exec rm -rf {} +
/usr/bin/python3 -m compileall -q "$tmp/lookout"   # the service itself can't write bytecode
chown -R root:root "$tmp"
chmod -R u=rwX,go=rX "$tmp"
if [[ -d $DEST ]]; then
  mv "$DEST" "$DEST.old" && mv "$tmp" "$DEST" && rm -rf "$DEST.old"
else
  mv "$tmp" "$DEST"
fi
echo "code installed to $DEST ($(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown))"

# 3. Unit files, checked before systemd sees them.
# Units are templates: @ENV_FILE@ becomes the secrets file (read by systemd as root).
ENV_FILE="${LOOKOUT_ENV_FILE:-$REPO/.env}"
[[ "$ENV_FILE" = /* && "$ENV_FILE" != *[[:space:]]* ]] || { echo "LOOKOUT_ENV_FILE must be an absolute path" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || echo "note: $ENV_FILE doesn't exist yet; create it before starting the services"
UNIT_FILES=(lookout.service lookout.timer lookout-watch.service lookout-investigate@.service lookout-dashboard.service)
rendered="$(mktemp -d)"
for f in "${UNIT_FILES[@]}"; do
  sed "s|@ENV_FILE@|$ENV_FILE|g" "$REPO/deploy/systemd/$f" > "$rendered/$f"
done
cp "$rendered/lookout-investigate@.service" "$rendered/lookout-investigate@caddy.service"   # an instance, for verify
(cd "$rendered" && systemd-analyze verify lookout.service lookout.timer lookout-watch.service \
                                          lookout-investigate@caddy.service lookout-dashboard.service)
for f in "${UNIT_FILES[@]}"; do
  install -m 644 -o root -g root "$rendered/$f" "$UNITS/"
done
rm -rf "$rendered"
systemctl daemon-reload
echo "units installed. Nothing was started. Next steps: README.md, Phase 8."
