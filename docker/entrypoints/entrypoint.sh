#!/bin/sh
# Container entrypoint: exec mavlink-routerd on the mounted config. No --syslog — diagnostics go
# to stderr, where the container runtime captures them (`docker logs` / `rig logs`).
#
# The deploy compose mounts the rendered config at /etc/mavlink-router/main.conf (also upstream's
# built-in default path); we pass -c explicitly so a missing mount fails loudly instead of
# silently starting an empty router. A bare `docker run` with its own args still works: the -c is
# only prepended when the file exists, and extra args are forwarded verbatim.
set -eu

if [ -f /etc/mavlink-router/main.conf ]; then
  set -- -c /etc/mavlink-router/main.conf "$@"
fi
exec /usr/bin/mavlink-routerd "$@"
