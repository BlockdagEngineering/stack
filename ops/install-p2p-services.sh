#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
P2P_PORT="${P2P_PORT:-8150}"
P2P_PROTOCOLS="${BDAG_P2P_PROTOCOLS:-tcp}"

warn() { printf 'WARNING: %s\n' "$*" >&2; }

need_sudo() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_firewall() {
  if [[ ! -f "$ROOT/ops/allow-p2p-iptables.sh" || ! -f "$ROOT/ops/systemd/bdag-p2p-firewall.service" ]]; then
    warn "P2P firewall files are missing under $ROOT/ops"
    return 0
  fi
  need_sudo install -m 0755 "$ROOT/ops/allow-p2p-iptables.sh" /usr/local/sbin/bdag-allow-p2p-iptables
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-p2p-firewall.service" /etc/systemd/system/bdag-p2p-firewall.service
  printf 'P2P_PORT=%s\nBDAG_P2P_PROTOCOLS=%s\n' "$P2P_PORT" "$P2P_PROTOCOLS" | need_sudo tee /etc/default/bdag-p2p-firewall >/dev/null
  need_sudo systemctl daemon-reload
  need_sudo systemctl enable --now bdag-p2p-firewall.service
}

install_local_peer_timer() {
  if [[ ! -x "$ROOT/ops/update-local-peers.py" || ! -f "$ROOT/ops/systemd/user-bdag-local-peers.timer" ]]; then
    warn "Local peer discovery files are missing under $ROOT/ops"
    return 0
  fi
  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$user_systemd_dir"
  cat > "$user_systemd_dir/bdag-local-peers.service" <<EOF
[Unit]
Description=BlockDAG local P2P peer discovery
After=default.target docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Nice=15
IOSchedulingClass=best-effort
IOSchedulingPriority=7
CPUWeight=25
IOWeight=25
ExecStart=$ROOT/ops/update-local-peers.py --apply
EOF
  install -m 0644 "$ROOT/ops/systemd/user-bdag-local-peers.timer" "$user_systemd_dir/bdag-local-peers.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now bdag-local-peers.timer
}

install_mining_host_tuning() {
  if [[ ! -x "$ROOT/ops/apply-mining-host-tuning.sh" || ! -f "$ROOT/ops/systemd/bdag-mining-host-tuning.service" || ! -f "$ROOT/ops/systemd/bdag-mining-host-tuning.timer" ]]; then
    warn "Mining host tuning files are missing under $ROOT/ops"
    return 0
  fi
  need_sudo install -m 0755 "$ROOT/ops/apply-mining-host-tuning.sh" /usr/local/sbin/bdag-apply-mining-host-tuning
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-mining-host-tuning.service" /etc/systemd/system/bdag-mining-host-tuning.service
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-mining-host-tuning.timer" /etc/systemd/system/bdag-mining-host-tuning.timer
  # The installed script runs from /usr/local/sbin under systemd, so persist
  # the release root explicitly. This lets active/passive tuning read the pool
  # metrics/env and prioritize the currently selected mining-template lane.
  printf 'BDAG_PROJECT_ROOT=%s\n' "$ROOT" | need_sudo tee /etc/default/bdag-mining-host-tuning >/dev/null
  need_sudo systemctl daemon-reload
  need_sudo systemctl enable --now bdag-mining-host-tuning.service
  need_sudo systemctl enable --now bdag-mining-host-tuning.timer
}

install_firewall
install_local_peer_timer
install_mining_host_tuning
