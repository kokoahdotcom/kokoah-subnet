#!/usr/bin/env bash
# Reproducible build of the sealed machine image (specification v6, section 16). Builds from a clean export of a
# commit (never the working tree), with every timestamp set to the commit's time and no build record attached, and
# prints the image digest: the same commit gives the same digest, so anyone can check an approved image.
#   deploy/agent/build.sh [commit] [image name]        (defaults: HEAD, kokoahdotcom/kokoah-subnet-agent)
# Needs Docker with the containerd image store (the default since Docker 29). Push with `docker push <name>:<tag>`.
set -euo pipefail
umask 022

commit=${1:-HEAD}
name=${2:-kokoahdotcom/kokoah-subnet-agent}
root=$(git rev-parse --show-toplevel)
tag=$(git -C "$root" rev-parse --short=12 "$commit")
epoch=$(git -C "$root" log -1 --format=%ct "$commit")
ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT

git -C "$root" archive "$commit" deploy/agent src/kokoah_subnet/__init__.py src/kokoah_subnet/agent | tar x -C "$ctx"
rm -f "$ctx/src/kokoah_subnet/agent/images.py"  # the approved list stays out of the image it approves
docker buildx build --no-cache --pull=false --provenance=false --sbom=false \
  --build-arg SOURCE_DATE_EPOCH="$epoch" \
  --output "type=image,name=$name:$tag,rewrite-timestamp=true,oci-mediatypes=true,unpack=false" \
  -f "$ctx/deploy/agent/Dockerfile" "$ctx" >&2
echo "$name:$tag"
docker image inspect --format '{{.Id}}' "$name:$tag"
