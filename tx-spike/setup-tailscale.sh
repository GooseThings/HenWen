#!/bin/bash
# HenWen HTTPS setup via Tailscale — the alternative to setup-https.sh for
# operators with no usable inbound port at all: ISPs that block port
# forwarding outright, CGNAT connections, or anyone who'd rather not
# expose the Kiosk to the public internet in the first place. Tailscale
# builds a private mesh network between your own devices and issues a
# real, publicly-trusted TLS certificate for a *.ts.net hostname over its
# own control channel — no router configuration, no port forwarding, no
# port 80/443 reachability required at all.
#
# Trade-off: only devices joined to your tailnet (e.g. your phone/laptop
# with the Tailscale app installed) can reach the Kiosk or use TX. If you
# want the Kiosk reachable by arbitrary members of the public, use
# setup-https.sh instead.
#
# Usage: sudo bash setup-tailscale.sh [--port N]
#   --port N   Serve HTTPS on port N instead of 443 (rarely needed — since
#              nothing is forwarded through your router, this is just a
#              local Apache bind, not a port-forwarding workaround).
#
# Touches:  installs the tailscale CLI (official install script) if missing
#           installs apache2
#           /etc/apache2/ports.conf                       (Listen bound to
#                                                            the box's own
#                                                            Tailscale IP
#                                                            only, replacing
#                                                            the wildcard
#                                                            "Listen 443"
#                                                            mod_ssl adds —
#                                                            the two can't
#                                                            coexist on the
#                                                            same port, and
#                                                            nothing here
#                                                            should be
#                                                            reachable off
#                                                            the tailnet)
#           /etc/apache2/sites-available/henwen-ssl.conf  (new)
#           /etc/systemd/system/apache2.service.d/henwen-tailscale.conf (new —
#                                                            orders Apache
#                                                            after tailscaled
#                                                            so a reboot can't
#                                                            start it before
#                                                            the IP it's bound
#                                                            to exists)
#           /var/lib/tailscale/certs/                     (cert + key)
#           a systemd timer (henwen-tailscale-cert-renew) for renewal
#           /etc/asterisk/henwen-https-{port,mode,hostname} (marker files
#           read by apply.sh and check-ports.sh)
#
# The resulting file MUST be named exactly .../henwen-ssl.conf — that path
# is hardcoded in tx-spike/apply.sh and tx-spike/check-ports.sh.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
FLASK_PORT="${PORT:-5000}"
HTTPS_PORT=443
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --port)   HTTPS_PORT="${2:-}"; shift 2 ;;
        --port=*) HTTPS_PORT="${1#*=}"; shift ;;
        -h|--help)
            echo "Usage: sudo bash $0 [--port N]"
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
SSL_AVAIL="/etc/apache2/sites-available/${CONF_NAME}-ssl.conf"

echo ""
echo "============================================"
echo "  HenWen HTTPS Setup via Tailscale (for Browser TX)"
echo "============================================"
echo ""

# ── Install Tailscale if needed ─────────────────────────────
echo "[1/8] Checking for Tailscale..."
if ! command -v tailscale &>/dev/null; then
    echo "      Installing Tailscale (official install script)..."
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo "      Already installed: $(tailscale version | head -1)"
fi

# ── Ensure this box is joined to a tailnet ──────────────────
echo "[2/8] Checking tailnet connection..."
TS_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
if [ "$TS_STATE" != "Running" ]; then
    echo "      Not connected yet. Running 'tailscale up' — open the printed link to authenticate."
    tailscale up
fi
TS_JSON=$(tailscale status --json)
MAGIC_DNS_NAME=$(echo "$TS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
TS_IP=$(tailscale ip -4)
[ -n "$MAGIC_DNS_NAME" ] && [ -n "$TS_IP" ] || { echo "Could not determine this box's Tailscale hostname/IP."; exit 1; }
echo "      This box: $MAGIC_DNS_NAME  ($TS_IP)"

# ── Request the certificate ──────────────────────────────────
echo "[3/8] Requesting a Tailscale-issued certificate for $MAGIC_DNS_NAME..."
CERT_DIR=/var/lib/tailscale/certs
mkdir -p "$CERT_DIR"
chmod 750 "$CERT_DIR"
CERT_FILE="$CERT_DIR/${MAGIC_DNS_NAME}.crt"
KEY_FILE="$CERT_DIR/${MAGIC_DNS_NAME}.key"
if ! tailscale cert --cert-file "$CERT_FILE" --key-file "$KEY_FILE" "$MAGIC_DNS_NAME" 2>/tmp/henwen-ts-cert-err; then
    echo ""
    echo "  FAILED to get a certificate. Most common cause: 'HTTPS Certificates' isn't"
    echo "  enabled for your tailnet yet — a one-time toggle only available in the"
    echo "  admin console (no CLI/API for this):"
    echo "    https://login.tailscale.com/admin/dns  ->  enable 'HTTPS Certificates'"
    echo "  Then re-run this script. Error was:"
    cat /tmp/henwen-ts-cert-err
    rm -f /tmp/henwen-ts-cert-err
    exit 1
fi
rm -f /tmp/henwen-ts-cert-err

# ── Install Apache ────────────────────────────────────────────
echo "[4/8] Installing Apache..."
apt-get install -y apache2
a2enmod ssl proxy proxy_http proxy_wstunnel headers rewrite >/dev/null

# ── Bind Apache to the Tailscale IP only ─────────────────────
echo "[5/8] Binding Apache's HTTPS listener to the Tailscale interface only..."

# Disabling the wildcard "Listen 443" below is only safe if nothing else on
# this box depends on it -- plenty of ASL3 boxes also run other Apache-
# hosted tools (AllScan, Supermon, a personal site, ...) on their own
# *:443 vhost. Apache can't bind both a wildcard and an IP-specific Listen
# on the same port (the wildcard already claims it), so disabling it out
# from under another site would silently make that site unreachable on its
# normal address -- reachable only via the Tailscale IP, or not at all.
# Bug report: exactly this happened to a user whose AllScan install went
# unreachable after running this script.
OTHER_443=$(grep -lE '<VirtualHost[^>]*:443' /etc/apache2/sites-enabled/*.conf 2>/dev/null \
            | grep -vxF "/etc/apache2/sites-enabled/${CONF_NAME}-ssl.conf" || true)
if [ -n "$OTHER_443" ] && [ "$HTTPS_PORT" = "443" ]; then
    echo ""
    echo "  ERROR: another Apache site is already using port 443 on this box:"
    echo "$OTHER_443" | sed 's/^/    /'
    echo ""
    echo "  Disabling the wildcard 'Listen 443' (needed to bind HenWen's vhost to only"
    echo "  this box's Tailscale IP) would make the site(s) above unreachable on their"
    echo "  normal address. Re-run with a dedicated port for HenWen instead, e.g.:"
    echo "    sudo bash $0 --port 8443"
    exit 1
fi

# Only disable the wildcard when we actually need port 443 ourselves --
# with --port already pointed elsewhere, there's no conflict to avoid, and
# leaving "Listen 443" alone means whatever else uses it keeps working
# without operator intervention.
if [ "$HTTPS_PORT" = "443" ]; then
    sed -i -E 's/^([[:space:]]*)Listen 443[[:space:]]*$/\1# Listen 443  (disabled by setup-tailscale.sh -- see the IP-scoped Listen below; this vhost must not be reachable off the tailnet)/' /etc/apache2/ports.conf
fi
# Tagged so a re-run can find and remove *this exact* line before adding a
# fresh one -- if the tailnet ever reassigns this box's IP (device
# re-registration, key expiry+rejoin), leaving the old Listen line behind
# would make Apache refuse to start at all (every configured Listen address
# must bind successfully), not just skip the dead one.
sed -i -E "/# HenWen browser transmitter: Tailscale IP\$/d" /etc/apache2/ports.conf
echo "Listen ${TS_IP}:${HTTPS_PORT}  # HenWen browser transmitter: Tailscale IP" >> /etc/apache2/ports.conf

# Apache binding to a specific Tailscale IP only works once that interface
# exists -- without an explicit ordering dependency, a reboot can start
# Apache before tailscaled has brought tailscale0 up, which makes the
# Listen directive above fail to bind and leaves Apache in a permanently
# failed state until someone notices and restarts it by hand. This is the
# most common way a working TX setup goes silently dead after a reboot.
mkdir -p /etc/systemd/system/apache2.service.d
cat > /etc/systemd/system/apache2.service.d/henwen-tailscale.conf <<'EOF'
[Unit]
Wants=tailscaled.service
After=tailscaled.service
EOF
systemctl daemon-reload

# ── Write the SSL vhost ───────────────────────────────────────
echo "[6/8] Writing Apache vhost for $MAGIC_DNS_NAME..."
cat > "$SSL_AVAIL" <<VHOST
<VirtualHost ${TS_IP}:${HTTPS_PORT}>
    ServerName ${MAGIC_DNS_NAME}
    SSLEngine on
    SSLCertificateFile    ${CERT_FILE}
    SSLCertificateKeyFile ${KEY_FILE}
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:${FLASK_PORT}/ retry=0 timeout=120
    ProxyPassReverse / http://127.0.0.1:${FLASK_PORT}/
</VirtualHost>
VHOST
a2ensite "${CONF_NAME}-ssl.conf" >/dev/null
apache2ctl configtest
# `reload` requires an already-active service -- on a fresh apache2 install
# it may not be running yet (e.g. policy-rc.d blocking auto-start on some
# minimal/cloud images), which would otherwise fail this script right here
# with "apache2.service is not active, cannot reload" despite everything
# above having gone fine.
if systemctl is-active --quiet apache2; then
    systemctl reload apache2
else
    systemctl start apache2
fi

# ── Renewal timer (tailscale cert doesn't auto-renew) ────────
echo "[7/8] Installing daily renewal timer..."
cat > /etc/systemd/system/henwen-tailscale-cert-renew.service <<EOF
[Unit]
Description=Renew HenWen's Tailscale-issued TLS certificate

[Service]
Type=oneshot
ExecStart=/bin/bash ${SPIKE_DIR}/renew-tailscale-cert.sh
EOF
cat > /etc/systemd/system/henwen-tailscale-cert-renew.timer <<'EOF'
[Unit]
Description=Daily renewal check for HenWen's Tailscale-issued TLS certificate

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now henwen-tailscale-cert-renew.timer >/dev/null

# ── Record the chosen port/mode/hostname for apply.sh and check-ports.sh ──
echo "[8/8] Recording configuration..."
echo "$HTTPS_PORT"     > /etc/asterisk/henwen-https-port
echo "tailscale"       > /etc/asterisk/henwen-https-mode
echo "$MAGIC_DNS_NAME" > /etc/asterisk/henwen-https-hostname
chmod 644 /etc/asterisk/henwen-https-port /etc/asterisk/henwen-https-mode /etc/asterisk/henwen-https-hostname

echo ""
PORT_SUFFIX=""
[ "$HTTPS_PORT" != "443" ] && PORT_SUFFIX=":${HTTPS_PORT}"
echo "Done. Kiosk is reachable from any device on your tailnet at:"
echo "  https://${MAGIC_DNS_NAME}${PORT_SUFFIX}/"
echo ""
echo "No router/port-forwarding changes needed — only devices signed into"
echo "this tailnet can reach it."
echo ""
echo "Next: sudo bash ${SPIKE_DIR}/apply.sh   to wire up PJSIP + the WSS proxy for TX."
