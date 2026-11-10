#!/usr/bin/env bash
# End-to-end smoke test of the launch surface against a REAL image and a Docker daemon: launcher
# -> compose -> entrypoint -> mavlink-routerd -> healthcheck. It covers what neither `rig certify`
# (renders compose client-side, never starts a container) nor the unit tests can see:
#   - the router starts on the rendered main.conf and the baked healthcheck passes
#   - an edited config makes `up -d` recreate the container; an unchanged one does not
#   - with logging on, the entrypoint pins flight logs into the open rig run, or the flat layout
#   - the process-liveness health fallback when the TCP server is disabled
#   - `status --format json` and `down`
#
#   tools/smoke_test.sh                              # tests the local mavlink_router:latest
#   MAVROUTER_IMAGE=reg:5000/mavlink_router:v1 tools/smoke_test.sh
#
# network_mode: host — the test router binds SMOKE_TCP_PORT (default 15760) and SMOKE_UDP_PORT /
# SMOKE_UDP_PORT+1 (default 24550/24551) on this machine for a few seconds; no serial device.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UP="$REPO/mavlink-router-up"

export MAVROUTER_IMAGE="${MAVROUTER_IMAGE:-mavlink_router:latest}"
TCP_PORT="${SMOKE_TCP_PORT:-15760}"
UDP_PORT="${SMOKE_UDP_PORT:-24550}"
# This is a standalone run: nothing of a surrounding rig session may leak in.
unset RIG_RUNTIME_IMAGE_PINS RIG_IMAGE_REGISTRY RIG_IMAGE_TAG RIG_IMAGE_PINS

docker image inspect "$MAVROUTER_IMAGE" >/dev/null 2>&1 || {
  echo "smoke: image '$MAVROUTER_IMAGE' is not in the local store — build it first, e.g." >&2
  echo "       docker build -f docker/Dockerfile.runtime -t mavlink_router:latest ." >&2
  exit 1
}

NAME="smoke$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mavrouter-smoke.XXXXXX")"
CFG="$WORK/$NAME.yaml"
export RIG_DATA_DIR="$WORK/data"
export COMPOSE_PROJECT_NAME="mavlink-router-smoke-$$"
RUN_ID="20260101T000000Z_smoke"
mkdir -p "$RIG_DATA_DIR/runs/$RUN_ID"

step() { echo "smoke: $*" >&2; }
fail() {
  echo "smoke: FAILED — $*" >&2
  "$UP" "$CFG" logs --no-color 2>/dev/null | tail -n 40 >&2 || true
  exit 1
}
cleanup() {
  "$UP" "$CFG" down --timeout 5 >/dev/null 2>&1 || true
  # the container wrote the log dirs as root: remove them from a container too
  docker run --rm --entrypoint /bin/sh -v "$WORK:/work" "$MAVROUTER_IMAGE" -c 'rm -rf /work/data' \
    >/dev/null 2>&1 || true
  rm -rf "$WORK" "$REPO/var/run/$NAME.conf"
}
trap cleanup EXIT

write_config() {  # write_config <tcp_server_port> <udp_port> <logging: true|false>
  cat > "$CFG" <<EOF
service: mavlink-router
name: $NAME
general:
  tcp_server_port: $1
  debug_log_level: info
logging:
  enabled: $3
  mode: always
  telemetry: true
  max_log_files: 5
udp_endpoints:
  - {name: gcs, mode: server, address: 127.0.0.1, port: $2}
EOF
}
container() { "$UP" "$CFG" ps -q 2>/dev/null; }
cmdline() { docker exec "$1" sh -c "tr '\\0' ' ' < /proc/1/cmdline"; }
healthy() {  # the image's own probe, retried while the router binds its ports
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    docker exec "$1" /usr/local/bin/healthcheck.sh >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

step "up with an open rig run ($MAVROUTER_IMAGE)"
ln -s "runs/$RUN_ID" "$RIG_DATA_DIR/current"
write_config "$TCP_PORT" "$UDP_PORT" true
"$UP" "$CFG" up -d >/dev/null 2>&1 || fail "up -d"
first="$(container)"
[ -n "$first" ] || fail "no container after up -d"
healthy "$first" || fail "healthcheck never passed on TCP $TCP_PORT"
running "$first" || fail "container is not running"
args="$(cmdline "$first")"
case "$args" in
  *"-c /etc/mavlink-router/main.conf"*"--log /data/runs/$RUN_ID/mavlink-router/$NAME"*) ;;
  *) fail "router is not pinned to the open run: $args" ;;
esac
docker exec "$first" grep -qx "Port = $UDP_PORT" /etc/mavlink-router/main.conf \
  || fail "mounted main.conf does not carry UDP port $UDP_PORT"

step "status --format json"
status="$("$UP" "$CFG" status --format json 2>/dev/null)"
case "$status" in *'"State":"running"'*) ;; *) fail "status does not report a running container: $status" ;; esac

step "unchanged config: up -d keeps the container"
"$UP" "$CFG" up -d >/dev/null 2>&1 || fail "second up -d"
[ "$(container)" = "$first" ] || fail "an unchanged config recreated the container"

step "edited config: up -d recreates the container on the new main.conf"
write_config "$TCP_PORT" "$((UDP_PORT + 1))" true
"$UP" "$CFG" up -d >/dev/null 2>&1 || fail "up -d after a config edit"
second="$(container)"
[ -n "$second" ] && [ "$second" != "$first" ] \
  || fail "a config edit did not recreate the container — the router would keep its old config"
healthy "$second" || fail "healthcheck after the config edit"
docker exec "$second" grep -qx "Port = $((UDP_PORT + 1))" /etc/mavlink-router/main.conf \
  || fail "recreated container does not see the edited main.conf"

step "no open run: flight logs use the flat layout"
rm "$RIG_DATA_DIR/current"
"$UP" "$CFG" up -d --force-recreate >/dev/null 2>&1 || fail "up -d --force-recreate"
third="$(container)"
healthy "$third" || fail "healthcheck without an open run"
args="$(cmdline "$third")"
case "$args" in *--log*) fail "router was pinned to a run although none is open: $args" ;; esac
docker exec "$third" grep -qx "Log = /data/mavlink-router/$NAME" /etc/mavlink-router/main.conf \
  || fail "main.conf does not carry the flat log dir"

step "logging off, TCP server off: process-liveness health, no data bind"
write_config 0 "$UDP_PORT" false
"$UP" "$CFG" up -d >/dev/null 2>&1 || fail "up -d with the TCP server disabled"
fourth="$(container)"
[ "$fourth" != "$third" ] || fail "disabling the TCP server did not recreate the container"
healthy "$fourth" || fail "process-liveness healthcheck"
[ "$(docker inspect -f '{{.Config.Healthcheck.Test}}' "$fourth")" != "<nil>" ] || fail "image has no HEALTHCHECK"
[ "$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$fourth")" = "/etc/mavlink-router/main.conf " ] \
  || fail "unexpected mounts with logging disabled"

step "down"
"$UP" "$CFG" down --timeout 5 >/dev/null 2>&1 || fail "down"
[ -z "$(container)" ] || fail "containers remain after down"

echo "smoke: OK" >&2
