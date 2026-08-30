#!/bin/bash
# Runs on jk-server-ccu (via cron/systemd timer, every 15-30 min). Pulls
# jkumar-server's freshly-refreshed GTFS feed only when it has actually
# changed (checksum-gated), rebuilds the OTP graph, and restarts the serving
# container on success. On build failure, the previous graph.obj and
# checksum are left untouched — serve stale-but-working over nothing, and
# let the next poll retry. See OTP_SIDECAR_PLAN.md "Refresh trigger design".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/upstream.env"
GRAPHS_DIR="$SCRIPT_DIR/graphs"
CHECKSUM_STATE="$GRAPHS_DIR/.last_checksum"
# Built in a scratch dir, not GRAPHS_DIR directly — OTP's own --build --save
# writes graph.obj into whatever directory it reads GTFS from, so building
# straight into GRAPHS_DIR would risk clobbering the last known-good
# graph.obj mid-write on a failed/interrupted build. Only a build that
# genuinely exits 0 gets its graph.obj promoted into GRAPHS_DIR below —
# that's what actually makes "serve stale-but-working over nothing" true,
# not just documented.
BUILD_DIR="$GRAPHS_DIR/.build-tmp"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: upstream.env not found at $ENV_FILE"
  echo "Copy upstream.env.example to upstream.env and fill in the values."
  exit 1
fi

# `|| [[ -n "$line" ]]` on the loop condition (not just `while read`) —
# `read` returns non-zero on the final line of a file with no trailing
# newline, which would otherwise silently drop it. Found in Opus review,
# 2026-08-12: upstream.env.example's last key (UPSTREAM_DATA_DIR) is exactly
# the kind of hand-edited file that can end up without one.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] && declare "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
done < "$ENV_FILE"

if [[ -z "${UPSTREAM_URL:-}" ]]; then
  echo "Error: UPSTREAM_URL is not set in $ENV_FILE — copy upstream.env.example and fill it in."
  exit 1
fi

mkdir -p "$GRAPHS_DIR"

# `-f` makes curl exit non-zero on a 4xx/5xx (e.g. the main app returning 404
# because no refresh has completed yet) instead of writing the error body as
# if it were the checksum — plain HTTP over the tailnet, replacing an
# earlier SSH pull (GitHub issue #28: Tailscale SSH's check-mode silently
# hangs unattended scheduled connections into jkumar-server). Bounded with
# --connect-timeout/--max-time so a wedged-but-not-down main app (e.g. stuck
# behind a DB lock, answering nothing rather than erroring) can't reproduce
# that same silent-hang failure mode at the HTTP layer instead.
remote_checksum="$(curl -fsS --connect-timeout 10 --max-time 30 "$UPSTREAM_URL/api/gtfs/checksum" 2>/dev/null || true)"
if [[ -z "$remote_checksum" ]]; then
  echo "$(date -Iseconds) poll: could not read remote checksum (unreachable, or not yet" \
       "persisted — check 'curl $UPSTREAM_URL/api/gtfs/checksum' works), skipping this run"
  exit 0
fi

local_checksum=""
[[ -f "$CHECKSUM_STATE" ]] && local_checksum="$(cat "$CHECKSUM_STATE")"

# Also rebuild if the checksum matches but the graph itself is missing
# (deleted, corrupted, or an interrupted `mv` from a previous run) — found
# in Opus review, 2026-08-12: without this, a lost graph.obj with a still-
# matching checksum would no-op forever until the next upstream feed change
# (up to 24h), while the serving container crash-loops on --load.
if [[ "$remote_checksum" == "$local_checksum" && -f "$GRAPHS_DIR/graph.obj" ]]; then
  echo "$(date -Iseconds) poll: checksum unchanged, no-op"
  exit 0
fi

if [[ "$remote_checksum" == "$local_checksum" ]]; then
  echo "$(date -Iseconds) poll: checksum unchanged but graph.obj is missing — rebuilding, pulling feed"
else
  echo "$(date -Iseconds) poll: checksum changed ($local_checksum -> $remote_checksum), pulling feed"
fi
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
# --max-time is generous here (the zip is ~80MB, not a quick request like
# the checksum above) but still bounded — same rationale as above.
if ! curl -fsS --connect-timeout 10 --max-time 300 "$UPSTREAM_URL/api/gtfs/zip" -o "$BUILD_DIR/gtfs.zip"; then
  echo "$(date -Iseconds) poll: could not fetch gtfs.zip from $UPSTREAM_URL/api/gtfs/zip," \
       "skipping this run (next poll will retry)"
  rm -rf "$BUILD_DIR"
  exit 0
fi

# The checksum was read separately from the zip fetch just above (two
# round-trips, not one atomic operation) — verify what actually landed
# matches what the checksum claimed, rather than trusting a check that ran
# moments earlier against a file that could since have changed again on the
# remote end (see OTP_SIDECAR_PLAN.md's refresh-trigger design; a refresh
# landing in that gap is very low-probability given a daily refresh vs. a
# 15-30 min poll interval, but cheap to actually verify rather than assume).
pulled_checksum="$(sha256sum "$BUILD_DIR/gtfs.zip" | cut -d' ' -f1)"
if [[ "$pulled_checksum" != "$remote_checksum" ]]; then
  echo "$(date -Iseconds) poll: pulled zip's checksum ($pulled_checksum) doesn't match the" \
       "checksum file ($remote_checksum) — feed likely changed mid-pull, skipping this run" \
       "(next poll will retry)"
  rm -rf "$BUILD_DIR"
  exit 0
fi

cp "$SCRIPT_DIR/otp-config.json" "$BUILD_DIR/otp-config.json"

# Pinned by digest, matching docker-compose.prod.yml's `otp` service and
# deploy-otp-sidecar.sh's pull — must always match, or the serving container
# refuses to load a graph built by a different OTP version ("graph file is
# incompatible with this version of OTP"). This is exactly what happened
# when this line pulled unpinned `opentripplanner/opentripplanner` (implicit
# `latest`, a rolling dev-2.x SNAPSHOT tag): the graph built here landed on
# ser.ver.id 270 while the serving image was still ser.ver.id 269, and the
# container crash-looped for a week (found 2026-08-30) with nothing to
# advance the checksum and force a retry, since the build itself "succeeded".
# 2.9.0 (pinned here) is a genuine numbered release off `master`, not a
# `dev-2.x` snapshot — bump all three places together, deliberately, then
# force a rebuild; never let any of them float independently or track
# `latest`.
OTP_IMAGE="opentripplanner/opentripplanner@sha256:a7eac7da397faa9ec9dee407d4204895d24df4981500662fa6793aae0e71fd8f"

echo "$(date -Iseconds) poll: building graph"
if docker run --rm -v "$BUILD_DIR:/var/opentripplanner" \
    "$OTP_IMAGE" --build --save; then
  mv "$BUILD_DIR/graph.obj" "$GRAPHS_DIR/graph.obj"
  # otp-config.json (enables ActuatorAPI) also needs to live in GRAPHS_DIR
  # itself, not just BUILD_DIR — the serving container (docker-compose.prod.yml)
  # mounts GRAPHS_DIR and runs --load, which reads it from there.
  cp "$SCRIPT_DIR/otp-config.json" "$GRAPHS_DIR/otp-config.json"
  echo "$remote_checksum" > "$CHECKSUM_STATE"
  rm -rf "$BUILD_DIR"
  echo "$(date -Iseconds) poll: build succeeded, restarting serving container"
  (cd "$SCRIPT_DIR" && docker compose -f docker-compose.prod.yml up -d --force-recreate)
else
  echo "$(date -Iseconds) poll: build FAILED, leaving previous graph.obj and checksum in place" \
       "(scratch build dir kept at $BUILD_DIR for debugging — next successful poll clears it)"
  exit 1
fi
