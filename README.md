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
| `rigging.yaml` | The rig service descriptor (launcher, verbs, build, sources, ports, launch surface) |
| `mavlink-router-up` | Launcher: config → identity + rendered `main.conf` → `docker compose` |
| `tools/render_conf.py` | Generic rig config → validated upstream `main.conf` INI, staged for the launcher |
| `tools/runtime_images.py` | rig's shared image-plan helper (`images` / `runtime-images` verbs, pinned `up`) |
| `tools/build_image.sh` | rig build phase: resolve pin → build + push (multi-arch under rig) |
| `tools/rig-build.sh` | rig's build-contract helper — an identical copy of rig's `packaging/rig-build.sh` |
| `docker/Dockerfile.runtime` | 2-stage alpine: pinned source → meson/ninja → binary-only runtime |
| `docker/compose/compose.deploy.yaml` | The production compose (serial/logging overlays join per config) |
| `configs/mavlink-router.example.yaml` | Example instance config; CI's certify config |
| `tools/test_*.py`, `tools/smoke_test.sh` | Unit tests; end-to-end test against a real image |

## Quick start

```sh
# build the image locally — a bare `docker build` always builds the pinned UPSTREAM (the
# Dockerfile's defaults); for the fork, or to follow sources/DEFAULT, use tools/build_image.sh
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

Re-running `up -d` after editing the config applies it: the rendered `main.conf`'s content hash
is a label on the container, so a changed config recreates the router and an unchanged one
leaves it alone.

## Configuration

One YAML per router instance (see the example's inline docs). `render_conf.py` derives upstream's
INI: `general:` → `[General]`, each `uart_endpoints`/`udp_endpoints`/`tcp_endpoints` entry → an
endpoint section, an endpoint's `filters:` map passed through (message/source filters, `Group`).
Omitted keys keep upstream defaults. Unknown keys are a hard error, and so is any value upstream
would refuse at startup — a port outside 1–65535, a hostname where it takes only an IP literal
(IPv6 bracketed and lowercase, `"[::1]"`), an unknown log level: the router restarts on failure,
so a config it rejects would otherwise be a crash loop on the vehicle rather than an error at `up`.

Notes:

- **Serial**: use the stable `/dev/serial/by-id/...` path — it doubles as the in-container device
  path (the serial compose overlay maps it 1:1). At most one UART endpoint per instance (the
  overlay carries one device slot). `by-path` paths are rejected: their colons break the mapping.
- **Health**: the container healthcheck TCP-connects to `general.tcp_server_port` (default 5760);
  set it to `0` (server disabled) and the probe degrades to process liveness.
- **Logging**: `logging.enabled: true` adds a compose overlay binding `$RIG_DATA_DIR` into the
  container. Flight logs go into the open rig run, `$RIG_DATA_DIR/current/mavlink-router/<name>`
  (the container resolves `current` once at start, so a log stays in the run it began in), or
  flat into `$RIG_DATA_DIR/mavlink-router/<name>` when there is no run registry. Upstream keeps
  logs forever by default — set `max_log_files` or `min_free_space_mb`; both act on the directory
  being written, so under rig they prune within the open run and never touch a sealed one.
- **Diagnostics** go to stderr (no `--syslog` in the image), so `rig logs` / `docker logs` see them.

## Choosing upstream vs fork

```sh
MAVROUTER_SOURCE=upstream tools/build_image.sh <registry> [tag]   # official repo
MAVROUTER_SOURCE=fork     tools/build_image.sh <registry> [tag]   # the latency-fix fork
tools/build_image.sh <registry> [tag]                             # whatever sources/DEFAULT says
MAVROUTER_SOURCE=fork rig build mavlink-router                    # the same toggle under rig
```

Both pins are exact SHAs, both are built by every CI run on both architectures, and the image
records its origin as OCI labels — `docker image inspect` (`org.opencontainers.image.source` /
`.revision`) answers "what is this vehicle actually running?", as does `mavlink-routerd --version`
(`git describe` of the pin for a cloned build, its short SHA for a build from a rig-exported
tree). Flip `sources/DEFAULT` to `fork` (a committed one-word change) when the fork should become
the fleet default.

## Under rig

`rigging.yaml` declares the whole contract; `rig certify --repo . --require-build-check` checks it.

- **Portable builds**: `rig build` passes `RIG_BUILD_PLATFORMS` (`linux/arm64,linux/amd64`); the
  script publishes that set with Buildx and reports the pushed digest to `RIG_BUILD_RESULT`.
  `rig build --no-cache` is honored (`RIG_BUILD_NO_CACHE`).
- **Offline builds**: both source pins are declared as `build.sources`, so rig mirrors them
  (`rig src sync`) and hands the build an exported tree instead of letting the Dockerfile clone —
  `rig build --offline` needs no git remote. The Dockerfile names no `# syntax=` frontend for the
  same reason. `apk add` still needs the network the first time; after that it is a cached layer.
- **Image planning**: the launcher answers `images` (for bundled fleet artifacts) and
  `runtime-images` (for replay/simulation sessions) without rendering or starting anything, and
  starts exactly the image IDs rig validated when a session sets `RIG_RUNTIME_IMAGE_PINS`.
- **Replay**: `replay: {start: false}` — rig does not start the router in a replay session
  unless the deployment asks (`replay.services`, a row's `replay: {start: true}`, or
  `--include-service mavlink-router`). It is a link to live hardware with nothing to play back.
- **Runs**: flight logs are written into the open run (see Logging above), so they are sealed,
  exported and synced with the rest of that run's data.

## Updating a source

Bump the full commit SHA in **both** places it is written — `version:` in `sources/<name>.repos`
(what the build script reads) and the matching `ref:` under `build.sources` in `rigging.yaml`
(what rig mirrors for offline builds); for the upstream pin, also the `MAVROUTER_REF` default in
the Dockerfile. `tools/test_contract.py` fails CI if they disagree, and the build script refuses
a mismatch at build time. Then rebuild, skim the changelog for renamed config keys, and commit as
e.g. `vendor mavlink-router <ref>`. The coupling surface to upstream is deliberately small: the
`main.conf` key names and value rules in `tools/render_conf.py`, the `-c`/`--log`/`--version` CLI
in the entrypoint, and the meson build invocation in the Dockerfile.

## Testing

```sh
python3 -m unittest discover -s tools -p 'test_*.py' -v     # renderer, pins, build script, image verbs
shellcheck -x mavlink-router-up tools/*.sh docker/entrypoints/*.sh
tools/smoke_test.sh                                         # real container: needs a local image
rig certify --repo . --require-build-check                  # rig's launcher + build contracts
```

## Releasing

`gh release create vX.Y.Z --generate-notes` — the `registry-release` workflow rewrites this
service's manifest in `rig-registry-public` (version + tagged rev) and regenerates the index.
One-time setup: a `RIG_REGISTRY_TOKEN` Actions secret (fine-grained PAT, Contents read/write on
`rig-registry-public` only).

## License

Apache-2.0 for this wrapper. Upstream mavlink-router keeps its own license; it is fetched at
build time and never vendored into this repo.
