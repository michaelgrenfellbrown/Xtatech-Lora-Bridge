#!/usr/bin/env bash
set -euo pipefail

CERT_DIR="${1:-certs}"
mkdir -p "$CERT_DIR"

HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
SAN="DNS:localhost,DNS:${HOSTNAME_FQDN},DNS:$(hostname),IP:127.0.0.1"

if [[ -n "$IP_ADDR" ]]; then
  SAN="${SAN},IP:${IP_ADDR}"
fi

openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$CERT_DIR/local.key" \
  -out "$CERT_DIR/local.crt" \
  -subj "/CN=${HOSTNAME_FQDN}" \
  -addext "subjectAltName=${SAN}"

chmod 600 "$CERT_DIR/local.key"

echo "Created $CERT_DIR/local.crt and $CERT_DIR/local.key"
echo "Use https://localhost:8088 or https://<this-device-ip>:8088"
