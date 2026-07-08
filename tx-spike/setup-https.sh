#!/bin/bash
# HenWen HTTPS setup — provisions Apache + a Let's Encrypt certificate.
#
# This is what makes the browser a "secure context", which the TX button
# requires (getUserMedia/WebRTC are blocked on plain http:// except on
# localhost) and gives Asterisk's SIP-over-WebSocket signaling somewhere
# to terminate TLS. Optional: the kiosk and every other HenWen feature
# work fine over plain HTTP — only run this if you want browser TX and
# have a public hostname pointed at this box.
#
# Requires: a DNS name that already resolves to this box's public IP, and
# TCP 80/443 forwarded here (certbot's HTTP-01 challenge needs port 80;
# the resulting site is served on 443). LAN-only installs should not run
# this script.
#
# Touches:  installs apache2, certbot, python3-certbot-apache
#           /etc/apache2/sites-available/henwen.conf       (new, port 80)
#           /etc/apache2/sites-available/henwen-ssl.conf   (new, port 443)
#           enables mod_ssl, mod_proxy, mod_proxy_http, mod_proxy_wstunnel,
#           mod_headers, mod_rewrite
#
# The resulting file MUST be named exactly .../henwen-ssl.conf — that path
# is hardcoded in tx-spike/apply.sh (which patches its ProxyPass line to
# add the /asterisk-ws WSS proxy) and tx-spike/check-ports.sh (which reads
# its ServerName to probe the WSS handshake). Certbot's apache plugin
# normally names its generated SSL vhost "<name>-le-ssl.conf" — this
# script renames it after the fact so both scripts keep working unmodified.
#
# Run as root: sudo bash setup-https.sh [hostname] [email]
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

PORT="${PORT:-5000}"
CONF_NAME="henwen"
HTTP_AVAIL="/etc/apache2/sites-available/${CONF_NAME}.conf"
SSL_AVAIL="/etc/apache2/sites-available/${CONF_NAME}-ssl.conf"
LE_SSL_AVAIL="/etc/apache2/sites-available/${CONF_NAME}-le-ssl.conf"

echo ""
echo "============================================"
echo "  HenWen HTTPS Setup (for Browser TX)"
echo "============================================"
echo ""

HOSTNAME_ARG="${1:-}"
if [ -z "$HOSTNAME_ARG" ]; then
    read -p "Public hostname for this box (must already resolve here, e.g. mynode.ddns.net): " HOSTNAME_ARG
fi
[ -n "$HOSTNAME_ARG" ] || { echo "A hostname is required."; exit 1; }

EMAIL_ARG="${2:-}"
if [ -z "$EMAIL_ARG" ]; then
    read -p "Email for Let's Encrypt expiry/renewal notices: " EMAIL_ARG
fi
[ -n "$EMAIL_ARG" ] || { echo "An email is required for Let's Encrypt registration."; exit 1; }

# ── DNS sanity check ───────────────────────────────────────
echo "[1/6] Checking that $HOSTNAME_ARG resolves to this box..."
PUBLIC_IP=$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || echo "")
RESOLVED_IP=$(getent ahostsv4 "$HOSTNAME_ARG" 2>/dev/null | awk '{print $1; exit}')
if [ -n "$PUBLIC_IP" ] && [ -n "$RESOLVED_IP" ] && [ "$PUBLIC_IP" != "$RESOLVED_IP" ]; then
    echo "      WARNING: $HOSTNAME_ARG resolves to $RESOLVED_IP but this box's public IP looks"
    echo "      like $PUBLIC_IP. Certbot's HTTP-01 challenge will fail unless $HOSTNAME_ARG"
    echo "      really does route here (DDNS not updated yet? port 80 not forwarded?)."
    read -p "      Continue anyway? [y/N]: " CONTINUE_ANYWAY
    [[ "$CONTINUE_ANYWAY" =~ ^[Yy] ]] || exit 1
elif [ -z "$PUBLIC_IP" ] || [ -z "$RESOLVED_IP" ]; then
    echo "      Could not verify (no outbound internet or DNS not resolving yet) — continuing."
else
    echo "      OK: $HOSTNAME_ARG -> $RESOLVED_IP matches this box's public IP."
fi

# ── Install Apache + certbot ───────────────────────────────
echo "[2/6] Installing Apache and certbot..."
apt-get install -y apache2 certbot python3-certbot-apache
a2enmod ssl proxy proxy_http proxy_wstunnel headers rewrite >/dev/null

# ── Base HTTP vhost (port 80) ──────────────────────────────
echo "[3/6] Writing Apache vhost for $HOSTNAME_ARG..."
cat > "$HTTP_AVAIL" <<VHOST
<VirtualHost *:80>
    ServerName ${HOSTNAME_ARG}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${PORT}/ retry=0 timeout=120
    ProxyPassReverse / http://127.0.0.1:${PORT}/
</VirtualHost>
VHOST
a2ensite "${CONF_NAME}.conf" >/dev/null
apache2ctl configtest
systemctl reload apache2

# ── Certbot ─────────────────────────────────────────────────
echo "[4/6] Requesting a Let's Encrypt certificate for $HOSTNAME_ARG..."
certbot --apache -d "$HOSTNAME_ARG" -m "$EMAIL_ARG" --agree-tos --redirect --non-interactive

# ── Normalize the SSL vhost filename ───────────────────────
echo "[5/6] Normalizing SSL vhost to the name apply.sh/check-ports.sh expect..."
if [ -f "$LE_SSL_AVAIL" ] && [ ! -f "$SSL_AVAIL" ]; then
    a2dissite "${CONF_NAME}-le-ssl.conf" >/dev/null 2>&1 || true
    mv "$LE_SSL_AVAIL" "$SSL_AVAIL"
    a2ensite "${CONF_NAME}-ssl.conf" >/dev/null
elif [ -f "$SSL_AVAIL" ]; then
    echo "      $SSL_AVAIL already exists, leaving it as-is."
else
    echo "      WARNING: could not find the SSL vhost certbot should have created"
    echo "      ($LE_SSL_AVAIL). Check 'certbot certificates' and 'apache2ctl -S'"
    echo "      and rename its vhost to $SSL_AVAIL by hand before running apply.sh."
fi

# The ProxyPass line in the SSL vhost must match the exact text apply.sh's
# sed searches for, so its /asterisk-ws insertion finds it on the first run.
if [ -f "$SSL_AVAIL" ] && ! grep -qE '^\s*ProxyPass\s+/ http://127\.0\.0\.1:'"${PORT}"'/ retry=0 timeout=120$' "$SSL_AVAIL"; then
    if grep -qE 'ProxyPass\s+/ http://127\.0\.0\.1:'"${PORT}"'/' "$SSL_AVAIL"; then
        sed -i -E "s|^[[:space:]]*ProxyPass[[:space:]]+/ http://127\.0\.0\.1:${PORT}/.*|    ProxyPass        / http://127.0.0.1:${PORT}/ retry=0 timeout=120|" "$SSL_AVAIL"
    else
        sed -i "/<\/VirtualHost>/i\\    ProxyPreserveHost On\\n    ProxyPass        / http://127.0.0.1:${PORT}/ retry=0 timeout=120\\n    ProxyPassReverse / http://127.0.0.1:${PORT}/" "$SSL_AVAIL"
    fi
fi

apache2ctl configtest
systemctl reload apache2

echo "[6/6] Done."
echo ""
echo "  Kiosk is now reachable at:  https://${HOSTNAME_ARG}/"
echo "  Certificate auto-renews via certbot's systemd timer (certbot.timer)."
echo ""
echo "  Next: sudo bash $(dirname "$0")/apply.sh   to wire up PJSIP + the WSS proxy for TX."
