#!/usr/bin/env bash
# Smoke one compose profile: build, run the test suite inside the image, then boot
# it and wait for /api/status to answer 200. Fails loudly at the first bad step.
#
#   docker/smoke.sh yolo | yolo-jetson | ds-x86 | ds-jetson
set -euo pipefail

P="${1:?usage: smoke.sh <yolo|yolo-jetson|ds-x86|ds-jetson>}"
SVC="countkit-$P"
cd "$(dirname "$0")/.."

echo "== build $SVC"
docker compose --profile "$P" build

echo "== tests inside the image"
docker compose --profile "$P" run --rm --entrypoint bash "$SVC" \
    -c 'for t in tests/test_*.py; do echo "-- $t"; python3 "$t" || exit 1; done'

echo "== boot"
docker compose --profile "$P" up -d
trap 'docker compose --profile "$P" down' EXIT

for i in $(seq 60); do
    if curl -fsS http://localhost:8090/api/status > /tmp/countkit-status.json 2>/dev/null; then
        echo "== up after ${i}s"
        cat /tmp/countkit-status.json; echo
        echo "== smoke ok: $P"
        exit 0
    fi
    sleep 1
done

echo "== FAILED: /api/status never answered 200 within 60s" >&2
docker compose --profile "$P" logs --tail 50 >&2
exit 1
