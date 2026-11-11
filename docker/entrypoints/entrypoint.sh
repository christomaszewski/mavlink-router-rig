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
  # TxQueueSize (the rig config's tx_queue_size) exists only in the fork build. The upstream
  # parser skips keys it does not know without a word, so say it here instead of letting an
  # operator believe the queue was sized. The key name is a literal in a binary that supports it.
  if grep -qi '^TxQueueSize[ =]' /etc/mavlink-router/main.conf \
     && ! grep -q TxQueueSize /usr/bin/mavlink-routerd; then
    echo "entrypoint: WARNING — main.conf sets TxQueueSize (tx_queue_size), which this build of mavlink-router does not support: it is IGNORED. Use an image built from the fork pin (MAVROUTER_SOURCE=fork)." >&2
  fi
fi

# Flight logging under rig. The logging compose overlay binds the fleet data dir at /data and
# names this instance in MAVROUTER_LOG_INSTANCE; main.conf carries the FLAT log dir
# (/data/mavlink-router/<name>). When rig has a run open (/data/current -> runs/<id>), re-point
# the router into it — <run>/mavlink-router/<name> — so the logs travel with that run. `current`
# is resolved ONCE, here: a recording belongs to the run it started in, and following the link
# live would lose the next log file to a run rotation. --log on the command line overrides the
# config's Log (upstream reads the conf files first, then argv). Without the env var this block
# is inert, so the image behaves as before under an older launch surface or a bare `docker run`.
if [ -n "${MAVROUTER_LOG_INSTANCE:-}" ]; then
  if [ -e /data/current ]; then
    run="$(readlink -f /data/current)"
    case "$run" in
      /data/runs/*)
        set -- "$@" --log "$run/mavlink-router/$MAVROUTER_LOG_INSTANCE"
        echo "entrypoint: flight logs -> $run/mavlink-router/$MAVROUTER_LOG_INSTANCE (the open rig run)" >&2
        ;;
      *)
        echo "entrypoint: /data/current resolves to '$run', not a run under /data/runs — flight logs stay in the flat /data/mavlink-router/$MAVROUTER_LOG_INSTANCE" >&2
        ;;
    esac
  elif [ -L /data/current ]; then
    echo "entrypoint: /data/current is a dangling link — flight logs stay in the flat /data/mavlink-router/$MAVROUTER_LOG_INSTANCE" >&2
  fi
fi

exec /usr/bin/mavlink-routerd "$@"
