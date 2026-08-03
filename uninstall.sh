#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/xtatech-lora-bridge"
SERVICE_NAME="xtatech-lora-bridge"
SUDOERS_FILE="/etc/sudoers.d/xtatech-lora-bridge-watchdog"
WIFI_POWERSAVE_FILE="/etc/NetworkManager/conf.d/wifi-powersave.conf"
PURGE_DOWNLOADS=0

usage() {
  echo "Usage: sudo ./uninstall.sh [--purge-downloads]"
  echo
  echo "Removes the installed Xtatech LoRa Bridge service and app files."
  echo "Use --purge-downloads to also remove ~/Downloads/Xtatech Lora Bridge for the sudo user."
}

for arg in "$@"; do
  case "$arg" in
    --purge-downloads)
      PURGE_DOWNLOADS=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $arg"
      usage
      exit 1
      ;;
  esac
done

echo "== Xtatech LoRa Bridge uninstaller =="

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:"
  echo "  sudo ./uninstall.sh"
  exit 1
fi

RUN_USER="${SUDO_USER:-user}"
RUN_HOME=""
if id "$RUN_USER" >/dev/null 2>&1; then
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
fi

echo "== Stop and disable service =="
if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
  systemctl stop "${SERVICE_NAME}.service" || true
  systemctl disable "${SERVICE_NAME}.service" || true
else
  echo "Service unit is not registered."
fi

echo "== Remove systemd unit =="
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}.service" || true

echo "== Remove sudoers permissions =="
rm -f "$SUDOERS_FILE"

echo "== Remove installed app directory =="
if [[ "$APP_DIR" == "/opt/xtatech-lora-bridge" ]]; then
  rm -rf "$APP_DIR"
else
  echo "Refusing to remove unexpected APP_DIR: $APP_DIR"
  exit 1
fi

echo "== Remove Xtatech Wi-Fi power-save override =="
if [[ -f "$WIFI_POWERSAVE_FILE" ]] && grep -q "wifi.powersave = 2" "$WIFI_POWERSAVE_FILE"; then
  rm -f "$WIFI_POWERSAVE_FILE"
  systemctl restart NetworkManager || true
else
  echo "No Xtatech Wi-Fi power-save override found."
fi

echo "== Unmask sleep targets =="
systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

if [[ $PURGE_DOWNLOADS -eq 1 ]]; then
  if [[ -n "$RUN_HOME" ]]; then
    DOWNLOAD_REPO="${RUN_HOME}/Downloads/Xtatech Lora Bridge"
    echo "== Remove Downloads checkout =="
    if [[ "$DOWNLOAD_REPO" == "${RUN_HOME}/Downloads/Xtatech Lora Bridge" ]]; then
      rm -rf "$DOWNLOAD_REPO"
    else
      echo "Refusing to remove unexpected Downloads path: $DOWNLOAD_REPO"
      exit 1
    fi
  else
    echo "Skipping Downloads purge because sudo user home could not be resolved."
  fi
else
  echo "== Keep Downloads checkout =="
  echo "Use --purge-downloads to remove ~/Downloads/Xtatech Lora Bridge."
fi

echo
echo "== Uninstalled =="
echo "Note: OS packages installed by install.sh were left in place."
echo "Note: usbcore.autosuspend=-1 was not removed from boot cmdline automatically."
