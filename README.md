# mavlink-router-rig

Rig-integrated wrapper for upstream [mavlink-router](https://github.com/mavlink-router/mavlink-router)
— the MAVLink hub between the flight controller, the GCS, and the onboard autonomy stack.

We wrap the router **unmodified** so a source update is a one-line version bump. We add no C++ of
our own — only the rig-integration shell. The one twist over the sibling wrappers (ouster, sbg):
this repo can build from **two pinned sources** — the official upstream, or the personal fork
carrying latency fixes — switched at build time, both kept green by CI.

## How it works

| File | Role |
|---|---|
| `sources/upstream.repos` | Pin of the official mavlink-router (exact SHA/tag, never a branch) |
| `sources/fork.repos` | Pin of the fork (`christomaszewski/mavlink-router`) with latency fixes |
| `sources/DEFAULT` | Which pin builds when `MAVROUTER_SOURCE` is unset |
| `rigging.yaml` | The rig service descriptor (launcher, verbs, build, ports, launch surface) |
| `mavlink-router-up` | Launcher: config → identity + rendered `main.conf` → `docker compose` |
| `tools/render_conf.py` | Generic rig config → upstream `main.conf` INI; `--env` identity mode |
| `tools/build_image.sh` | rig build phase: resolve pin → `docker build` + push |
| `docker/Dockerfile.runtime` | 2-stage alpine: clone pinned ref → meson/ninja → binary-only runtime |
| `docker/compose/compose.deploy.yaml` | The production compose (serial/logging overlays join per config) |
| `configs/mavlink-router.example.yaml` | Example instance config; CI's certify config |

## Quick start

```sh
# build the image locally (default source = sources/DEFAULT)
docker build -f docker/Dockerfile.runtime -t mavlink_router:latest .

# copy + edit the example config, then bring the router up
cp configs/mavlink-router.example.yaml configs/fcu.yaml
./mavlink-router-up configs/fcu.yaml up -d
./mavlink-router-up configs/fcu.yaml status
./mavlink-router-up configs/fcu.yaml logs -f
./mavlink-router-up configs/fcu.yaml down
```

On a rig vehicle none of this is manual: `rig build` runs `tools/build_image.sh`, `rig up` runs
the launcher with the fleet env (`RIG_IMAGE_REGISTRY`/`RIG_IMAGE_TAG`, `COMPOSE_PROJECT_NAME`,
`RIG_DATA_DIR`).

## Configuration

One YAML per router instance (see the example's inline docs). `render_conf.py` derives upstream's
INI: `general:` → `[General]`, each `uart_endpoints`/`udp_endpoints`/`tcp_endpoints` entry → an
endpoint section, an endpoint's `filters:` map passed verbatim (message/source filters, `Group`).
Omitted keys keep upstream defaults; unknown keys are a hard error.

Notes:

- **Serial**: use the stable `/dev/serial/by-id/...` path — it doubles as the in-container device
  path (the serial compose overlay maps it 1:1). At most one UART endpoint per instance (the
  overlay carries one device slot). `by-path` paths are rejected: their colons break the mapping.
- **Health**: the container healthcheck TCP-connects to `general.tcp_server_port` (default 5760);
  set it to `0` (server disabled) and the probe degrades to process liveness.
- **Logging**: `logging.enabled: true` adds a compose overlay binding
  `$RIG_DATA_DIR/mavlink-router/<name>` over the container's log dir. Upstream keeps logs
  forever by default — set `max_log_files` or `min_free_space_mb`.
- **Diagnostics** go to stderr (no `--syslog` in the image), so `rig logs` / `docker logs` see them.

## Choosing upstream vs fork

```sh
MAVROUTER_SOURCE=upstream tools/build_image.sh <registry> [tag]   # official repo
MAVROUTER_SOURCE=fork     tools/build_image.sh <registry> [tag]   # the latency-fix fork
tools/build_image.sh <registry> [tag]                             # whatever sources/DEFAULT says
```

Both pins are exact SHAs, both are built by every CI run, and the image records its origin as OCI
labels — `docker image inspect` (`org.opencontainers.image.source` / `.revision`) answers "what is
this vehicle actually running?". Flip `sources/DEFAULT` to `fork` (a committed one-word change)
when the fork should become the fleet default.

## Updating a source

Bump `version:` in the pin file (exact tag or full SHA), rebuild, skim the changelog for renamed
config keys, commit as e.g. `vendor mavlink-router <ref>`. The coupling surface to upstream is
deliberately small: the `main.conf` key names rendered by `tools/render_conf.py`, the
`-c`/`--version` CLI in the entrypoint, and the meson build invocation in the Dockerfile.

## Releasing

`gh release create vX.Y.Z --generate-notes` — the `registry-release` workflow rewrites this
service's manifest in `rig-registry-public` (version + tagged rev) and regenerates the index.
One-time setup: a `RIG_REGISTRY_TOKEN` Actions secret (fine-grained PAT, Contents read/write on
`rig-registry-public` only).

## License

Apache-2.0 for this wrapper. Upstream mavlink-router keeps its own license; it is fetched at
build time and never vendored into this repo.
