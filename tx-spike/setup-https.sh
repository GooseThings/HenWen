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
echo "[2/7] Checking for Apache and certbot..."
# Ask before touching the system, mirroring install.sh's own dependency
# prompt (see its NEED_PKGS block). Answering "yes" to *set up HTTPS* is
# not the same as consenting to apt-get pulling in three packages, and
# this script shouldn't treat it as such -- reported as issue #75.
#
# Only the genuinely-missing packages are named and installed, so a re-run
# on a box that already has them prompts for nothing and changes nothing.
# dpkg-query's Status field is the check rather than `dpkg -s`, which also
# succeeds for a removed-but-not-purged package still holding config files.
HTTPS_PKGS=(apache2 certbot python3-certbot-apache)
NEED_HTTPS_PKGS=()
for pkg in "${HTTPS_PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q '^install ok installed$' \
        || NEED_HTTPS_PKGS+=("$pkg")
done

if [ ${#NEED_HTTPS_PKGS[@]} -gt 0 ]; then
    echo "      HTTPS setup needs these package(s): ${NEED_HTTPS_PKGS[*]}"
    DO_HTTPS_INSTALL=1
    if [ -t 0 ]; then
        read -p "      Install via apt-get now? [Y/n]: " REPLY
        [[ "$REPLY" =~ ^[Nn] ]] && DO_HTTPS_INSTALL=0
    fi
    if [ "$DO_HTTPS_INSTALL" -ne 1 ]; then
        echo "      Skipped — HTTPS is not set up, and the browser TX button will"
        echo "      stay hidden until it is. Everything else works over plain HTTP."
        echo "      To do it yourself later:"
        echo "        sudo apt-get update && sudo apt-get install -y ${NEED_HTTPS_PKGS[*]}"
        echo "        sudo bash $0 $HOSTNAME_ARG $EMAIL_ARG"
        exit 1
    fi
    # apt-get update first -- a stale package list here 404s the same way it did
    # for install.sh's python3-venv install (see install.sh's own comment on
    # this), and this step is unattended (--non-interactive certbot below), so
    # there's no later error message pointing back at a fix.
    echo "      Running apt-get update..."
    apt-get update
    echo "      Installing: ${NEED_HTTPS_PKGS[*]}"
    apt-get install -y "${NEED_HTTPS_PKGS[@]}"
else
    echo "      Already installed: ${HTTPS_PKGS[*]}"
fi
a2enmod ssl proxy proxy_http proxy_wstunnel headers rewrite >/dev/null

if [ "$HTTPS_PORT" != "443" ]; then
    echo "      Adding 'Listen $HTTPS_PORT' (custom HTTPS port)..."
    grep -qxF "Listen ${HTTPS_PORT}" /etc/apache2/ports.conf 2>/dev/null || \
        echo "Listen ${HTTPS_PORT}" >> /etc/apache2/ports.conf
fi

# ── Preserve whatever else this box already serves ─────────
# A catch-all `ProxyPass / -> Flask` on a *named* vhost hijacks every URL on
# that hostname, including apps that were working long before HenWen was
# installed. Allmon3 and AllScan live under the default DocumentRoot on a
# great many AllStar nodes, and issue #77 reported exactly that: /allmon3/
# and /allscan/ began returning 404 (Flask has no such route) the moment
# this vhost claimed the hostname, while the same paths still worked by raw
# IP -- because an IP doesn't match ServerName, so it fell through to the
# default vhost, which was still serving them perfectly well.
#
# So before claiming the hostname, look at what the default site already
# serves and exclude each of those paths with `ProxyPass /<dir> !`. Apache
# honours an exclusion only when it appears *before* the catch-all, hence
# the insertion point below. Excluded paths are then served straight from
# DocumentRoot exactly as they were.
HENWEN_RESERVED=(accept-invite accessible api asl3-ez-manager forgot-password
                 henwen-manager login logout reset-password status static)
EXCL_MARKER="# HenWen: preserve paths this box already served (issue #77)"

DOCROOT=$(awk '/^[[:space:]]*DocumentRoot[[:space:]]+/ {print $2; exit}' \
          /etc/apache2/sites-enabled/*.conf 2>/dev/null)
[ -n "$DOCROOT" ] || DOCROOT=/var/www/html
DOCROOT="${DOCROOT%/}"

PRESERVED=()
SHADOWED=()
if [ -d "$DOCROOT" ]; then
    for _d in "$DOCROOT"/*/; do
        [ -d "$_d" ] || continue
        _name=$(basename "$_d")
        # Anything needing quoting would produce a malformed directive; a
        # directory named like that isn't a served app worth guessing about.
        case "$_name" in *[!A-Za-z0-9._-]*) continue ;; esac
        # Never shadow HenWen's own routes -- the operator explicitly pointed
        # this hostname at HenWen, so on a collision HenWen has to win. Say so
        # rather than silently picking a side.
        if printf '%s\n' "${HENWEN_RESERVED[@]}" | grep -qxF "$_name"; then
            SHADOWED+=("$_name")
            continue
        fi
        PRESERVED+=("$_name")
    done
fi

# Insert the DocumentRoot + exclusions immediately above the catch-all
# ProxyPass in a vhost file. Idempotent (marker check), and a no-op when
# there's nothing to preserve, so re-running the script never stacks
# duplicates.
inject_exclusions() {
    local conf="$1" line tmp injected=0 n
    [ -f "$conf" ] || return 0
    [ "${#PRESERVED[@]}" -gt 0 ] || return 0
    grep -qF "$EXCL_MARKER" "$conf" && return 0
    tmp=$(mktemp)
    while IFS= read -r line; do
        if [ "$injected" -eq 0 ] && \
           [[ "$line" =~ ^[[:space:]]*ProxyPass[[:space:]]+/[[:space:]]+http://127\.0\.0\.1: ]]; then
            printf '    DocumentRoot %s\n' "$DOCROOT"
            printf '    %s\n' "$EXCL_MARKER"
            for n in "${PRESERVED[@]}"; do printf '    ProxyPass /%s !\n' "$n"; done
            injected=1
        fi
        printf '%s\n' "$line"
    done < "$conf" > "$tmp"
    mv "$tmp" "$conf"
}

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
inject_exclusions "$HTTP_AVAIL"

if [ "${#PRESERVED[@]}" -gt 0 ]; then
    echo "      Preserving paths already served from ${DOCROOT}: ${PRESERVED[*]}"
    echo "      (these keep working on $HOSTNAME_ARG instead of being proxied to HenWen)"
fi
if [ "${#SHADOWED[@]}" -gt 0 ]; then
    echo "      WARNING: ${DOCROOT} also contains: ${SHADOWED[*]}"
    echo "      Those names collide with HenWen's own URLs, so HenWen wins on"
    echo "      $HOSTNAME_ARG and they stay reachable only by IP. Rename them if"
    echo "      you need both on this hostname."
fi
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

# Same preservation for the SSL vhost. certbot builds it by copying the :80
# vhost, so the exclusions are usually carried over already -- inject_exclusions
# is a no-op then (marker check). This covers the case where certbot rewrites
# or reorders enough that they don't survive the copy, and costs nothing when
# they did.
inject_exclusions "$SSL_AVAIL"

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
