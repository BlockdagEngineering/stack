#!/bin/sh
# Fix ownership of persisted paths when needed. Named/bind volumes are often
# populated as root, but recursively chowning full chain data on every start
# wastes IO after the first successful repair.
set -eu

if [ -n "${NODEWORKER_EXTRA_ARGS:-}" ]; then
  # Intended for simple flag lists such as:
  #   --health.mining-readiness-timeout=2m --health.mining-readiness-grace=2m
  # shellcheck disable=SC2086
  set -- "$@" $NODEWORKER_EXTRA_ARGS
fi

fix_volume_ownership() {
  path="$1"
  mode="${BDAG_FIX_VOLUME_OWNERSHIP:-auto}"
  marker="$path/.bdagStack-ownership-ok"
  owner="$(stat -c '%u:%g' "$path" 2>/dev/null || true)"
  target="$(id -u bdagStack):$(getent group bdagStack | cut -d: -f3)"

  case "$mode" in
    never)
      echo "Skipping ownership repair for $path (BDAG_FIX_VOLUME_OWNERSHIP=never)"
      return 0
      ;;
    auto|"")
      if [ -f "$marker" ] && [ "$owner" = "$target" ]; then
        echo "Ownership already repaired for $path"
        return 0
      fi
      ;;
    always)
      ;;
    *)
      echo "Invalid BDAG_FIX_VOLUME_OWNERSHIP=$mode; use auto, always, or never" >&2
      return 1
      ;;
  esac

  echo "Repairing ownership for $path"
  if chown -R bdagStack:bdagStack "$path"; then
    touch "$marker" || true
    chown bdagStack:bdagStack "$marker" || true
  else
    echo "Ownership repair for $path failed; continuing without marker" >&2
  fi
}

if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  fix_volume_ownership /var/lib/bdagStack/node
  fix_volume_ownership /var/lib/bdagStack/nodeworker
  fix_volume_ownership /var/log/bdagStack
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
