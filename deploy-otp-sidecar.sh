#!/bin/bash
# Deploys otp-sidecar/ to jk-server-ccu. Mirrors rail-disruption-monitor's
# deploy.sh pattern (see that repo), adapted for this sidecar: no custom
# image to build (uses opentripplanner/opentripplanner directly), so this
# just pushes files and leaves the actual build/serve lifecycle to
# poll_and_build.sh (run manually the first time, then via the systemd
# timer — see otp-sidecar/README.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env.deploy not found at $ENV_FILE"
    echo "Copy .env.deploy.example to .env.deploy and fill in the values."
    exit 1
fi

# `|| [[ -n "$line" ]]` on the loop condition (not just `while read`) —
# `read` returns non-zero on the final line of a file with no trailing
# newline, silently dropping it otherwise. Found in Opus review, 2026-08-12.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] && declare "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
done < "$ENV_FILE"

for var in REMOTE_USER REMOTE_HOST REMOTE_APP_DIR; do
  if [[ -z "${!var:-}" ]]; then
    echo "Error: $var is not set in $ENV_FILE — copy .env.deploy.example and fill in all three values."
    exit 1
  fi
done

REMOTE="$REMOTE_USER@$REMOTE_HOST"

# otp-poll.service hardcodes WorkingDirectory=%h/otp-sidecar (systemd's %h
# expands to the unit's owning user's home dir) — a REMOTE_APP_DIR pointing
# anywhere else deploys a timer that fails every single fire with
# status=203/EXEC, 20 minutes after setup looked like it succeeded. Found in
# Opus review, 2026-08-12: catch the mismatch at deploy time, not at the
# first silent 3am failure.
remote_home="$(ssh "$REMOTE" 'echo $HOME')"
if [[ "$REMOTE_APP_DIR" != "$remote_home/otp-sidecar" ]]; then
  echo "Error: REMOTE_APP_DIR ($REMOTE_APP_DIR) must be exactly \$HOME/otp-sidecar" \
       "($remote_home/otp-sidecar) on $REMOTE_HOST — otp-poll.service's WorkingDirectory" \
       "hardcodes %h/otp-sidecar and won't resolve to anything else."
  exit 1
fi

echo "==> Ensuring remote directory exists: $REMOTE_APP_DIR"
ssh "$REMOTE" "mkdir -p '$REMOTE_APP_DIR/graphs' ~/.config/systemd/user"

echo "==> Pushing sidecar files (config/scripts/compose/systemd units)"
scp "$SCRIPT_DIR/otp-sidecar/otp-config.json" \
    "$SCRIPT_DIR/otp-sidecar/poll_and_build.sh" \
    "$SCRIPT_DIR/otp-sidecar/docker-compose.prod.yml" \
    "$SCRIPT_DIR/otp-sidecar/otp-poll.service" \
    "$SCRIPT_DIR/otp-sidecar/otp-poll.timer" \
    "$SCRIPT_DIR/otp-sidecar/README.md" \
    "$REMOTE:$REMOTE_APP_DIR/"

ssh "$REMOTE" "chmod +x '$REMOTE_APP_DIR/poll_and_build.sh'"

# Also install the unit files into the user-systemd search path and
# daemon-reload, so a future edit to otp-poll.service/.timer actually takes
# effect on redeploy rather than sitting in REMOTE_APP_DIR unused (found in
# Opus review, 2026-08-12). Doesn't enable/start anything — first-time
# enable is still a deliberate manual step (see README.md), since it also
# needs loginctl enable-linger, which needs the box owner's sudo.
echo "==> Installing/refreshing systemd user units"
ssh "$REMOTE" "cp '$REMOTE_APP_DIR/otp-poll.service' '$REMOTE_APP_DIR/otp-poll.timer' ~/.config/systemd/user/ && systemctl --user daemon-reload"

# upstream.env / .env hold this box's own secrets/config (SSH target,
# Tailscale IP) — never overwrite an existing one, only seed if absent.
ssh "$REMOTE" "test -f '$REMOTE_APP_DIR/upstream.env'" || {
  echo "==> Seeding upstream.env.example (fill in on the remote box)"
  scp "$SCRIPT_DIR/otp-sidecar/upstream.env.example" "$REMOTE:$REMOTE_APP_DIR/upstream.env.example"
}
ssh "$REMOTE" "test -f '$REMOTE_APP_DIR/.env'" || {
  echo "==> Seeding .env.example (fill in TAILSCALE_IP on the remote box)"
  scp "$SCRIPT_DIR/otp-sidecar/.env.example" "$REMOTE:$REMOTE_APP_DIR/.env.example"
}

echo "==> Pulling OTP image on remote"
ssh "$REMOTE" "docker pull opentripplanner/opentripplanner"

echo "==> Done. On $REMOTE_HOST, in $REMOTE_APP_DIR:"
echo "    - fill in upstream.env and .env if this was a first deploy (see README.md)"
echo "    - run ./poll_and_build.sh once by hand to do the first build"
echo "    - enable otp-poll.timer via systemctl --user (see README.md)"
