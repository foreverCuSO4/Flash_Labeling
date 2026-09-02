# 1. 立即生效（对当前正在运行的 mihomo）
sudo ip rule add sport 8000 table main priority 90
sudo ip rule add dport 8000 table main priority 90

# 2. 持久化：挂到 mihomo.service 生命周期上，随服务启停自动增删
sudo mkdir -p /etc/systemd/system/mihomo.service.d
sudo tee /etc/systemd/system/mihomo.service.d/ssh-bypass.conf <<'EOF'
[Service]
ExecStartPost=+/bin/sh -c 'ip rule add sport 8000 table main priority 90 2>/dev/null || true'
ExecStartPost=+/bin/sh -c 'ip rule add dport 8000 table main priority 90 2>/dev/null || true'
ExecStopPost=+/bin/sh -c 'ip rule del sport 8000 table main priority 90 2>/dev/null || true'
ExecStopPost=+/bin/sh -c 'ip rule del dport 8000 table main priority 90 2>/dev/null || true'
EOF
sudo systemctl daemon-reload
