#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DOCKER_BIN="${DOCKER:-docker}"
SERVICES="${BDAG_SAVEPOINT_SERVICES:-node pool dashboard watchdog status-sampler sentinel}"
COMPRESS="${BDAG_SAVEPOINT_COMPRESS:-1}"

usage() {
  cat <<'USAGE'
Usage: scripts/create-live-savepoint.sh [--root PATH] [--stamp STAMP]

Creates a rollback savepoint under ops/runtime/savepoints/STAMP.

The script fails closed unless it can:
  - render the compose configuration;
  - verify the configured Dockerfile exists;
  - inspect every required running or exited service container;
  - tag each service image with :savepoint-STAMP;
  - export those image tags into a Docker archive; and
  - write checksums and manifests.

This saves config/provenance/images only. It does not copy chain data volumes.
USAGE
}

die() {
  printf 'create-live-savepoint: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      ROOT="$(cd "${2:?--root requires a path}" && pwd)"
      shift 2
      ;;
    --stamp)
      STAMP="${2:?--stamp requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

command -v "$DOCKER_BIN" >/dev/null 2>&1 || die "docker command not found: $DOCKER_BIN"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"

docker_tag_component() {
  local raw="$1" safe
  safe="$(printf '%s' "$raw" | sed -E 's/[^A-Za-z0-9_.-]+/-/g; s/^[^A-Za-z0-9_]+//')"
  safe="${safe:0:128}"
  if [[ -z "$safe" || ! "$safe" =~ ^[A-Za-z0-9_] ]]; then
    safe="sp-${safe}"
    safe="${safe:0:128}"
  fi
  printf '%s\n' "$safe"
}

ENV_FILE="$ROOT/.env"
COMPOSE_FILE="$ROOT/docker-compose.yml"
SAVEPOINT="$ROOT/ops/runtime/savepoints/$STAMP"
IMAGES_DIR="$SAVEPOINT/images"
MANIFEST="$SAVEPOINT/manifest.tsv"
TAG_STAMP="$(docker_tag_component "$STAMP")"

mkdir -p "$IMAGES_DIR"
printf '%s\n' "$TAG_STAMP" > "$SAVEPOINT/docker-tag-stamp"

env_value() {
  local key="$1" file="$2" line value
  [[ -f "$file" ]] || return 1
  line="$(grep -E "^[[:space:]]*$key=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  printf '%s\n' "$value"
}

copy_if_present() {
  local src="$1" dest="$2"
  if [[ -e "$src" ]]; then
    cp -a "$src" "$dest"
  fi
}

render_compose() {
  [[ -f "$COMPOSE_FILE" ]] || die "docker-compose.yml not found at $COMPOSE_FILE"
  if [[ -f "$ENV_FILE" ]]; then
    "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config > "$SAVEPOINT/docker-compose.rendered.yml"
  else
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" config > "$SAVEPOINT/docker-compose.rendered.yml"
  fi
}

verify_dockerfile() {
  local dockerfile
  dockerfile="$(env_value DOCKERFILE "$ENV_FILE" || printf 'dockerfile')"
  [[ -n "$dockerfile" ]] || dockerfile="dockerfile"
  [[ "$dockerfile" != /* ]] || die "DOCKERFILE must be relative to the stack root: $dockerfile"
  [[ -f "$ROOT/$dockerfile" ]] || die "configured Dockerfile is missing: $ROOT/$dockerfile"
  printf '%s\n' "$dockerfile" > "$SAVEPOINT/dockerfile.path"
}

record_git() {
  local repo
  : > "$SAVEPOINT/git-revisions.tsv"
  for repo in "$ROOT" "$ROOT/../blockdag-corechain" "$ROOT/../pool" "$ROOT/../redis-dash"; do
    if [[ -d "$repo/.git" ]]; then
      printf '%s\t%s\t%s\n' \
        "$(basename "$repo")" \
        "$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || true)" \
        "$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)" \
        >> "$SAVEPOINT/git-revisions.tsv"
    fi
  done
}

capture_baseline() {
  copy_if_present "$ENV_FILE" "$SAVEPOINT/env.before"
  copy_if_present "$ROOT/node.conf" "$SAVEPOINT/node.conf.before"
  "$DOCKER_BIN" ps -a > "$SAVEPOINT/docker-ps.before.txt" || die "failed to capture docker ps"
  "$DOCKER_BIN" images -a --digests > "$SAVEPOINT/docker-images.before.txt" || die "failed to capture docker images"
  render_compose
  verify_dockerfile
  record_git
}

preserve_images() {
  local service image_id image_ref tag tar archive checksum
  local tags=()

  : > "$MANIFEST"
  printf 'service\tcontainer_image\timage_id\tsavepoint_tag\n' >> "$MANIFEST"

  for service in $SERVICES; do
    image_id="$("$DOCKER_BIN" inspect -f '{{.Image}}' "$service" 2>/dev/null || true)"
    [[ -n "$image_id" && "$image_id" != "<no value>" ]] || die "container not found or has no image: $service"

    image_ref="$("$DOCKER_BIN" inspect -f '{{.Config.Image}}' "$service" 2>/dev/null || true)"
    [[ -n "$image_ref" && "$image_ref" != "<no value>" ]] || image_ref="unknown"

    "$DOCKER_BIN" image inspect "$image_id" > "$IMAGES_DIR/$service-image.json" \
      || die "image preservation failed; Docker cannot inspect image for $service ($image_id)"

    tag="stack-$service:savepoint-$TAG_STAMP"
    "$DOCKER_BIN" tag "$image_id" "$tag" \
      || die "image preservation failed; Docker cannot tag image for $service ($image_id)"

    printf '%s\t%s\t%s\t%s\n' "$service" "$image_ref" "$image_id" "$tag" >> "$MANIFEST"
    tags+=("$tag")
  done

  tar="$IMAGES_DIR/stack-images-savepoint-$TAG_STAMP.tar"
  "$DOCKER_BIN" save -o "$tar" "${tags[@]}" \
    || die "image preservation failed; Docker could not export savepoint images"

  if [[ "$COMPRESS" == "1" ]]; then
    command -v zstd >/dev/null 2>&1 || die "zstd is required when BDAG_SAVEPOINT_COMPRESS=1"
    archive="$tar.zst"
    zstd -T0 -19 -f "$tar" -o "$archive"
    rm -f "$tar"
  else
    archive="$tar"
  fi

  [[ -s "$archive" ]] || die "image preservation failed; archive is empty: $archive"
  checksum="$archive.sha256"
  sha256sum "$archive" > "$checksum"
  printf '%s\n' "$archive" > "$SAVEPOINT/image-archive.path"
}

write_summary() {
  {
    printf 'Savepoint: %s\n' "$STAMP"
    printf 'Root: %s\n' "$ROOT"
    printf 'Services: %s\n' "$SERVICES"
    printf 'Image archive: %s\n' "$(cat "$SAVEPOINT/image-archive.path")"
    printf '\nRestore prerequisite:\n'
    printf '  sha256sum -c %s.sha256\n' "$(cat "$SAVEPOINT/image-archive.path")"
    if [[ "$(cat "$SAVEPOINT/image-archive.path")" == *.zst ]]; then
      printf '  zstd -dc %s | docker load\n' "$(cat "$SAVEPOINT/image-archive.path")"
    else
      printf '  docker load -i %s\n' "$(cat "$SAVEPOINT/image-archive.path")"
    fi
  } > "$SAVEPOINT/savepoint-summary.txt"
}

capture_baseline
preserve_images
write_summary

printf 'Created live savepoint: %s\n' "$SAVEPOINT"
