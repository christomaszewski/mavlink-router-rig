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
# A plain-HTTP local registry needs the daemon's insecure-registries on this host (with the :port).
# Cross-arch vehicle: export MAVROUTER_BUILD_PLATFORM=linux/<arch> (default: host-native). The
# build is a small meson/ninja compile — fine under qemu, unlike the heavy ROS images.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/mavlink_router:${TAG}"

SOURCE="${MAVROUTER_SOURCE:-$(tr -d '[:space:]' < "$REPO/sources/DEFAULT")}"
PIN="$REPO/sources/${SOURCE}.repos"
if [ ! -f "$PIN" ]; then
  echo "build_image.sh: unknown MAVROUTER_SOURCE '$SOURCE' — expected a sources/<name>.repos file" \
       "(upstream | fork)" >&2
  exit 1
fi

# The pin files are fixed-shape two-key YAML (see their headers) — a full YAML parser is overkill.
URL="$(awk '$1 == "url:" {print $2}' "$PIN")"
REF="$(awk '$1 == "version:" {print $2}' "$PIN")"
if [ -z "$URL" ] || [ -z "$REF" ]; then
  echo "build_image.sh: $PIN must define url: and version:" >&2
  exit 1
fi

echo "build_image.sh: source=$SOURCE url=$URL ref=$REF" >&2
docker build ${MAVROUTER_BUILD_PLATFORM:+--platform "${MAVROUTER_BUILD_PLATFORM}"} \
  --build-arg MAVROUTER_URL="$URL" --build-arg MAVROUTER_REF="$REF" \
  -f "$REPO/docker/Dockerfile.runtime" -t "$IMAGE" "$REPO"
docker push "$IMAGE"
echo "build_image.sh: pushed $IMAGE" >&2
