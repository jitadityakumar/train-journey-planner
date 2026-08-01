#!/usr/bin/env bash
# Docker smoke test: builds the image, runs it, and confirms the app comes up
# and serves a request. Not pytest — a fast sanity check that the container
# itself works, run as part of Docker verification (see PLAN.md Phase 1).
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER_NAME="tjp-smoke-test"
PORT="${SMOKE_TEST_PORT:-8099}"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Building image..."
docker build -t train-journey-planner:smoke-test .

echo "Starting container..."
docker run -d --name "$CONTAINER_NAME" -p "${PORT}:8000" train-journey-planner:smoke-test

echo "Waiting for /health..."
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "App is up."
    curl -s "http://127.0.0.1:${PORT}/health"
    echo
    echo "Smoke test passed."
    exit 0
  fi
  sleep 2
done

echo "App did not become healthy in time." >&2
docker logs "$CONTAINER_NAME" >&2
exit 1
