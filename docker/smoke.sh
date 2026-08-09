#!/usr/bin/env bash
# Smoke one compose profile: build, run the test suite inside the image, then boot
# it and wait for /api/status to answer 200. Fails loudly at the first bad step.
#
#   docker/smoke.sh yolo | yolo-jetson | ds-x86 | ds-jetson
set -euo pipefail

P="${1:?usage: smoke.sh <yolo|yolo-jetson|ds-x86|ds-jetson>}"
SVC="countkit-$P"
cd "$(dirname "$0")/.."

# The compose binds refuse to create a missing source (see docker-compose.yaml), so
# prepare the default no-drive paths here — and the SAME paths compose will use: with
# INGEST_DIR/DATA_DIR pointing at a drive, creating ./ingest and ./data instead leaves
# the real sources to Docker, which is the failure the guard exists for.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
[ -f config.yaml ] || cp config.example.yaml config.yaml
for d in "${INGEST_DIR:-ingest}" "${DATA_DIR:-data}"; do
    [ -d "$d" ] && continue
    # A relative path is the checkout's own; an absolute one is the operator's drive,
    # and creating it here would put footage and crops on the boot device instead.
    case "$d" in
        /*) echo "== FAILED: $d does not exist — mount the drive, then re-run" >&2; exit 1 ;;
        *)  mkdir -p "$d" ;;
    esac
done

echo "== build $SVC"
docker compose --profile "$P" build

echo "== tests inside the image"
docker compose --profile "$P" run --rm --entrypoint bash "$SVC" \
    -c 'for t in tests/test_*.py; do echo "-- $t"; python3 "$t" || exit 1; done'

# A stack the operator started is never touched — not `down` (which kills a live
# analysis; restart:unless-stopped won't bring it back) and not `up -d` either, which
# recreates the container whenever the image changed, i.e. whenever the build above did
# anything. The in-image test suite ran under `compose run`, which is safe alongside.
WAS_UP=$(docker compose --profile "$P" ps -q "$SVC")
if [ -n "$WAS_UP" ]; then
    echo "== already running — left untouched; polling the live service"
else
    echo "== boot"
    docker compose --profile "$P" up -d
    trap 'docker compose --profile "$P" down' EXIT
fi

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
