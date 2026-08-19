#!/bin/sh
# Liveness probe for the runtime image: healthy when the router's built-in TCP server accepts a
# connection on MAVROUTER_HEALTH_PORT — proof the config parsed and the endpoints opened. The
# launcher derives the port from the config's general.tcp_server_port (compose passes it through);
# 0 = TCP server disabled in the config -> fall back to a process-liveness check. Host networking
# means 127.0.0.1 here IS the vehicle's loopback, so probe the exact port this instance bound.
set -eu

port="${MAVROUTER_HEALTH_PORT:-5760}"
if [ "$port" = "0" ]; then
  pidof mavlink-routerd >/dev/null || exit 1
else
  nc -z -w 2 127.0.0.1 "$port" || exit 1
fi
exit 0
