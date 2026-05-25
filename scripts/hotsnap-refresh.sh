#!/bin/sh
set -eu

DOCKER_BIN="${DOCKER_BIN:-docker}"
DATADIR="${BDAG_HOTSNAP_DATADIR:-/var/lib/bdagStack/node/mainnet}"
ARCHIVE="${BDAG_HOTSNAP_ARCHIVE:-$DATADIR/snapshot.bdsnap}"
REFRESH_ARCHIVE="$ARCHIVE.refresh"
INTERVAL="${BDAG_HOTSNAP_INTERVAL:-1h}"
INITIAL_DELAY="${BDAG_HOTSNAP_INITIAL_DELAY:-20m}"
STOP_TIMEOUT="${BDAG_HOTSNAP_STOP_TIMEOUT:-60}"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

sleep_forever() {
  while :; do
    sleep "${BDAG_HOTSNAP_DISABLED_SLEEP:-3600}"
  done
}

compose_project() {
  cid="$(hostname 2>/dev/null || true)"
  [ -n "$cid" ] || return 0
  "$DOCKER_BIN" inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$cid" 2>/dev/null || true
}

find_target_container() {
  if [ -n "${BDAG_HOTSNAP_TARGET_CONTAINER:-}" ]; then
    printf '%s\n' "$BDAG_HOTSNAP_TARGET_CONTAINER"
    return 0
  fi

  project="$(compose_project)"
  if [ -n "$project" ]; then
    target="$("$DOCKER_BIN" ps -aq \
      --filter "label=com.docker.compose.project=$project" \
      --filter "label=com.docker.compose.service=snapshot-node" \
      | head -n 1)"
    if [ -n "$target" ]; then
      printf '%s\n' "$target"
      return 0
    fi
  fi

  for candidate in \
    pool-stack-docker-snapshot-node-1 \
    snapshot-node-node-1 \
    pool-stack-docker-node-1
  do
    if "$DOCKER_BIN" ps -a --format '{{.Names}}' | grep -qx "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

container_image() {
  "$DOCKER_BIN" inspect -f '{{.Image}}' "$1"
}

run_in_target_volumes() {
  target="$1"
  image="$2"
  shift 2
  entrypoint="$1"
  shift
  "$DOCKER_BIN" run --rm --volumes-from "$target" --entrypoint "$entrypoint" "$image" "$@"
}

target_ready_for_export() {
  target="$1"
  image="$2"
  run_in_target_volumes "$target" "$image" /bin/sh -c "test -d '$DATADIR/BdagChain' && test ! -f '$DATADIR/fastsync-v2.importing'"
}

cleanup_refresh_files() {
  target="$1"
  image="$2"
  run_in_target_volumes "$target" "$image" /bin/sh -c "rm -f '$REFRESH_ARCHIVE' '$REFRESH_ARCHIVE.manifest.json'"
}

publish_refresh() {
  target="$1"
  image="$2"
  run_in_target_volumes "$target" "$image" /bin/sh -c "
    set -eu
    test -s '$REFRESH_ARCHIVE'
    mv -f '$REFRESH_ARCHIVE' '$ARCHIVE'
    if [ -f '$REFRESH_ARCHIVE.manifest.json' ]; then
      mv -f '$REFRESH_ARCHIVE.manifest.json' '$ARCHIVE.manifest.json'
    fi
    chown bdagStack:bdagStack '$ARCHIVE' '$ARCHIVE.manifest.json' 2>/dev/null || true
  "
}

export_snapshot() {
  target="$1"
  image="$2"
  if [ -n "${BDAG_HOTSNAP_CHUNK_SIZE:-}" ]; then
    run_in_target_volumes "$target" "$image" /usr/local/bin/blockdag-node \
      snap export --datadir "$DATADIR" --path "$REFRESH_ARCHIVE" --chunk-size "$BDAG_HOTSNAP_CHUNK_SIZE"
  else
    run_in_target_volumes "$target" "$image" /usr/local/bin/blockdag-node \
      snap export --datadir "$DATADIR" --path "$REFRESH_ARCHIVE"
  fi
}

run_once() {
  target="$(find_target_container)" || {
    log "hot snapshot refresh skipped; no snapshot-node container found"
    return 1
  }
  image="$(container_image "$target")"
  if ! target_ready_for_export "$target" "$image"; then
    log "hot snapshot refresh skipped; $target has no complete chain database at $DATADIR yet"
    return 1
  fi

  was_running="$("$DOCKER_BIN" inspect -f '{{.State.Running}}' "$target")"
  stopped=0
  if [ "$was_running" = "true" ]; then
    log "stopping $target for consistent snapshot export"
    "$DOCKER_BIN" stop --time "$STOP_TIMEOUT" "$target" >/dev/null
    stopped=1
  fi

  restart_target() {
    if [ "$stopped" = "1" ]; then
      log "starting $target"
      "$DOCKER_BIN" start "$target" >/dev/null 2>&1 || true
      stopped=0
    fi
  }
  trap restart_target EXIT INT TERM

  cleanup_refresh_files "$target" "$image"
  log "exporting hot staged snapshot from $target to $ARCHIVE"
  export_snapshot "$target" "$image"
  publish_refresh "$target" "$image"
  log "hot staged snapshot published at $ARCHIVE"

  restart_target
  trap - EXIT INT TERM
}

main() {
  if ! truthy "${BDAG_HOTSNAP_ENABLED:-1}"; then
    log "hot staged snapshots disabled by BDAG_HOTSNAP_ENABLED"
    [ "${1:-}" = "loop" ] && sleep_forever
    exit 0
  fi

  if [ "${1:-}" = "loop" ]; then
    if [ "$INITIAL_DELAY" != "0" ]; then
      log "hot snapshot refresher waiting initial delay $INITIAL_DELAY"
      sleep "$INITIAL_DELAY"
    fi
    while :; do
      run_once || true
      log "next hot snapshot refresh in $INTERVAL"
      sleep "$INTERVAL"
    done
  fi

  run_once
}

main "$@"
