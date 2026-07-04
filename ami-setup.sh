#!/bin/bash
# henwen-ami-setup
# Verifies and optionally fixes the Asterisk AMI configuration for HenWen.
# Run as root: sudo bash ami-setup.sh
#
# This script:
#   1. Shows the current manager.conf
#   2. Tests the AMI connection directly using Python (no app needed)
#   3. Optionally creates/updates the AMI user entry
#   4. Updates the HenWen service file to match
#   5. Reloads Asterisk manager module

set -e

MANAGER_CONF="/etc/asterisk/manager.conf"
SERVICE_FILE="/etc/systemd/system/HenWen.service"
AMI_PORT=5038
AMI_HOST="127.0.0.1"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

echo ""
echo "============================================"
echo "  HenWen AMI Setup and Verification"
echo "============================================"
echo ""

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}ERROR: Please run as root: sudo bash ami-setup.sh${NC}"
    exit 1
fi

# ── Check Asterisk is running ─────────────────────────────────────────────────
echo -e "${CYAN}[1/5] Checking Asterisk status...${NC}"
if systemctl is-active --quiet asterisk; then
    echo -e "      ${GREEN}Asterisk is running.${NC}"
else
    echo -e "      ${RED}Asterisk is NOT running!${NC}"
    echo "      Start it first: sudo systemctl start asterisk"
    exit 1
fi

# ── Show current manager.conf ─────────────────────────────────────────────────
echo ""
echo -e "${CYAN}[2/5] Current manager.conf contents:${NC}"
if [ -f "$MANAGER_CONF" ]; then
    echo "---"
    cat "$MANAGER_CONF"
    echo "---"
else
    echo -e "      ${RED}$MANAGER_CONF not found!${NC}"
    exit 1
fi

# ── Extract existing AMI user/secret from manager.conf ───────────────────────
echo ""
echo -e "${CYAN}[3/5] Parsing AMI credentials from manager.conf...${NC}"

# Use Python to parse it reliably
AMI_INFO=$(python3 - <<'PYEOF'
import re, sys

conf = open('/etc/asterisk/manager.conf').read()

# Check enabled
m = re.search(r'^\s*enabled\s*=\s*(\S+)', conf, re.MULTILINE)
enabled = m.group(1).lower() if m else 'yes'
if enabled in ('no','false','0'):
    print("ENABLED=no")
    sys.exit(0)
print("ENABLED=yes")

# Extract port
m = re.search(r'^\s*port\s*=\s*(\d+)', conf, re.MULTILINE)
print("PORT=" + (m.group(1) if m else "5038"))

# Find first non-general user with a secret
current_header = None
current_secret = None

for line in conf.splitlines():
    line = line.strip()
    if not line or line.startswith(';'):
        continue
    hdr = re.match(r'^\[([^\]]+)\]', line)
    if hdr:
        if current_header and current_header.lower() != 'general' and current_secret:
            print("USER=" + current_header)
            print("SECRET=" + current_secret)
            import sys; sys.exit(0)
        current_header = hdr.group(1).strip()
        current_secret = None
        continue
    if '=' in line and not line.startswith(';'):
        k = line.split('=',1)[0].strip().split(';')[0].strip().lower()
        v = line.split('=',1)[1].split(';')[0].strip()
        if k == 'secret':
            current_secret = v

if current_header and current_header.lower() != 'general' and current_secret:
    print("USER=" + current_header)
    print("SECRET=" + current_secret)
PYEOF
)

eval "$AMI_INFO" 2>/dev/null || true

if [ "${ENABLED:-yes}" = "no" ]; then
    echo -e "  ${RED}ERROR: AMI is disabled in manager.conf (enabled = no)${NC}"
    echo "  Fix: set 'enabled = yes' in the [general] section"
    exit 1
fi

echo "  AMI port:  ${PORT:-5038}"
echo "  AMI user:  ${USER:-(none found)}"
echo "  AMI secret: ${SECRET:+(set)}"

if [ -z "$USER" ] || [ -z "$SECRET" ]; then
    echo ""
    echo -e "  ${YELLOW}No valid AMI user found in manager.conf.${NC}"
    echo "  A user stanza with 'secret = ...' is required."
    echo ""
    read -p "  Create an AMI user now? [Y/n]: " CREATE_USER
    CREATE_USER="${CREATE_USER:-Y}"
    if [[ "$CREATE_USER" =~ ^[Yy] ]]; then
        read -p "  AMI username [admin]: " NEW_USER
        NEW_USER="${NEW_USER:-admin}"
        # Generate a random secret
        NEW_SECRET=$(python3 -c "import secrets,string; print(''.join(secrets.choices(string.ascii_letters+string.digits,k=16)))")
        echo ""
        echo "  Adding [$NEW_USER] with secret=$NEW_SECRET to manager.conf..."
        cat >> "$MANAGER_CONF" << AMIEOF

[$NEW_USER]
secret = $NEW_SECRET
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
permit = 127.0.0.1/255.255.255.0
AMIEOF
        USER="$NEW_USER"
        SECRET="$NEW_SECRET"
        echo -e "  ${GREEN}Added AMI user '$USER' to manager.conf.${NC}"
    else
        echo "  Aborting. Add a user manually and re-run."
        exit 1
    fi
fi

# ── Test AMI connection directly ──────────────────────────────────────────────
echo ""
echo -e "${CYAN}[4/5] Testing AMI login for user '$USER'...${NC}"

TEST_RESULT=$(python3 - "$USER" "$SECRET" "${PORT:-5038}" <<'PYEOF'
import socket, time, sys

user   = sys.argv[1]
secret = sys.argv[2]
port   = int(sys.argv[3])

try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(6)
    s.connect(('127.0.0.1', port))

    # Read banner
    buf = b''
    deadline = time.time() + 4
    while time.time() < deadline:
        s.settimeout(0.5)
        try:
            c = s.recv(256)
            if c: buf += c
            if b'\r\n' in buf: break
        except socket.timeout:
            continue
    banner = buf.decode('utf-8','replace').strip()
    print("BANNER=" + banner)

    # Send login
    s.settimeout(6)
    login = (f"Action: Login\r\nUsername: {user}\r\nSecret: {secret}\r\nEvents: off\r\n\r\n")
    s.sendall(login.encode('utf-8'))

    # Read response
    resp = b''
    deadline = time.time() + 6
    s.settimeout(0.5)
    while time.time() < deadline:
        try:
            c = s.recv(1024)
            if c: resp += c
            if b'\r\n\r\n' in resp: break
        except socket.timeout:
            continue
    s.close()

    resp_str = resp.decode('utf-8','replace')
    if 'Response: Success' in resp_str:
        print("RESULT=success")
    elif 'Authentication failed' in resp_str or 'Response: Error' in resp_str:
        print("RESULT=authfail")
        # Extract message
        for line in resp_str.splitlines():
            if line.startswith('Message:'):
                print("MESSAGE=" + line.split(':',1)[1].strip())
                break
    else:
        print("RESULT=unknown")
        print("RAW=" + repr(resp_str[:200]))
except Exception as e:
    print("RESULT=error")
    print("ERROR=" + str(e))
PYEOF
)

eval "$TEST_RESULT" 2>/dev/null || true

echo "  Banner:  ${BANNER:-(none)}"

case "${RESULT}" in
    success)
        echo -e "  ${GREEN}AMI login SUCCESSFUL for user '$USER'${NC}"
        ;;
    authfail)
        echo -e "  ${RED}AMI login FAILED: Authentication rejected${NC}"
        echo "  Message: ${MESSAGE:-unknown}"
        echo ""
        echo "  This means the secret in manager.conf does not match what"
        echo "  Asterisk has loaded. Asterisk may not have reloaded manager.conf"
        echo "  since it was last edited."
        echo ""
        echo "  Reloading Asterisk manager module now..."
        asterisk -rx "module reload manager" && echo -e "  ${GREEN}Manager reloaded.${NC}" || true
        sleep 1
        echo "  Re-testing..."
        # Quick re-test
        RETEST=$(python3 -c "
import socket,time
s=socket.socket(); s.settimeout(5); s.connect(('127.0.0.1',${PORT:-5038}))
buf=b''
dl=time.time()+3
while time.time()<dl:
    s.settimeout(0.3)
    try:
        c=s.recv(256)
        if c: buf+=c
        if b'\r\n' in buf: break
    except: continue
s.settimeout(5)
s.sendall(b'Action: Login\r\nUsername: ${USER}\r\nSecret: ${SECRET}\r\nEvents: off\r\n\r\n')
resp=b''
dl=time.time()+5
s.settimeout(0.3)
while time.time()<dl:
    try:
        c=s.recv(1024)
        if c: resp+=c
        if b'\r\n\r\n' in resp: break
    except: continue
s.close()
print('ok' if b'Success' in resp else 'fail')
" 2>/dev/null || echo "error")
        if [ "$RETEST" = "ok" ]; then
            echo -e "  ${GREEN}Re-test PASSED after reload.${NC}"
            RESULT=success
        else
            echo -e "  ${RED}Still failing after reload.${NC}"
            echo "  Check: sudo asterisk -rx 'manager show users'"
        fi
        ;;
    error)
        echo -e "  ${RED}Socket error: ${ERROR}${NC}"
        echo "  Is Asterisk running and manager.conf enabled=yes?"
        exit 1
        ;;
    *)
        echo -e "  ${YELLOW}Unexpected response: ${RAW}${NC}"
        ;;
esac

# ── Update service file ───────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}[5/5] Updating HenWen service file...${NC}"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "  Service file not found at $SERVICE_FILE"
    echo "  Run install.sh first, then re-run this script."
else
    # Update AMI_USER and AMI_SECRET in the service file
    sed -i "s|^Environment=\"AMI_USER=.*\"|Environment=\"AMI_USER=${USER}\"|" "$SERVICE_FILE"
    sed -i "s|^Environment=\"AMI_SECRET=.*\"|Environment=\"AMI_SECRET=${SECRET}\"|" "$SERVICE_FILE"

    # Verify the lines exist; add them if not
    if ! grep -q "^Environment=\"AMI_USER=" "$SERVICE_FILE"; then
        sed -i "/\[Service\]/a Environment=\"AMI_USER=${USER}\"" "$SERVICE_FILE"
    fi
    if ! grep -q "^Environment=\"AMI_SECRET=" "$SERVICE_FILE"; then
        sed -i "/AMI_USER/a Environment=\"AMI_SECRET=${SECRET}\"" "$SERVICE_FILE"
    fi

    systemctl daemon-reload

    if systemctl is-active --quiet HenWen 2>/dev/null; then
        systemctl restart HenWen
        sleep 1
        if systemctl is-active --quiet HenWen; then
            echo -e "  ${GREEN}HenWen service updated and restarted successfully.${NC}"
        else
            echo -e "  ${YELLOW}Service restart may have failed. Check: journalctl -u HenWen -n 20${NC}"
        fi
    else
        echo "  HenWen service not currently running. Start with: sudo systemctl start HenWen"
    fi
fi

# ── Also reload manager module to be sure ────────────────────────────────────
echo ""
echo "  Reloading Asterisk manager module..."
asterisk -rx "module reload manager" 2>/dev/null && echo -e "  ${GREEN}Manager reloaded.${NC}" || echo "  (reload skipped - may not be needed)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
if [ "${RESULT}" = "success" ]; then
    echo -e "  ${GREEN}AMI setup complete and verified!${NC}"
else
    echo -e "  ${YELLOW}AMI setup complete. Verify in the web UI.${NC}"
fi
echo ""
echo "  AMI User:   $USER"
echo "  AMI Secret: $SECRET"
echo "  AMI Port:   ${PORT:-5038}"
echo ""
echo "  To verify in the web UI:"
echo "    http://$(hostname -I | awk '{print $1}'):5000"
echo "    -> AMI Diagnostics -> Run Test"
echo ""
echo "  To check logs:"
echo "    journalctl -u HenWen -f"
echo "============================================"
