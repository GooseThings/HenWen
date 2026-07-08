#!/bin/bash
# HenWen HTTPS setup — provisions Apache + a Let's Encrypt certificate.
#
# This is what makes the browser a "secure context", which the TX button
# requires (getUserMedia/WebRTC are blocked on plain http:// except on
# localhost) and gives Asterisk's SIP-over-WebSocket signaling somewhere
# to terminate TLS. Optional: the kiosk and every other HenWen feature
# work fine over plain HTTP — only run this if you want browser TX and
# have a public hostname pointed at this box. If you're behind CGNAT or
# can't forward a port at all, a third-party reverse-proxy/VPN service
# that can front HTTPS for you (e.g. Tailscale Serve/Funnel, Cloudflare
# Tunnel) is an option — HenWen doesn't script or manage that setup
# itself, but any of them work as long as they proxy plain HTTP to this
# box's Flask port and a WebSocket-capable proxy to Asterisk's :8088 for
# /asterisk-ws (see the README's Browser TX section).
#
# Usage: sudo bash setup-https.sh [--port N] [--dns-manual] [hostname] [email]
#
#   --port N       Serve HTTPS on port N instead of 443. Useful when your
#                   ISP won't forward 443 (some block all ports below 1000
#                   entirely) but will forward a high port. The TX button
#                   adapts automatically — it reads the port from the
#                   page's own URL, no other config needed once Apache is
#                   listening here and the port is forwarded.
#   --dns-manual   Use a manual DNS-01 challenge instead of the default
#                   HTTP-01. Needed if your ISP blocks port 80 too: Let's
#                   Encrypt's HTTP-01 validator ALWAYS connects to port 80
#                   externally, no matter what --port you choose for
#                   serving — if 80 itself is unreachable this is the only
#                   way to get a cert. You'll be prompted to create one DNS
#                   TXT record (works with any DNS provider, no API
#                   needed) and confirm once it's live. Trade-off: this
#                   does NOT auto-renew — re-run this script with
#                   --dns-manual again every ~60-90 days.
#
# Requires: a DNS name that already resolves to this box's public IP.
# Without --dns-manual, also requires TCP 80 forwarded (challenge) and
# your chosen --port (default 443) forwarded (serving).
#
# Touches:  installs apache2, certbot, python3-certbot-apache
#           /etc/apache2/sites-available/henwen.conf       (new, port 80)
#           /etc/apache2/sites-available/henwen-ssl.conf   (new, HTTPS port)
#           /etc/apache2/ports.conf                        (adds Listen N if --port used)
#           enables mod_ssl, mod_proxy, mod_proxy_http, mod_proxy_wstunnel,
#           mod_headers, mod_rewrite
#           /etc/asterisk/henwen-https-{port,mode,hostname} (marker files
#           read by apply.sh and check-ports.sh)
#
# The resulting file MUST be named exactly .../henwen-ssl.conf — that path
# is hardcoded in tx-spike/apply.sh (which patches its ProxyPass line to
# add the /asterisk-ws WSS proxy) and tx-spike/check-ports.sh. Certbot's
# apache plugin normally names its generated SSL vhost "<name>-le-ssl.conf"
# — this script renames it after the fact so both scripts keep working
# unmodified.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

FLASK_PORT="${PORT:-5000}"   # HenWen's own port — distinct from --port (the HTTPS port) below
HTTPS_PORT=443
DNS_MANUAL=0
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --port)       HTTPS_PORT="${2:-}"; shift 2 ;;
        --port=*)     HTTPS_PORT="${1#*=}"; shift ;;
        --dns-manual) DNS_MANUAL=1; shift ;;
        -h|--help)
            echo "Usage: sudo bash $0 [--port N] [--dns-manual] [hostname] [email]"
            exit 0 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

[[ "$HTTPS_PORT" =~ ^[0-9]+$ ]] && [ "$HTTPS_PORT" -ge 1 ] && [ "$HTTPS_PORT" -le 65535 ] || {
    echo "Invalid --port: $HTTPS_PORT"; exit 1; }
if [ "$HTTPS_PORT" = "$FLASK_PORT" ]; then
    echo "--port $HTTPS_PORT collides with HenWen's own Flask port ($FLASK_PORT) — pick a different one."
    exit 1
fi

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
echo "[1/7] Checking that $HOSTNAME_ARG resolves to this box..."
PUBLIC_IP=$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || echo "")
RESOLVED_IP=$(getent ahostsv4 "$HOSTNAME_ARG" 2>/dev/null | awk '{print $1; exit}')
if [ -n "$PUBLIC_IP" ] && [ -n "$RESOLVED_IP" ] && [ "$PUBLIC_IP" != "$RESOLVED_IP" ]; then
    echo "      WARNING: $HOSTNAME_ARG resolves to $RESOLVED_IP but this box's public IP looks"
    echo "      like $PUBLIC_IP. Browsers won't reach this box at that hostname unless DNS and"
    echo "      port forwarding both actually point here."
    read -p "      Continue anyway? [y/N]: " CONTINUE_ANYWAY
    [[ "$CONTINUE_ANYWAY" =~ ^[Yy] ]] || exit 1
elif [ -z "$PUBLIC_IP" ] || [ -z "$RESOLVED_IP" ]; then
    echo "      Could not verify (no outbound internet or DNS not resolving yet) — continuing."
else
    echo "      OK: $HOSTNAME_ARG -> $RESOLVED_IP matches this box's public IP."
fi

# ── Install Apache + certbot ───────────────────────────────
echo "[2/7] Installing Apache and certbot..."
apt-get install -y apache2 certbot python3-certbot-apache
a2enmod ssl proxy proxy_http proxy_wstunnel headers rewrite >/dev/null

if [ "$HTTPS_PORT" != "443" ]; then
    echo "      Adding 'Listen $HTTPS_PORT' (custom HTTPS port)..."
    grep -qxF "Listen ${HTTPS_PORT}" /etc/apache2/ports.conf 2>/dev/null || \
        echo "Listen ${HTTPS_PORT}" >> /etc/apache2/ports.conf
fi

# ── Base HTTP vhost (port 80) ──────────────────────────────
echo "[3/7] Writing Apache vhost for $HOSTNAME_ARG..."
cat > "$HTTP_AVAIL" <<VHOST
<VirtualHost *:80>
    ServerName ${HOSTNAME_ARG}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${FLASK_PORT}/ retry=0 timeout=120
    ProxyPassReverse / http://127.0.0.1:${FLASK_PORT}/
</VirtualHost>
VHOST
a2ensite "${CONF_NAME}.conf" >/dev/null
apache2ctl configtest
systemctl reload apache2

# ── Certbot ─────────────────────────────────────────────────
echo "[4/7] Requesting a Let's Encrypt certificate for $HOSTNAME_ARG..."
if [ "$DNS_MANUAL" = "1" ]; then
    echo "      Manual DNS-01 challenge selected — no port 80 needed for validation."
    echo "      You'll be prompted to create a TXT record; certbot waits for you to"
    echo "      confirm before it checks. This does NOT auto-renew — re-run this"
    echo "      script with --dns-manual again every ~60-90 days."
    certbot --installer apache --authenticator manual --preferred-challenges dns \
        --no-eff-email -d "$HOSTNAME_ARG" -m "$EMAIL_ARG" --agree-tos --redirect
else
    certbot --apache --no-eff-email -d "$HOSTNAME_ARG" -m "$EMAIL_ARG" --agree-tos --redirect --non-interactive
fi

# ── Normalize the SSL vhost filename + port ────────────────
echo "[5/7] Normalizing SSL vhost to the name apply.sh/check-ports.sh expect..."
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

if [ -f "$SSL_AVAIL" ] && [ "$HTTPS_PORT" != "443" ]; then
    sed -i "s|<VirtualHost \*:443>|<VirtualHost *:${HTTPS_PORT}>|" "$SSL_AVAIL"
fi

# certbot's --redirect enhancement sends plain-HTTP visitors to
# "https://%{SERVER_NAME}%{REQUEST_URI}" — no port, so it implicitly means
# 443. On a custom port that redirect would be wrong (send them somewhere
# nothing is listening), so pin it to the actual serving port.
if [ "$HTTPS_PORT" != "443" ] && [ -f "$HTTP_AVAIL" ]; then
    if grep -q 'https://%{SERVER_NAME}%{REQUEST_URI}' "$HTTP_AVAIL"; then
        sed -i "s|https://%{SERVER_NAME}%{REQUEST_URI}|https://%{SERVER_NAME}:${HTTPS_PORT}%{REQUEST_URI}|" "$HTTP_AVAIL"
    else
        echo "      NOTE: couldn't find certbot's redirect rule to re-target at :${HTTPS_PORT} —"
        echo "      check $HTTP_AVAIL by hand if plain-http visitors should be redirected."
    fi
fi

# The ProxyPass line in the SSL vhost must match the exact text apply.sh's
# sed searches for, so its /asterisk-ws insertion finds it on the first run.
if [ -f "$SSL_AVAIL" ] && ! grep -qE '^\s*ProxyPass\s+/ http://127\.0\.0\.1:'"${FLASK_PORT}"'/ retry=0 timeout=120$' "$SSL_AVAIL"; then
    if grep -qE 'ProxyPass\s+/ http://127\.0\.0\.1:'"${FLASK_PORT}"'/' "$SSL_AVAIL"; then
        sed -i -E "s|^[[:space:]]*ProxyPass[[:space:]]+/ http://127\.0\.0\.1:${FLASK_PORT}/.*|    ProxyPass        / http://127.0.0.1:${FLASK_PORT}/ retry=0 timeout=120|" "$SSL_AVAIL"
    else
        sed -i "/<\/VirtualHost>/i\\    ProxyPreserveHost On\\n    ProxyPass        / http://127.0.0.1:${FLASK_PORT}/ retry=0 timeout=120\\n    ProxyPassReverse / http://127.0.0.1:${FLASK_PORT}/" "$SSL_AVAIL"
    fi
fi

apache2ctl configtest
systemctl reload apache2

# ── Record the chosen port/mode/hostname for apply.sh and check-ports.sh ──
echo "[6/7] Recording configuration..."
echo "$HTTPS_PORT"   > /etc/asterisk/henwen-https-port
echo "public"        > /etc/asterisk/henwen-https-mode
echo "$HOSTNAME_ARG" > /etc/asterisk/henwen-https-hostname
chmod 644 /etc/asterisk/henwen-https-port /etc/asterisk/henwen-https-mode /etc/asterisk/henwen-https-hostname

echo "[7/7] Done."
echo ""
PORT_SUFFIX=""
[ "$HTTPS_PORT" != "443" ] && PORT_SUFFIX=":${HTTPS_PORT}"
echo "  Kiosk is now reachable at:  https://${HOSTNAME_ARG}${PORT_SUFFIX}/"
if [ "$DNS_MANUAL" = "1" ]; then
    echo "  Certificate does NOT auto-renew (manual DNS-01) — re-run with --dns-manual every ~60-90 days."
else
    echo "  Certificate auto-renews via certbot's systemd timer (certbot.timer)."
fi
if [ "$HTTPS_PORT" != "443" ]; then
    echo "  Router forward needed: TCP ${HTTPS_PORT} -> this box's port ${HTTPS_PORT} (NOT 443)."
fi
echo ""
echo "  Next: sudo bash $(dirname "$0")/apply.sh   to wire up PJSIP + the WSS proxy for TX."
