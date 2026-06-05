#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/xtatech-lora-bridge"
SERVICE_NAME="xtatech-lora-bridge"
PY_BIN="python3"

echo "== Xtatech LoRa Bridge installer (v2) =="

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:"
  echo "  sudo ./install.sh"
  exit 1
fi

RUN_USER="${SUDO_USER:-user}"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "User '$RUN_USER' not found."
  exit 1
fi

echo "Using service user: $RUN_USER"

echo "== Install OS dependencies =="
apt-get update
apt-get install -y --no-install-recommends   ${PY_BIN} ${PY_BIN}-venv ${PY_BIN}-pip   ca-certificates   openssl   git   mosquitto-clients   network-manager

echo "== Create app directory =="
mkdir -p "$APP_DIR/web"
mkdir -p "$APP_DIR/certs"

echo "== Copy files into $APP_DIR =="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -f "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
cp -f "$SCRIPT_DIR/serial_probe.py" "$APP_DIR/serial_probe.py"
cp -f "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp -f "$SCRIPT_DIR/config.yaml" "$APP_DIR/config.yaml"
cp -f "$SCRIPT_DIR/web/index.html" "$APP_DIR/web/index.html"
cp -f "$SCRIPT_DIR/web/diagnostics.html" "$APP_DIR/web/diagnostics.html"
cp -f "$SCRIPT_DIR/web/services.html" "$APP_DIR/web/services.html"
cp -f "$SCRIPT_DIR/web/metrics.html" "$APP_DIR/web/metrics.html"
cp -f "$SCRIPT_DIR/web/ssh.html" "$APP_DIR/web/ssh.html"
cp -f "$SCRIPT_DIR/web/terminal.html" "$APP_DIR/web/terminal.html"
cp -f "$SCRIPT_DIR/web/xtatech.png" "$APP_DIR/web/xtatech.png"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "== Create local HTTPS certificate if missing =="
if [[ ! -f "$APP_DIR/certs/local.crt" || ! -f "$APP_DIR/certs/local.key" ]]; then
  HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
  IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
  SAN="DNS:localhost,DNS:${HOSTNAME_FQDN},DNS:$(hostname),IP:127.0.0.1"
  if [[ -n "$IP_ADDR" ]]; then
    SAN="${SAN},IP:${IP_ADDR}"
  fi

  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$APP_DIR/certs/local.key" \
    -out "$APP_DIR/certs/local.crt" \
    -subj "/CN=${HOSTNAME_FQDN}" \
    -addext "subjectAltName=${SAN}"

  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/certs/local.crt" "$APP_DIR/certs/local.key"
  chmod 600 "$APP_DIR/certs/local.key"
fi

echo "== Create venv and install Python requirements =="
sudo -u "$RUN_USER" bash -lc "
  cd '$APP_DIR'
  ${PY_BIN} -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
"

echo "== Configure local terminal token =="
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/python" - <<PY
from pathlib import Path
import secrets
import yaml

config_path = Path("$APP_DIR/config.yaml")
cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
terminal = cfg.setdefault("terminal", {})
terminal.setdefault("enabled", True)
token = terminal.get("token")
if not token or token == "changeme":
    terminal["token"] = secrets.token_hex(12)
terminal.setdefault("shell", "/bin/bash")
terminal.setdefault("cwd", "$APP_DIR")
terminal.setdefault("max_session_seconds", 3600)
config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
PY

echo "== Serial permissions =="
usermod -aG dialout "$RUN_USER" || true

echo "== Disable system sleep targets (never suspend) =="
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

echo "== Disable USB autosuspend (prevents ttyACM drop) =="
CMDLINE_FILE=""
if [[ -f /boot/firmware/cmdline.txt ]]; then
  CMDLINE_FILE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
  CMDLINE_FILE="/boot/cmdline.txt"
fi
if [[ -n "$CMDLINE_FILE" ]]; then
  if ! grep -q "usbcore.autosuspend=-1" "$CMDLINE_FILE"; then
    sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE_FILE"
    echo "Added usbcore.autosuspend=-1 to $CMDLINE_FILE (reboot recommended)"
  fi
fi

echo "== Disable Wi-Fi power saving =="
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/wifi-powersave.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
systemctl restart NetworkManager || true

echo "== Allow service user to reboot via watchdog (sudoers) =="
cat >/etc/sudoers.d/xtatech-lora-bridge-watchdog <<EOF
${RUN_USER} ALL=NOPASSWD: /usr/bin/systemctl reboot
EOF
chmod 440 /etc/sudoers.d/xtatech-lora-bridge-watchdog

echo "== Install systemd service =="
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Xtatech LoRa -> MQTT Bridge with Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/app.py
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo
echo "== Installed and started =="
systemctl --no-pager status "${SERVICE_NAME}.service" || true
echo
echo "RECOMMENDED: reboot once to apply USB autosuspend change"
echo "  sudo reboot"
