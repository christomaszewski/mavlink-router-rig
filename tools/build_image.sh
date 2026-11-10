#!/usr/bin/env bash
# rig's BUILD-phase entrypoint (rigging.yaml `build.command`): build the self-contained runtime
# image and push it to the fleet registry. rig invokes it as
#   tools/build_image.sh <registry> [tag]        (cwd = repo root, tag defaults to latest)
# and the deploy compose pulls the result as ${RIG_IMAGE_REGISTRY}/mavlink_router:${RIG_IMAGE_TAG}.
# Works standalone too:  tools/build_image.sh 192.168.1.10:5000 mytag
#
# SOURCE TOGGLE — which mavlink-router this image is built from:
#   MAVROUTER_SOURCE=upstream   -> sources/upstream.repos (the official repo)
#   MAVROUTER_SOURCE=fork       -> sources/fork.repos     (the personal fork with latency fixes)
# Unset = whatever sources/DEFAULT says. The pin file's url/version become build args, and the
# image records them as OCI source/revision labels (docker image inspect answers "what is this?").
#
# rig build-env contract (rig exports these; all are rig-owned, set-or-popped, so the
# ${VAR:-} expansions below are safe under set -u):
#   RIG_BUILD_PLATFORMS  comma-separated linux/arm64,linux/amd64 — publish that set with Buildx and
#                        append one {ref, digest} JSON line to RIG_BUILD_RESULT (tools/rig-build.sh)
#   RIG_BUILD_NO_CACHE   set => --no-cache, plus --pull unless RIG_BUILD_PULL=0
#   RIG_BUILD_SOURCES    rig >= v0.2.74: the rigging's `build.sources` trees at their pins, exported
#                        from rig's local git mirrors (one directory per source + sources.tsv). The
#                        selected source's tree replaces the Dockerfile's clone, so the build
#                        needs no git remote — which is what makes `rig build --offline` work.
#   RIG_OFFLINE          set => never fall back to cloning in the builder; fail by name instead
#
# A plain-HTTP local registry needs the daemon's insecure-registries on this host (with the :port).
# Cross-arch vehicle: export MAVROUTER_BUILD_PLATFORM=linux/<arch> (default: host-native). The
# build is a small meson/ninja compile — fine under qemu, unlike the heavy ROS images.
# PUSH=0 keeps a standalone build local; loading a Buildx result requires selecting exactly one
# architecture.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

die() { echo "build_image.sh: $*" >&2; exit 1; }

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/mavlink_router:${TAG}"

# Keep the standalone CPU override, but Rig's requested matrix always takes precedence.
set --
if [ -z "${RIG_BUILD_PLATFORMS:-}" ] && [ -n "${MAVROUTER_BUILD_PLATFORM:-}" ]; then
  set -- --platform "$MAVROUTER_BUILD_PLATFORM"
fi
# shellcheck source=tools/rig-build.sh
. "$REPO/tools/rig-build.sh"

SOURCE="${MAVROUTER_SOURCE:-$(tr -d '[:space:]' < "$REPO/sources/DEFAULT")}"
# The name selects a file and (under rig) a directory — keep it a plain token, never a path.
case "$SOURCE" in
  ''|*[!a-z0-9_-]*) die "invalid source name '$SOURCE' — expected a sources/<name>.repos name" \
                        "(upstream | fork)" ;;
esac
PIN="$REPO/sources/${SOURCE}.repos"
[ -f "$PIN" ] || die "unknown MAVROUTER_SOURCE '$SOURCE' — expected a sources/<name>.repos file" \
                     "(upstream | fork)"

# The pin files are fixed-shape two-key YAML (see their headers) — a full YAML parser is overkill.
URL="$(awk '$1 == "url:" {print $2}' "$PIN")"
REF="$(awk '$1 == "version:" {print $2}' "$PIN")"
if [ -z "$URL" ] || [ -z "$REF" ]; then
  die "$PIN must define url: and version:"
fi
# The ref lands in a git command line and the binary's version stamp: an exact tag or a full SHA.
case "$REF" in
  -*|*[!A-Za-z0-9._/-]*) die "$PIN: version '$REF' is not an exact tag or commit SHA" ;;
esac

# rig-exported source: use the tree rig already resolved through its mirror instead of cloning
# in the builder. sources.tsv is name<TAB>repo<TAB>ref<TAB>commit; the row must agree with the pin
# file, because rigging.yaml's `build.sources` repeats the pins (tools/test_contract.py holds the
# two together) and a drifted pair would otherwise build one rev and label it as the other.
if [ -n "${RIG_BUILD_SOURCES:-}" ] && [ -f "$RIG_BUILD_SOURCES/sources.tsv" ]; then
  row="$(awk -F'\t' -v name="$SOURCE" '$1 == name {print $2 "\t" $3 "\t" $4; exit}' \
           "$RIG_BUILD_SOURCES/sources.tsv")"
else
  row=""
fi
if [ -n "$row" ]; then
  IFS=$'\t' read -r src_repo src_ref src_commit <<EOF
$row
EOF
  if [ "$src_repo" != "$URL" ] || [ "$src_ref" != "$REF" ]; then
    die "rigging.yaml build.sources '$SOURCE' ($src_repo @ $src_ref) disagrees with $PIN" \
        "($URL @ $REF) — bump both together"
  fi
  [ -f "$RIG_BUILD_SOURCES/$SOURCE/meson.build" ] \
    || die "rig exported no usable tree at $RIG_BUILD_SOURCES/$SOURCE"
  set -- "$@" --build-context "mavrouter-src=$RIG_BUILD_SOURCES/$SOURCE"
  echo "build_image.sh: source=$SOURCE url=$URL ref=$REF (rig export, commit ${src_commit:-?})" >&2
elif [ -n "${RIG_OFFLINE:-}" ]; then
  die "offline build, but rig exported no '$SOURCE' source (RIG_BUILD_SOURCES) — declare it in" \
      "rigging.yaml build.sources and run \`rig src sync\` while online"
else
  echo "build_image.sh: source=$SOURCE url=$URL ref=$REF (cloned in the builder)" >&2
fi

rig_build "$IMAGE" "$@" \
  --build-arg MAVROUTER_URL="$URL" --build-arg MAVROUTER_REF="$REF" \
  -f "$REPO/docker/Dockerfile.runtime" "$REPO"
echo "build_image.sh: built $IMAGE (PUSH=${PUSH:-1})" >&2
