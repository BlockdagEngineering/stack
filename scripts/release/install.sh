#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS_NAME=$(uname -s 2>/dev/null || echo unknown)
ARCH_NAME=$(uname -m 2>/dev/null || echo unknown)

case "$ARCH_NAME" in
  x86_64|amd64) BDAG_INSTALL_ARCH=amd64 ;;
  arm64|aarch64) BDAG_INSTALL_ARCH=arm64 ;;
  *)
    echo "Unsupported CPU architecture: $ARCH_NAME" >&2
    exit 1
    ;;
esac
export BDAG_INSTALL_ARCH

case "$OS_NAME" in
  Linux)
    export BDAG_INSTALL_OS=linux
    exec sh "$SCRIPT_DIR/installers/install-linux.sh" "$@"
    ;;
  Darwin)
    export BDAG_INSTALL_OS=macos
    exec sh "$SCRIPT_DIR/installers/install-macos.sh" "$@"
    ;;
  MINGW*|MSYS*|CYGWIN*)
    if command -v powershell.exe >/dev/null 2>&1; then
      exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/install.ps1" "$@"
    fi
    echo "Windows detected, but powershell.exe was not found. Run install.cmd or install.ps1 from Windows." >&2
    exit 1
    ;;
  *)
    echo "Unsupported operating system: $OS_NAME" >&2
    exit 1
    ;;
esac
