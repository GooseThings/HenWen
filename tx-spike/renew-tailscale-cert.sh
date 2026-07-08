#!/bin/bash
# Re-issues the Tailscale-managed TLS cert for the browser TX HTTPS vhost
# and reloads Apache only if the cert actually changed. `tailscale cert`
# itself doesn't auto-renew — this is what the systemd timer installed by
# setup-tailscale.sh calls daily; it's also safe to run by hand any time.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

HOSTNAME_FILE=/etc/asterisk/henwen-https-hostname
[ -f "$HOSTNAME_FILE" ] || { echo "No $HOSTNAME_FILE — run setup-tailscale.sh first."; exit 1; }
MAGIC_DNS_NAME=$(cat "$HOSTNAME_FILE")

CERT_DIR=/var/lib/tailscale/certs
CERT_FILE="$CERT_DIR/${MAGIC_DNS_NAME}.crt"
KEY_FILE="$CERT_DIR/${MAGIC_DNS_NAME}.key"

BEFORE=""
[ -f "$CERT_FILE" ] && BEFORE=$(sha256sum "$CERT_FILE" | awk '{print $1}')

tailscale cert --cert-file "$CERT_FILE" --key-file "$KEY_FILE" "$MAGIC_DNS_NAME"

AFTER=$(sha256sum "$CERT_FILE" | awk '{print $1}')
if [ "$BEFORE" != "$AFTER" ]; then
    echo "Certificate renewed — reloading Apache."
    apache2ctl configtest && systemctl reload apache2
else
    echo "Certificate unchanged, nothing to reload."
fi
