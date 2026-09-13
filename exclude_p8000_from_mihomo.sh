# Traffic that must never enter mihomo's TUN policy table. The Kubernetes
# ranges are the default pod and service CIDRs used by this cluster.
# Ports 80/443 are intentionally NOT bypassed so that fake-ip destinations
# (e.g. chatgpt.com -> 198.18.0.x) stay inside mihomo's TUN.
RULES=(
  "to 10.244.0.0/16 table main priority 80"
  "to 10.96.0.0/12 table main priority 80"
  "sport 8000 table main priority 90"
  "dport 8000 table main priority 90"
)

add_rules() {
  for rule in "${RULES[@]}"; do
    sudo ip rule add $rule 2>/dev/null || true
  done
}

del_rules() {
  for rule in "${RULES[@]}"; do
    sudo ip rule del $rule 2>/dev/null || true
  done
}

# 1. 立即生效（对当前正在运行的 mihomo）
add_rules

# 2. 持久化：挂到 mihomo.service 生命周期上，随服务启停自动增删
sudo mkdir -p /etc/systemd/system/mihomo.service.d
sudo tee /etc/systemd/system/mihomo.service.d/ssh-bypass.conf <<'EOF'
[Service]
ExecStartPost=+/bin/sh -c 'ip rule add to 10.244.0.0/16 table main priority 80 2>/dev/null || true; ip rule add to 10.96.0.0/12 table main priority 80 2>/dev/null || true; ip rule add sport 8000 table main priority 90 2>/dev/null || true; ip rule add dport 8000 table main priority 90 2>/dev/null || true'
ExecStopPost=+/bin/sh -c 'ip rule del to 10.244.0.0/16 table main priority 80 2>/dev/null || true; ip rule del to 10.96.0.0/12 table main priority 80 2>/dev/null || true; ip rule del sport 8000 table main priority 90 2>/dev/null || true; ip rule del dport 8000 table main priority 90 2>/dev/null || true'
EOF
sudo systemctl daemon-reload
