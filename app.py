#!/usr/bin/env python3
"""
ASL3-EZ - AllStarLink 3 rpt.conf Editor + Node Control
by N8GMZ

FIXES in this version:
  - AMI: proper TCP socket connect with full banner drain before login
  - AMI: correct \r\n\r\n packet terminator (was missing in original)
  - AMI: login response properly validated; detailed error messages logged
  - AMI: response reader handles multi-packet event floods after login
  - rpt.conf save: writes via temp file + atomic rename so partial writes can't corrupt
  - rpt.conf save: explicit flush+fsync before rename
  - rpt.conf save: ownership restored to asterisk:asterisk (mode 640) after atomic rename
    so ASL3 can read the file (Asterisk runs as asterisk user, not root)
  - Asterisk restart: uses full path /bin/systemctl to avoid PATH issues under gunicorn
  - Asterisk reload: uses /usr/sbin/asterisk full path
  - Node control: corrected ilink command syntax for ASL3 / app_rpt
  - Removed duplicate service name (was referencing both asl3-rpt-editor and ASL3-EZ)
  - No emojis anywhere in output or logs
  - Verbose logging throughout for dashboard debug display
  - AMI: persistent connection pool + background poller replaces per-request
    connect/login/logoff cycle; status reads from cache (microseconds vs ~200ms)
  - AMI: use 'rpt show nodes' instead of 'rpt show variables' for reliable keyed
    state detection in ASL3 (Rx=1 field); previous RPT_RXKEYED approach was unreliable
  - AMI: CACHE_TTL raised to 30s and POLL_INTERVAL to 3s to eliminate false
    stale warnings and match Allmon's update rate
"""

import os
import re
import subprocess
import shutil
import socket
import time
import json
import sqlite3
import threading
import tempfile
import sys
import pwd
import grp
import secrets
from datetime import datetime
from flask import Flask, render_template, request, jsonify

try:
    import urllib.request as urlreq
except ImportError:
    import urllib2 as urlreq

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration  (all overridable via environment variables in service file)
# ---------------------------------------------------------------------------
RPT_CONF_PATH   = os.environ.get("RPT_CONF_PATH",   "/etc/asterisk/rpt.conf")
MANAGER_CONF    = os.environ.get("MANAGER_CONF",    "/etc/asterisk/manager.conf")
BACKUP_DIR      = os.environ.get("BACKUP_DIR",      "/etc/asterisk/rpt_backups")
SECRET_KEY      = os.environ.get("SECRET_KEY",      "asl3-ez-change-me")
PORT            = int(os.environ.get("PORT",         5000))
HOST            = os.environ.get("HOST",             "0.0.0.0")
DB_PATH         = os.environ.get("DB_PATH",          "/etc/asterisk/asl3ez.db")
AMI_HOST        = os.environ.get("AMI_HOST",         "127.0.0.1")
AMI_PORT        = int(os.environ.get("AMI_PORT",     5038))
SERVICE_NAME    = os.environ.get("SERVICE_NAME",     "ASL3-EZ")
SERVICE_FILE_PATH = os.environ.get("SERVICE_FILE_PATH",
                                    f"/etc/systemd/system/{SERVICE_NAME}.service")

# SECRET_KEY values that ship with the app/installer — used to warn the user
# in the dashboard that they're still on the default and should change it.
DEFAULT_SECRET_KEYS = {"", "asl3-ez-change-me", "asl3-ez-change-me-in-production"}

# Persistent AMI poller settings (tunable via service file env vars)
# 3s poll matches Allmon's update rate; 30s TTL avoids false "stale" warnings
POLL_INTERVAL   = float(os.environ.get("AMI_POLL_INTERVAL", "3.0"))   # seconds between polls
CACHE_TTL       = float(os.environ.get("AMI_CACHE_TTL",     "30.0"))  # seconds before cache is stale

# Full paths — do NOT rely on PATH env under gunicorn/systemd
SYSTEMCTL_PATH  = "/bin/systemctl"
if not os.path.exists(SYSTEMCTL_PATH):
    SYSTEMCTL_PATH = "/usr/bin/systemctl"
ASTERISK_PATH   = "/usr/sbin/asterisk"

# ASL3 astdb.txt is written by asl3-update-astdb (from asl3-update-nodelist package)
ASTDB_PATHS = [
    "/var/lib/asterisk/astdb.txt",
    "/var/log/asterisk/astdb.txt",
    "/tmp/astdb.txt",
]
ASL_STATS_URL  = "https://stats.allstarlink.org/api/stats/{}"
# AllStarLink node description database (callsign, location, description)
# Format: node,callsign,description,location  (pipe-separated fields)
ALLMONDB_URL   = "https://allmondb.allstarlink.org/allmondb.php"
_allmondb_cache = {}
_allmondb_loaded = False
_allmondb_lock   = threading.Lock()

app.secret_key = SECRET_KEY

# ---------------------------------------------------------------------------
# Logging  (verbose, timestamp-prefixed, written to stdout for journald)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()

def log(level, msg):
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = f"[{ts}] [{level}] {msg}"
    with _log_lock:
        print(out, flush=True)

log("INFO", "ASL3-EZ starting up")
log("INFO", f"  RPT_CONF_PATH  = {RPT_CONF_PATH}")
log("INFO", f"  MANAGER_CONF   = {MANAGER_CONF}")
log("INFO", f"  BACKUP_DIR     = {BACKUP_DIR}")
log("INFO", f"  AMI_HOST:PORT  = {AMI_HOST}:{AMI_PORT}")
log("INFO", f"  DB_PATH        = {DB_PATH}")
log("INFO", f"  Running as UID = {os.getuid()} ({'root' if os.getuid()==0 else 'non-root'})")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS favorites (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        node  TEXT    UNIQUE NOT NULL,
        label TEXT    DEFAULT '',
        added TEXT    DEFAULT (datetime('now'))
    )""")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# AMI credential resolution
# ---------------------------------------------------------------------------
def parse_manager_conf():
    """
    Return dict: {user, secret, host, port}.

    Priority:
      1. AMI_USER + AMI_SECRET env vars  (set in the service file — PREFERRED)
      2. Parse manager.conf directly

    Parser is intentionally permissive:
      - A user stanza only needs 'secret'.
      - 'enabled' defaults to True if absent (ASL3 default manager.conf omits it).
      - 'write' line is NOT required — we accept any user that has a secret.
        The write= check was the root cause of auth failures on stock ASL3
        installs where manager.conf has no explicit write= line.
      - Commented-out lines (;) are skipped.
      - Inline comments after values are stripped.
    """
    result = {"user": None, "secret": None, "host": AMI_HOST, "port": AMI_PORT}

    env_user   = os.environ.get("AMI_USER",   "").strip()
    env_secret = os.environ.get("AMI_SECRET", "").strip()

    # Ignore placeholder values that ship in the default service file
    PLACEHOLDERS = {"yourpassword", "your_secret_here", "changeme", "amp111", ""}

    if env_user and env_secret and env_secret.lower() not in PLACEHOLDERS:
        log("INFO", f"[AMI-CREDS] Using env vars: AMI_USER='{env_user}'")
        result["user"]   = env_user
        result["secret"] = env_secret
        return result
    elif env_user and env_secret and env_secret.lower() in PLACEHOLDERS:
        log("WARN", f"[AMI-CREDS] AMI_SECRET is a placeholder ('{env_secret}') — "
                    "falling through to read manager.conf directly")
    else:
        log("INFO", f"[AMI-CREDS] AMI_USER/AMI_SECRET not set — reading {MANAGER_CONF} directly")

    log("INFO", f"[AMI-CREDS] Parsing {MANAGER_CONF} for credentials")
    try:
        with open(MANAGER_CONF) as f:
            raw = f.read()
    except FileNotFoundError:
        log("ERROR", f"[AMI-CREDS] {MANAGER_CONF} not found — set AMI_USER/AMI_SECRET in service file")
        return result
    except PermissionError:
        log("ERROR", f"[AMI-CREDS] Cannot read {MANAGER_CONF} (permission denied) — set AMI_USER/AMI_SECRET in service file")
        return result
    except Exception as e:
        log("ERROR", f"[AMI-CREDS] Error reading {MANAGER_CONF}: {e}")
        return result

    # Extract port from [general]
    m = re.search(r'^\s*port\s*=\s*(\d+)', raw, re.MULTILINE)
    if m:
        result["port"] = int(m.group(1))
        log("INFO", f"[AMI-CREDS] AMI port from manager.conf: {result['port']}")

    # Walk stanzas — take the FIRST non-[general] stanza that has a secret
    # and is not explicitly disabled. Do NOT require a write= line.
    current_header  = None
    current_secret  = None
    current_enabled = True

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        hdr = re.match(r'^\[([^\]]+)\]', line)
        if hdr:
            if current_header and current_header.lower() != "general" and current_secret and current_enabled:
                log("INFO", f"[AMI-CREDS] Using manager.conf user: '{current_header}'")
                result["user"]   = current_header
                result["secret"] = current_secret
                return result
            current_header  = hdr.group(1).strip()
            current_secret  = None
            current_enabled = True
            log("DEBUG", f"[AMI-CREDS] Entering stanza [{current_header}]")
            continue

        if "=" in line:
            kv  = line.split(";", 1)[0]
            key = kv.split("=", 1)[0].strip().lower()
            val = kv.split("=", 1)[1].strip()

            if key == "secret":
                current_secret = val
                log("DEBUG", f"[AMI-CREDS] [{current_header}] secret found")
            elif key == "enabled":
                current_enabled = val.lower() not in ("no", "false", "0")
                log("DEBUG", f"[AMI-CREDS] [{current_header}] enabled={current_enabled}")
            elif key == "permit":
                log("DEBUG", f"[AMI-CREDS] [{current_header}] permit={val} (must include 127.0.0.1)")
            elif key == "deny":
                log("WARN", f"[AMI-CREDS] [{current_header}] deny={val} (check this doesn't block localhost)")

    if current_header and current_header.lower() != "general" and current_secret and current_enabled:
        log("INFO", f"[AMI-CREDS] Using manager.conf user: '{current_header}'")
        result["user"]   = current_header
        result["secret"] = current_secret
        return result

    log("ERROR",
        "[AMI-CREDS] No usable AMI user found in manager.conf. "
        "Add a user stanza with 'secret = ...' OR set AMI_USER and AMI_SECRET "
        "in /etc/systemd/system/ASL3-EZ.service then: "
        "systemctl daemon-reload && systemctl restart ASL3-EZ")
    return result


# ---------------------------------------------------------------------------
# AMI TCP client
#
# Protocol reference (confirmed against Asterisk 20 / ASL3):
#   1. TCP connect to port 5038
#   2. Asterisk sends ONE line: "Asterisk Call Manager/X.Y.Z\r\n"
#      — this is NOT a key:value packet, it has no \r\n\r\n terminator
#   3. Client sends Login action (terminated by blank line = \r\n\r\n)
#   4. Asterisk responds with Response: Success\r\n\r\n  (or Error)
#   5. All subsequent actions end with \r\n\r\n
#   6. AMI Command responses accumulate lines starting with "Output:"
#      and end with "--END COMMAND--\r\n\r\n"
# ---------------------------------------------------------------------------
class AMIClient:

    def __init__(self, host, port, user, secret, timeout=12):
        self.host    = host
        self.port    = port
        self.user    = user
        self.secret  = secret
        self.timeout = timeout
        self._sock   = None

    # ── low-level I/O ────────────────────────────────────────────────────────

    def _send(self, text: str):
        self._sock.sendall(text.encode("utf-8"))

    def _recv_until(self, sentinel: str, timeout: float = None) -> str:
        deadline = time.time() + (timeout or self.timeout)
        buf = ""
        self._sock.settimeout(0.5)
        while time.time() < deadline:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                if sentinel in buf:
                    break
            except socket.timeout:
                continue
            except Exception as e:
                log("WARN", f"[AMI] recv error: {e}")
                break
        self._sock.settimeout(self.timeout)
        return buf

    def _send_action(self, params: dict):
        msg = "".join(f"{k}: {v}\r\n" for k, v in params.items()) + "\r\n"
        log("DEBUG", f"[AMI] >> {list(params.items())[:3]}")
        self._send(msg)

    def _parse_packet(self, raw: str) -> dict:
        result = {}
        for line in raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k:
                    result[k] = v
        return result

    # ── connect / login ───────────────────────────────────────────────────────

    def connect(self):
        log("INFO", f"[AMI] Connecting to {self.host}:{self.port} as '{self.user}'")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)

        try:
            self._sock.connect((self.host, self.port))
        except ConnectionRefusedError:
            raise Exception(
                f"AMI connection refused ({self.host}:{self.port}). "
                "Is Asterisk running? Check: systemctl status asterisk"
            )
        except OSError as e:
            raise Exception(f"AMI connect failed: {e}")

        banner = self._recv_until("\r\n", timeout=5)
        banner = banner.strip()
        log("INFO", f"[AMI] Banner: {banner!r}")
        if not banner:
            raise Exception(
                "AMI: no banner received. Is Asterisk running and "
                "manager.conf 'enabled = yes'?"
            )
        if "Asterisk Call Manager" not in banner:
            log("WARN", f"[AMI] Unexpected banner (continuing anyway): {banner!r}")

        login_action = (
            f"Action: Login\r\n"
            f"Username: {self.user}\r\n"
            f"Secret: {self.secret}\r\n"
            f"Events: off\r\n"
            f"\r\n"
        )
        log("DEBUG", f"[AMI] Sending Login for user '{self.user}'")
        self._send(login_action)

        resp_raw = self._recv_until("\r\n\r\n", timeout=self.timeout)
        log("DEBUG", f"[AMI] Login raw response: {resp_raw!r}")

        chunks = [c.strip() for c in resp_raw.split("\r\n\r\n") if c.strip()]
        login_response = {}
        for chunk in chunks:
            pkt = self._parse_packet(chunk)
            if "Response" in pkt:
                login_response = pkt

        log("DEBUG", f"[AMI] Login response parsed: {login_response}")

        if not login_response:
            raise Exception(
                f"AMI: No Response packet received after Login. "
                f"Raw data was: {resp_raw!r}"
            )

        if login_response.get("Response") == "Success":
            log("INFO", f"[AMI] Authenticated successfully as '{self.user}'")
        else:
            msg = login_response.get("Message", "unknown")
            raise Exception(
                f"AMI authentication failed: {msg}\n"
                f"  User:   '{self.user}'\n"
                f"  Host:   {self.host}:{self.port}\n"
                f"  Fix:    Set AMI_USER and AMI_SECRET in ASL3-EZ.service to match\n"
                f"          the [username] and secret= in /etc/asterisk/manager.conf\n"
                f"  Then:   systemctl daemon-reload && systemctl restart ASL3-EZ"
            )

    def close(self):
        try:
            if self._sock:
                self._send("Action: Logoff\r\n\r\n")
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        log("DEBUG", "[AMI] Session closed")

    # ── AMI Command ───────────────────────────────────────────────────────────

    def command(self, cmd: str) -> list:
        log("INFO", f"[AMI] CMD: {cmd!r}")
        action = (
            f"Action: Command\r\n"
            f"Command: {cmd}\r\n"
            f"\r\n"
        )
        self._send(action)

        # This Asterisk build's AMI (banner reports protocol 11.0.0) never
        # emits "--END COMMAND--" — every Command response, with or without
        # output, is terminated the same way as any other AMI packet: a
        # blank line. Waiting on "--END COMMAND--" meant every call here
        # silently burned the full internal timeout before returning,
        # compounding badly for sequences like disconnect-then-connect and
        # for the background poller (which issues several of these per
        # cycle), eventually exceeding gunicorn's request timeout. Verified
        # directly against this AMI instance: responses arrive in <1ms and
        # are always closed by "\r\n\r\n", matching the Login flow above.
        raw = self._recv_until("\r\n\r\n", timeout=self.timeout)
        log("DEBUG", f"[AMI] CMD raw ({len(raw)} bytes): {raw[:300]!r}")

        output = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("Output:"):
                output.append(line[7:].strip())
            elif line.startswith("Response: Error"):
                log("WARN", f"[AMI] Command returned error for: {cmd!r}")

        log("DEBUG", f"[AMI] CMD output lines: {len(output)}")
        return output

    # ── app_rpt helpers ───────────────────────────────────────────────────────

    def rpt_cmd(self, node: str, subcmd: str) -> dict:
        """
        Issue an app_rpt command via AMI.

        ilink function numbers:
            1  = disconnect specific node
            2  = connect monitor-only
            3  = connect transceive
            6  = disconnect all
            12 = permanent monitor
            13 = permanent transceive
        """
        full_cmd = f"rpt cmd {node} {subcmd}"
        log("INFO", f"[AMI] rpt_cmd: {full_cmd!r}")
        output_lines = self.command(full_cmd)
        log("DEBUG", f"[AMI] rpt_cmd output: {output_lines}")

        error_indicators = ["error", "invalid", "no such", "failed", "unknown command"]
        raw_joined = " ".join(output_lines).lower()
        is_error = any(e in raw_joined for e in error_indicators)

        return {
            "output":  output_lines,
            "success": not is_error,
            "raw":     raw_joined,
            "command": full_cmd,
        }

    def get_node_status(self, node: str) -> dict:
        """
        Return keyed state and connected node list for `node`.

        Keyed detection uses 'rpt show variables <node>' and the
        RPT_RXKEYED=0/1 variable. The previous implementation called
        'rpt show nodes <node>' for this, but that CLI command does not
        exist in this app_rpt build (confirmed via `core show help rpt`
        and a live "No such command" response) — it always errored, so
        `keyed` was never actually set. RPT_RXKEYED is confirmed present
        and correct via 'rpt show variables' on this build.

        Connected nodes come from 'rpt lstats <node>' which gives one line
        per connected node containing the remote node number.
        """
        status = {"keyed": False, "connected": [], "raw": [], "lstats": []}

        # Primary: rpt show variables — RPT_RXKEYED gives keyed state
        lines = self.command(f"rpt show variables {node}")
        status["raw"] = lines
        log("DEBUG", f"[AMI] rpt show variables {node} -> {lines}")
        for line in lines:
            if re.search(r'\bRPT_RXKEYED\s*=\s*1\b', line, re.IGNORECASE):
                status["keyed"] = True

        # Secondary: rpt lstats — definitive connected node list
        lstats = self.command(f"rpt lstats {node}")
        status["lstats"] = lstats
        log("DEBUG", f"[AMI] rpt lstats {node} -> {lstats}")
        for line in lstats:
            for n in re.findall(r'\b(\d{4,7})\b', line):
                if n != str(node) and n not in status["connected"]:
                    status["connected"].append(n)

        return status


# ---------------------------------------------------------------------------
# Persistent AMI connection pool + background poller
#
# Instead of opening a new TCP socket on every HTTP request, we keep one
# long-lived AMI session and have a background thread poll it on a fixed
# interval. API endpoints read from the cache (sub-millisecond) rather than
# waiting on a fresh TCP round-trip each time.
#
# The old path per status request:
#   TCP connect (~10-50ms) + banner + login + command + logoff = ~100-300ms
#
# With pool:
#   /api/ami/status = dict lookup = <1ms
#   Background thread does all AMI work decoupled from HTTP requests
# ---------------------------------------------------------------------------

_ami_pool_lock   = threading.Lock()
_ami_client      = None      # the live AMIClient instance (or None)
_ami_cache       = {}        # {node: status_dict}
_ami_cache_ts    = {}        # {node: float unix timestamp}
_ami_last_error  = None      # last connection error string
_ami_connected   = False


def _ami_ensure_connected() -> AMIClient:
    """
    Return the live AMIClient, (re)connecting if necessary.
    Must be called with _ami_pool_lock held.
    """
    global _ami_client, _ami_connected, _ami_last_error
    if _ami_client is not None:
        return _ami_client
    creds = parse_manager_conf()
    if not creds.get("user") or not creds.get("secret"):
        raise Exception("AMI credentials not configured")
    client = AMIClient(creds["host"], creds["port"], creds["user"], creds["secret"])
    client.connect()
    _ami_client    = client
    _ami_connected = True
    _ami_last_error = None
    log("INFO", "[AMI-POOL] Persistent connection established")
    return client


def _ami_invalidate():
    """
    Drop the current connection so the next call reconnects.
    Must be called with _ami_pool_lock held.
    """
    global _ami_client, _ami_connected
    try:
        if _ami_client:
            _ami_client.close()
    except Exception:
        pass
    _ami_client    = None
    _ami_connected = False
    log("WARN", "[AMI-POOL] Connection invalidated — will reconnect on next poll")


def _poll_loop():
    """
    Background daemon thread. Polls node status over the persistent AMI
    connection and writes results into _ami_cache. On any socket error the
    connection is dropped and re-established on the next iteration.
    """
    global _ami_last_error
    log("INFO", f"[AMI-POLL] Background poller started (interval={POLL_INTERVAL}s)")
    while True:
        try:
            content = read_conf_file(RPT_CONF_PATH)
            nodes   = get_node_numbers(content) if content else []
            if not nodes:
                time.sleep(POLL_INTERVAL)
                continue

            with _ami_pool_lock:
                try:
                    ami = _ami_ensure_connected()
                    for node in nodes:
                        status = ami.get_node_status(node)
                        _ami_cache[node]    = status
                        _ami_cache_ts[node] = time.time()
                except Exception as e:
                    _ami_last_error = str(e)
                    log("ERROR", f"[AMI-POLL] Error during poll: {e}")
                    _ami_invalidate()

        except Exception as outer:
            log("ERROR", f"[AMI-POLL] Unexpected outer error: {outer}")

        time.sleep(POLL_INTERVAL)


def start_poller():
    t = threading.Thread(target=_poll_loop, name="ami-poller", daemon=True)
    t.start()
    log("INFO", "[AMI-POLL] Poller thread launched")


def get_cached_status(node: str) -> dict:
    """
    Return the most recent cached status for a node.
    If the cache is stale or missing, stale=True is included in the result
    so the frontend can show a loading indicator rather than wrong data.
    """
    node   = str(node)
    status = _ami_cache.get(node)
    ts     = _ami_cache_ts.get(node, 0)
    age    = time.time() - ts
    if status is None:
        return {"node": node, "stale": True, "age": None, "error": "No data yet"}
    return {**status, "node": node, "stale": age > CACHE_TTL, "age": round(age, 2)}


def ami_send_command(subcmd_fn) -> dict:
    """
    Execute a one-off command (connect/disconnect/etc.) over the persistent
    connection. subcmd_fn receives an AMIClient and returns a result dict.
    Falls back to a fresh connection if the persistent one is broken.
    """
    with _ami_pool_lock:
        try:
            ami = _ami_ensure_connected()
            return subcmd_fn(ami)
        except Exception as e:
            log("WARN", f"[AMI-POOL] Command failed on persistent conn: {e} — retrying fresh")
            _ami_invalidate()
            try:
                ami = _ami_ensure_connected()
                return subcmd_fn(ami)
            except Exception as e2:
                _ami_invalidate()
                raise e2


# ---------------------------------------------------------------------------
# rpt.conf file helpers
# ---------------------------------------------------------------------------
def read_conf_file(path):
    try:
        with open(path) as f:
            content = f.read()
        log("DEBUG", f"[CONF] Read {len(content)} bytes from {path}")
        return content
    except FileNotFoundError:
        log("ERROR", f"[CONF] File not found: {path}")
        return None
    except PermissionError:
        log("ERROR", f"[CONF] Permission denied reading {path} (running as UID {os.getuid()})")
        return None
    except Exception as e:
        log("ERROR", f"[CONF] Error reading {path}: {e}")
        return None


def write_conf_file(path, content):
    """
    Atomically write content to path:
      1. Create timestamped backup of existing file
      2. Write to a temp file in the same directory
      3. fsync + rename (atomic on Linux)
      4. Restore ownership to asterisk:asterisk and mode to 640
         so ASL3/Asterisk (which runs as the asterisk user) can read the file.

    Per https://allstarlink.github.io/adv-topics/permissions/:
      - Files that Asterisk reads must be readable by the asterisk user
      - Owner: asterisk, Group: asterisk, Mode: 640 (rw-r-----)
      - Parent directory must also be accessible by asterisk

    Raises PermissionError / OSError on failure.
    """
    # Ensure backup directory exists and is accessible by asterisk
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        uid = pwd.getpwnam("asterisk").pw_uid
        gid = grp.getgrnam("asterisk").gr_gid
        _have_asterisk_ids = True
    except KeyError:
        uid = gid = -1
        _have_asterisk_ids = False
        log("WARN", "[CONF] 'asterisk' user/group not found — skipping chown (non-ASL3 system?)")

    # Set backup directory ownership so asterisk can read backups
    if _have_asterisk_ids:
        try:
            os.chown(BACKUP_DIR, uid, gid)
            os.chmod(BACKUP_DIR, 0o750)
        except Exception as e:
            log("WARN", f"[CONF] Could not set backup dir permissions: {e}")

    # Backup existing file
    backup_path = None
    if os.path.exists(path):
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"rpt.conf.{ts}.bak")
        shutil.copy2(path, backup_path)
        log("INFO", f"[CONF] Backup created: {backup_path}")
        if _have_asterisk_ids:
            try:
                os.chown(backup_path, uid, gid)
                os.chmod(backup_path, 0o640)
            except Exception as e:
                log("WARN", f"[CONF] Could not set backup file permissions: {e}")

    # Write atomically via temp file
    conf_dir = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=conf_dir, prefix=".rpt_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, path)
            log("INFO", f"[CONF] Saved {len(content)} bytes to {path}")

            # Restore ownership and permissions required by ASL3.
            # The temp file was created as root:root — after os.rename those
            # credentials stick.  Asterisk runs as asterisk:asterisk and must
            # be able to read rpt.conf or it crashes on reload/restart.
            if _have_asterisk_ids:
                try:
                    os.chown(path, uid, gid)
                    os.chmod(path, 0o640)
                    log("INFO", f"[CONF] Restored {path} owner=asterisk:asterisk mode=640")
                except PermissionError as e:
                    log("ERROR", f"[CONF] chown/chmod failed: {e}")

        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except PermissionError as e:
        log("ERROR", f"[CONF] Permission denied writing {path}: {e}. "
                     f"Running as UID {os.getuid()}. Service must run as root (User=root).")
        raise

    return backup_path


def get_node_numbers(content):
    nodes = []
    for line in content.splitlines():
        m = re.match(r'^\s*\[(\d{4,7})\]', line)
        if m:
            nodes.append(m.group(1))
    return nodes


def parse_stanza_settings(content, stanza_name):
    """
    Parse key=value pairs from a specific named stanza in rpt.conf.

    ASL3 uses Asterisk config templates — a node stanza like [64393](node-main)
    inherits all settings from [node-main](!) but can override them.
    This function returns the *effective* settings for the requested stanza by:
      1. Collecting settings from the template stanza it inherits from (if any)
      2. Overlaying settings from the named stanza itself (overrides win)

    This matches how Asterisk actually reads the file and fixes the bug where
    the old flat parser would return the wrong value when the same key appeared
    in multiple stanzas (e.g. duplex=3 in [node-main] but duplex=0 somewhere else).

    Returns dict: { key: {"value": str, "commented": bool, "raw_line": str} }
    """
    lines = content.splitlines()

    # ── Pass 1: collect all stanzas ──────────────────────────────────────────
    # stanzas = { name: {"template": str|None, "lines": [...]} }
    stanzas = {}
    current = None
    for line in lines:
        s = line.strip()
        hdr = re.match(r'^\[([^\]]+)\](?:\(([^)]+)\))?', s)
        if hdr:
            raw_name = hdr.group(1).strip()
            raw_tmpl = (hdr.group(2) or "").strip()
            # Template definition stanzas end with (!) — skip them as base stanzas
            is_template_def = raw_tmpl == "!"
            template_ref    = None if (not raw_tmpl or raw_tmpl == "!") else raw_tmpl
            current = raw_name
            stanzas[current] = {
                "is_template": is_template_def,
                "template":    template_ref,
                "lines":       [],
            }
        elif current is not None:
            stanzas[current]["lines"].append(line)

    def parse_lines(lines_list):
        """Parse key=value pairs from a list of raw lines."""
        result = {}
        for line in lines_list:
            stripped  = line.strip()
            commented = stripped.startswith(";")
            if commented:
                # Heavily-indented comment-only lines are wrapped documentation
                # (e.g. stock rpt.conf explains node_lookup_method's "both"/
                # "dns"/"file" values as indented comment lines under the
                # directive), not a real disabled setting — those are
                # conventionally flush-left. Without this, "both"/"dns"/"file"
                # get parsed as bogus settings of their own.
                indent = len(line) - len(line.lstrip(" \t"))
                if indent > 8:
                    continue
                stripped = stripped[1:].strip()
            m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^;]*?)(?:\s*;.*)?$', stripped)
            if m:
                k = m.group(1).strip()
                v = m.group(2).strip()
                result[k] = {"value": v, "commented": commented, "raw_line": line}
        return result

    # ── Pass 2: resolve effective settings for the requested stanza ──────────
    target = stanzas.get(stanza_name)
    if target is None:
        log("WARN", f"[CONF] Stanza [{stanza_name}] not found in rpt.conf")
        return {}

    # Start with template settings if this stanza inherits one
    effective = {}
    tmpl_name = target.get("template")
    if tmpl_name and tmpl_name in stanzas:
        effective = parse_lines(stanzas[tmpl_name]["lines"])
        log("DEBUG", f"[CONF] [{stanza_name}] inherits from [{tmpl_name}]: {len(effective)} base settings")

    # Overlay with the stanza's own settings (these override the template)
    own = parse_lines(target["lines"])
    effective.update(own)
    log("DEBUG", f"[CONF] [{stanza_name}] effective settings: {len(effective)} total ({len(own)} own overrides)")

    return effective


def parse_node_settings(content):
    """
    Legacy flat parser — kept for the /api/conf general_settings field.
    Parses the entire file as a flat dict (last value for any key wins).
    Use parse_stanza_settings() for per-stanza accurate parsing.
    """
    settings = {}
    for line in content.splitlines():
        stripped  = line.strip()
        commented = stripped.startswith(";")
        if commented:
            stripped = stripped[1:].strip()
        m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^;]*?)(?:\s*;.*)?$', stripped)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            settings[k] = {"value": v, "commented": commented, "raw_line": line}
    return settings


def update_setting_in_content(content, section, key, value, enable=True):
    """Update or insert a key=value in the given section of the config."""
    lines   = content.splitlines(keepends=True)
    result  = []
    in_sec  = False
    found   = False

    for line in lines:
        s = line.strip()
        sec_m = re.match(r'^\[([^\]\(]+)', s)
        if sec_m:
            in_sec = (sec_m.group(1).strip() == section)

        if in_sec and not found:
            test = s.lstrip(";").strip()
            km   = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*=', test)
            if km and km.group(1) == key:
                found = True
                prefix = "" if enable else ";"
                result.append(f"{prefix}{key} = {value}\n")
                continue

        result.append(line)

    if not found:
        new_lines  = []
        in_target  = False
        inserted   = False
        for line in result:
            s     = line.strip()
            sec_m = re.match(r'^\[([^\]\(]+)', s)
            if sec_m:
                if in_target and not inserted:
                    prefix = "" if enable else ";"
                    new_lines.append(f"{prefix}{key} = {value}\n")
                    inserted = True
                in_target = (sec_m.group(1).strip() == section)
            new_lines.append(line)
        if in_target and not inserted:
            prefix = "" if enable else ";"
            new_lines.append(f"{prefix}{key} = {value}\n")
        result = new_lines

    return "".join(result)


# ---------------------------------------------------------------------------
# rpt.conf setting validation
#
# Mirrors the GENERAL_META / NODE_SECS metadata in templates/index.html
# (sourced from https://allstarlink.github.io/config/rpt_conf/). The web UI
# already restricts these fields to dropdowns/number inputs, but a request
# can bypass the UI entirely, so the same constraints are enforced here
# before anything is written to rpt.conf. Keys not listed are free-form
# (paths, stanza names, etc.) and are not validated.
# ---------------------------------------------------------------------------
SETTINGS_SCHEMA = {
    # [general]
    "node_lookup_method": {"type": "enum", "options": ["both", "dns", "file"]},
    "max_dns_node_length": {"type": "number", "min": 1},

    # Basic / Required
    "duplex":              {"type": "enum", "options": ["0", "1", "2", "3", "4"]},

    # Station Identification
    "idtime":              {"type": "number", "min": 0},
    "politeid":            {"type": "number", "min": 0},
    "beaconing":           {"type": "enum", "options": ["0", "1"]},

    # Timers
    "hangtime":            {"type": "number", "min": 0},
    "althangtime":         {"type": "number", "min": 0},
    "totime":              {"type": "number", "min": 0, "max": 9999999},
    "time_out_reset_unkey_interval":    {"type": "number", "min": 0, "max": 10000},
    "time_out_reset_kerchunk_interval": {"type": "number", "min": 0},
    "sleeptime":           {"type": "number", "min": 0},

    # Telemetry
    "telemdefault":        {"type": "enum", "options": ["0", "1", "2"]},
    "telemdynamic":        {"type": "enum", "options": ["0", "1"]},
    "holdofftelem":        {"type": "enum", "options": ["0", "1"]},
    "telemduckdb":         {"type": "number"},
    "telemnomdb":          {"type": "number"},
    "nounkeyct":           {"type": "enum", "options": ["yes", "no"]},
    "nolocallinkct":       {"type": "enum", "options": ["0", "1"]},

    # DTMF & Functions
    "dtmfkey":             {"type": "enum", "options": ["0", "1"]},
    "propagate_dtmf":      {"type": "enum", "options": ["yes", "no"]},
    "linktolink":          {"type": "enum", "options": ["yes", "no"]},

    # Node Connections
    "lnkactenable":        {"type": "enum", "options": ["0", "1"]},
    "lnkacttime":          {"type": "number", "min": 0},

    # Audio
    "linkmongain":         {"type": "number"},
    "erxgain":             {"type": "number"},
    "etxgain":             {"type": "number"},

    # Tail & Scheduler
    "tailmessagetime":     {"type": "number", "min": 0, "max": 200000000},
    "tailsquashedtime":    {"type": "number", "min": 0},

    # Parrot / Echo
    "parrot":              {"type": "enum", "options": ["0", "1"]},
    "parrottime":          {"type": "number", "min": 0},

    # EchoLink
    "eannmode":            {"type": "enum", "options": ["0", "1", "2", "3"]},
    "echolinkdefault":     {"type": "enum", "options": ["0", "1", "2", "3"]},
    "echolinkdynamic":     {"type": "enum", "options": ["0", "1"]},

    # Archiving & Stats
    "archiveformat":       {"type": "enum", "options": ["wav49", "wav", "gsm"]},
    "archiveaudio":        {"type": "enum", "options": ["yes", "no"]},

    # Stanza Name Overrides (telemetry-mode keys despite the section name)
    "guilinkdefault":      {"type": "enum", "options": ["0", "1", "2", "3"]},
    "guilinkdynamic":      {"type": "enum", "options": ["0", "1"]},
    "phonelinkdefault":    {"type": "enum", "options": ["0", "1", "2", "3"]},
    "phonelinkdynamic":    {"type": "enum", "options": ["0", "1"]},
    "tlbdefault":          {"type": "enum", "options": ["0", "1", "2", "3"]},
    "tlbdynamic":          {"type": "enum", "options": ["0", "1"]},

    # Voting
    "votermode":           {"type": "enum", "options": ["0", "1", "2"]},
    "votertype":           {"type": "enum", "options": ["0", "1", "2"]},
    "votermargin":         {"type": "number"},
}


def validate_setting(key, value):
    """
    Return None if `value` is acceptable for `key`, otherwise an error string.
    Keys with no schema entry are free-form and always pass.
    """
    schema = SETTINGS_SCHEMA.get(key)
    if schema is None or value == "":
        return None

    if schema["type"] == "enum":
        if value not in schema["options"]:
            return f"{key}: {value!r} is not valid. Must be one of {schema['options']}"
    elif schema["type"] == "number":
        try:
            num = float(value)
        except ValueError:
            return f"{key}: {value!r} is not a number"
        if "min" in schema and num < schema["min"]:
            return f"{key}: {value!r} is below the minimum of {schema['min']}"
        if "max" in schema and num > schema["max"]:
            return f"{key}: {value!r} is above the maximum of {schema['max']}"

    return None


# ---------------------------------------------------------------------------
# Node lookup — AllStarLink Allmon DB (live API) + local astdb.txt fallback
# ---------------------------------------------------------------------------
_astdb_cache   = {}
_astdb_loaded  = False
_astdb_lock    = threading.Lock()


def load_astdb():
    global _astdb_cache, _astdb_loaded
    with _astdb_lock:
        for path in ASTDB_PATHS:
            if os.path.exists(path):
                try:
                    count = 0
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            sep = "|" if "|" in line else ","
                            parts = [p.strip() for p in line.split(sep)]
                            if len(parts) >= 2:
                                node     = parts[0]
                                callsign = parts[1] if len(parts) > 1 else ""
                                desc     = parts[2] if len(parts) > 2 else ""
                                location = parts[3] if len(parts) > 3 else ""
                                _astdb_cache[node] = {
                                    "callsign": callsign,
                                    "desc":     desc,
                                    "location": location,
                                }
                                count += 1
                    _astdb_loaded = True
                    log("INFO", f"[ASTDB] Loaded {count} nodes from {path}")
                    return True
                except Exception as e:
                    log("ERROR", f"[ASTDB] Failed to load {path}: {e}")
    log("WARN", "[ASTDB] No local astdb.txt found — will use live API for lookups")
    return False


def fetch_allmondb_node(node: str) -> dict:
    global _allmondb_cache, _allmondb_loaded
    node = str(node)

    with _allmondb_lock:
        if node in _allmondb_cache:
            return _allmondb_cache[node]

    if _astdb_loaded and node in _astdb_cache:
        return _astdb_cache[node]

    try:
        url = ALLMONDB_URL
        req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0"})
        log("INFO", f"[ALLMONDB] Fetching node database from {url}")
        with urlreq.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="replace")

        count = 0
        with _allmondb_lock:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                sep   = "|" if "|" in line else ","
                parts = [p.strip() for p in line.split(sep)]
                if len(parts) >= 2:
                    n = parts[0]
                    _allmondb_cache[n] = {
                        "callsign": parts[1] if len(parts) > 1 else "",
                        "desc":     parts[2] if len(parts) > 2 else "",
                        "location": parts[3] if len(parts) > 3 else "",
                    }
                    count += 1
            _allmondb_loaded = True
            log("INFO", f"[ALLMONDB] Loaded {count} nodes from live API")
            return _allmondb_cache.get(node, {"callsign": "", "desc": "", "location": ""})

    except Exception as e:
        log("WARN", f"[ALLMONDB] API fetch failed: {e} — falling back to local cache")
        return _astdb_cache.get(node, {"callsign": "", "desc": "", "location": ""})


def lookup_node(node: str) -> dict:
    node = str(node)

    with _allmondb_lock:
        if node in _allmondb_cache:
            return _allmondb_cache[node]

    if not _astdb_loaded:
        load_astdb()
    if node in _astdb_cache:
        return _astdb_cache[node]

    return fetch_allmondb_node(node)


# ---------------------------------------------------------------------------
# System info helpers
# ---------------------------------------------------------------------------
def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass
    try:
        r = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r'[\d.]+', r.stdout)
        if m:
            return float(m.group())
    except Exception:
        pass
    return None


def get_disk_usage():
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            p = lines[1].split()
            return {"total": p[1], "used": p[2], "avail": p[3], "pct": p[4]}
    except Exception:
        pass
    return {}


def get_uptime():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"
    except Exception:
        return "unknown"


def get_asl_version():
    try:
        r = subprocess.run(["dpkg", "-l", "asl3"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("ii"):
                return line.split()[2]
    except Exception:
        pass
    return "unknown"


def get_asterisk_status():
    try:
        r = subprocess.run([SYSTEMCTL_PATH, "is-active", "asterisk"],
                           capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
    except Exception:
        active = False

    version = "unknown"
    try:
        r = subprocess.run([ASTERISK_PATH, "-rx", "core show version"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            version = r.stdout.strip().splitlines()[0]
    except Exception:
        pass

    return {"active": active, "version": version}


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    content = read_conf_file(RPT_CONF_PATH)
    nodes   = get_node_numbers(content) if content else []
    return render_template("index.html",
                           conf_exists=content is not None,
                           nodes=nodes,
                           conf_path=RPT_CONF_PATH)


# ── rpt.conf API ──────────────────────────────────────────────────────────────

@app.route("/api/conf")
def api_get_conf():
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        log("ERROR", f"[API] /api/conf: cannot read {RPT_CONF_PATH}")
        return jsonify({"error": "Cannot read rpt.conf", "path": RPT_CONF_PATH,
                        "hint": "Ensure the service runs as root (User=root in service file)"}), 404
    nodes = get_node_numbers(content)
    return jsonify({
        "content":          content,
        "nodes":            nodes,
        "general_settings": parse_stanza_settings(content, "general"),
    })


@app.route("/api/conf/node/<node>")
def api_get_node_conf(node):
    """
    Return the effective settings for a specific node stanza, correctly
    resolving Asterisk template inheritance (e.g. [64393](node-main) inherits
    from [node-main](!)).  This fixes the bug where the flat parser returned
    the wrong value when the same key appeared in multiple stanzas.
    """
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    settings = parse_stanza_settings(content, node)
    if not settings:
        # Try common template names as fallback
        for tmpl in ["node-main", "node-template", node]:
            s = parse_stanza_settings(content, tmpl)
            if s:
                settings = s
                log("INFO", f"[API] /api/conf/node/{node}: using template [{tmpl}] as fallback")
                break
    return jsonify({"node": node, "settings": settings})


@app.route("/api/save", methods=["POST"])
def api_save():
    data    = request.json or {}
    content = read_conf_file(RPT_CONF_PATH) or ""
    raw     = data.get("raw_content")

    if raw is not None:
        log("INFO", f"[API] /api/save raw content ({len(raw)} bytes)")
        try:
            backup = write_conf_file(RPT_CONF_PATH, raw)
            return jsonify({"success": True, "backup": backup,
                            "message": f"Saved. Backup: {backup}"})
        except PermissionError as e:
            return jsonify({"error": str(e),
                            "hint": "Service must run as root. Check User=root in ASL3-EZ.service"}), 403
        except Exception as e:
            log("ERROR", f"[API] /api/save error: {e}")
            return jsonify({"error": str(e)}), 500

    section = data.get("section", "")
    changes = data.get("changes", {})
    log("INFO", f"[API] /api/save section={section!r} changes={list(changes.keys())}")

    errors = []
    for key, info in changes.items():
        if info.get("enabled", True):
            err = validate_setting(key, info.get("value", ""))
            if err:
                errors.append(err)
    if errors:
        log("WARN", f"[API] /api/save rejected invalid value(s): {errors}")
        return jsonify({"error": "Invalid setting value(s)", "details": errors}), 400

    for key, info in changes.items():
        content = update_setting_in_content(
            content, section, key,
            info.get("value", ""), enable=info.get("enabled", True)
        )

    try:
        backup = write_conf_file(RPT_CONF_PATH, content)
        return jsonify({"success": True, "backup": backup,
                        "message": f"Saved {len(changes)} setting(s). Backup: {backup}"})
    except PermissionError as e:
        return jsonify({"error": str(e),
                        "hint": "Service must run as root. Check User=root in ASL3-EZ.service"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/save error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/restart", methods=["POST"])
def api_restart():
    log("INFO", "[API] /api/restart called")
    try:
        r = subprocess.run(
            [SYSTEMCTL_PATH, "restart", "asterisk"],
            capture_output=True, text=True, timeout=30
        )
        log("INFO", f"[API] systemctl restart asterisk -> rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        if r.returncode == 0:
            return jsonify({"success": True,
                            "output": r.stdout or "Asterisk restarted successfully.",
                            "command": f"{SYSTEMCTL_PATH} restart asterisk"})
        return jsonify({
            "error":     r.stderr.strip() or f"systemctl returned code {r.returncode}",
            "stdout":    r.stdout,
            "returncode": r.returncode,
            "hint":      "Check: systemctl status asterisk  and  journalctl -u asterisk -n 30",
        }), 500
    except PermissionError as e:
        return jsonify({"error": str(e),
                        "hint": "Service must run as root to call systemctl restart"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/restart exception: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """
    Reload rpt.conf without restarting Asterisk.

    'rpt reload' is NOT a real app_rpt CLI command (verified against
    `core show help rpt` — app_rpt only registers 'rpt restart'). Asterisk's
    `-rx` always exits 0 even for an unknown command, so the previous
    "rpt reload" call silently did nothing while the API still reported
    success — saved changes never actually took effect until a full
    Asterisk restart. 'rpt restart' re-reads rpt.conf and applies changes
    live without restarting all of Asterisk.
    """
    log("INFO", "[API] /api/reload called")
    cmd = "rpt restart"
    try:
        r = subprocess.run(
            [ASTERISK_PATH, "-rx", cmd],
            capture_output=True, text=True, timeout=15
        )
        out = r.stdout.strip()
        log("INFO", f"[API] asterisk -rx '{cmd}' -> rc={r.returncode} out={out!r}")
        if r.returncode != 0 or "No such command" in out:
            return jsonify({"error": out or f"asterisk returned code {r.returncode}",
                            "command": f"{ASTERISK_PATH} -rx '{cmd}'"}), 500
        return jsonify({"success": True,
                        "output":  out or "rpt.conf reloaded.",
                        "command": f"{ASTERISK_PATH} -rx '{cmd}'"})
    except Exception as e:
        log("ERROR", f"[API] /api/reload exception: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups")
def api_backups():
    if not os.path.exists(BACKUP_DIR):
        return jsonify({"backups": [], "backup_dir": BACKUP_DIR})
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".bak")],
        reverse=True
    )[:10]
    return jsonify({"backups": files, "backup_dir": BACKUP_DIR})


@app.route("/api/backup/<filename>")
def api_get_backup(filename):
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', filename):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        return jsonify({"content": f.read(), "filename": filename})


# ── Favorites API ─────────────────────────────────────────────────────────────

@app.route("/api/favorites")
def api_favorites():
    try:
        db   = get_db()
        rows = db.execute("SELECT * FROM favorites ORDER BY id").fetchall()
        favs = [dict(r) for r in rows]
        for fav in favs:
            fav.update(lookup_node(fav["node"]))
        return jsonify({"favorites": favs})
    except Exception as e:
        log("ERROR", f"[API] /api/favorites: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/add", methods=["POST"])
def api_fav_add():
    data  = request.json or {}
    node  = str(data.get("node",  "")).strip()
    label = str(data.get("label", "")).strip()
    if not node or not node.isdigit():
        return jsonify({"error": "Invalid node number"}), 400
    if not label:
        info  = lookup_node(node)
        label = info.get("callsign") or info.get("desc") or f"Node {node}"
    try:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO favorites (node, label) VALUES (?,?)", (node, label))
        db.commit()
        log("INFO", f"[API] Favorite added: node={node} label={label!r}")
        return jsonify({"success": True, "node": node, "label": label})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/delete", methods=["POST"])
def api_fav_delete():
    data = request.json or {}
    node = str(data.get("node", "")).strip()
    try:
        db = get_db()
        db.execute("DELETE FROM favorites WHERE node=?", (node,))
        db.commit()
        log("INFO", f"[API] Favorite deleted: node={node}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/label", methods=["POST"])
def api_fav_label():
    data  = request.json or {}
    node  = str(data.get("node",  "")).strip()
    label = str(data.get("label", "")).strip()
    try:
        db = get_db()
        db.execute("UPDATE favorites SET label=? WHERE node=?", (label, node))
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── AllStarLink stats proxy ───────────────────────────────────────────────────

@app.route("/api/nodestats/<node>")
def api_node_stats(node):
    if not node.isdigit():
        return jsonify({"error": "Invalid node"}), 400
    try:
        url = ASL_STATS_URL.format(node)
        log("DEBUG", f"[API] Fetching stats for node {node} from {url}")
        req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0"})
        with urlreq.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return jsonify(data)
    except Exception as e:
        log("WARN", f"[API] nodestats {node}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/nodestats/batch", methods=["POST"])
def api_nodestats_batch():
    data  = request.json or {}
    nodes = data.get("nodes", [])
    results = {}
    log("INFO", f"[API] nodestats/batch for {len(nodes)} nodes")
    for node in nodes[:15]:
        try:
            url = ASL_STATS_URL.format(node)
            req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0"})
            with urlreq.urlopen(req, timeout=6) as resp:
                results[node] = json.loads(resp.read().decode())
        except Exception as e:
            results[node] = {"error": str(e)}
        time.sleep(0.15)
    return jsonify(results)


# ── AMI node control API ──────────────────────────────────────────────────────

# ilink function numbers accepted from API callers (see AMIClient.rpt_cmd docstring).
# local_node/remote_node/mode are interpolated directly into the AMI Command: line,
# so every field must be validated before it reaches rpt_cmd() — an unvalidated
# field containing \r\n could smuggle extra AMI actions into the connection.
VALID_ILINK_MODES = {"1", "2", "3", "6", "12", "13"}


def _valid_node(node: str) -> bool:
    return bool(node) and node.isdigit()


@app.route("/api/ami/status")
def api_ami_status():
    """
    Returns cached node status. Sub-millisecond — reads from the background
    poller cache rather than opening a new AMI connection.
    Always returns JSON — never an HTML error page.
    """
    try:
        content = read_conf_file(RPT_CONF_PATH)
        nodes   = get_node_numbers(content) if content else []
        if not nodes:
            return jsonify({"error": "No nodes found in rpt.conf",
                            "hint": "Check rpt.conf path and permissions"}), 404
        node   = request.args.get("node", nodes[0])
        result = get_cached_status(node)
        if _ami_last_error and result.get("stale"):
            result["error"] = _ami_last_error
        log("DEBUG", f"[API] /api/ami/status node={node} age={result.get('age')}s stale={result.get('stale')}")
        return jsonify(result)
    except Exception as e:
        log("ERROR", f"[API] /api/ami/status exception: {e}")
        return jsonify({"error": str(e), "keyed": False, "connected": [], "stale": True}), 500


@app.route("/api/ami/connect", methods=["POST"])
def api_ami_connect():
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    mode        = str(data.get("mode", "3"))
    disc_first  = data.get("disconnect_first", False)

    if not _valid_node(local_node) or not _valid_node(remote_node):
        return jsonify({"error": "local_node and remote_node must be numeric"}), 400
    if mode not in VALID_ILINK_MODES:
        return jsonify({"error": f"Invalid mode {mode!r}. Must be one of {sorted(VALID_ILINK_MODES)}"}), 400

    log("INFO", f"[API] /api/ami/connect local={local_node} remote={remote_node} mode={mode} disc_first={disc_first}")

    def _do(ami):
        output = []
        if disc_first:
            log("INFO", f"[API] Disconnecting all first on node {local_node}")
            r = ami.rpt_cmd(local_node, "ilink 6")
            output.extend(r["output"])
            time.sleep(0.3)
        r = ami.rpt_cmd(local_node, f"ilink {mode} {remote_node}")
        output.extend(r["output"])
        return {
            "success": r["success"],
            "output":  output,
            "command": r["command"],
            "note":    "Empty output is normal for ilink commands — success means no error was returned",
        }

    try:
        return jsonify(ami_send_command(_do))
    except Exception as e:
        log("ERROR", f"[API] /api/ami/connect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ami/disconnect", methods=["POST"])
def api_ami_disconnect():
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()

    if not _valid_node(local_node):
        return jsonify({"error": "local_node must be numeric"}), 400
    if remote_node and not _valid_node(remote_node):
        return jsonify({"error": "remote_node must be numeric"}), 400

    log("INFO", f"[API] /api/ami/disconnect local={local_node} remote={remote_node or '(all)'}")

    def _do(ami):
        if remote_node:
            r = ami.rpt_cmd(local_node, f"ilink 1 {remote_node}")
        else:
            r = ami.rpt_cmd(local_node, "ilink 6")
        return {"success": r["success"], "output": r["output"], "command": r["command"]}

    try:
        return jsonify(ami_send_command(_do))
    except Exception as e:
        log("ERROR", f"[API] /api/ami/disconnect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ami/perm_connect", methods=["POST"])
def api_ami_perm_connect():
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    mode        = str(data.get("mode", "13"))

    if not _valid_node(local_node) or not _valid_node(remote_node):
        return jsonify({"error": "local_node and remote_node must be numeric"}), 400
    if mode not in VALID_ILINK_MODES:
        return jsonify({"error": f"Invalid mode {mode!r}. Must be one of {sorted(VALID_ILINK_MODES)}"}), 400

    log("INFO", f"[API] /api/ami/perm_connect local={local_node} remote={remote_node} mode={mode}")

    def _do(ami):
        r = ami.rpt_cmd(local_node, f"ilink {mode} {remote_node}")
        return {"success": r["success"], "output": r["output"], "command": r["command"]}

    try:
        return jsonify(ami_send_command(_do))
    except Exception as e:
        log("ERROR", f"[API] /api/ami/perm_connect error: {e}")
        return jsonify({"error": str(e)}), 500


# ── System info API ───────────────────────────────────────────────────────────

@app.route("/api/sysinfo")
def api_sysinfo():
    creds       = parse_manager_conf()
    ami_user    = creds.get("user") or "NOT CONFIGURED"
    ast_status  = get_asterisk_status()
    return jsonify({
        "cpu_temp":          get_cpu_temp(),
        "disk":              get_disk_usage(),
        "uptime":            get_uptime(),
        "asl_version":       get_asl_version(),
        "asterisk_active":   ast_status["active"],
        "asterisk_version":  ast_status["version"],
        "ami_user":          ami_user,
        "ami_host":          f"{AMI_HOST}:{AMI_PORT}",
        "ami_connected":     _ami_connected,
        "ami_poll_interval": POLL_INTERVAL,
        "running_as":        "root" if os.getuid() == 0 else f"uid={os.getuid()} (NOT ROOT - some features will fail)",
        "rpt_conf_path":     RPT_CONF_PATH,
        "rpt_conf_exists":   os.path.exists(RPT_CONF_PATH),
        "rpt_conf_writable": os.access(RPT_CONF_PATH, os.W_OK),
        "secret_key_is_default": SECRET_KEY in DEFAULT_SECRET_KEYS,
    })


# ── App settings (SECRET_KEY) ─────────────────────────────────────────────────
#
# SECRET_KEY ships with a well-known default ("asl3-ez-change-me-in-production")
# so the app works out of the box. It's not currently used to protect anything
# sensitive (no sessions/auth yet), but changing it is still good hygiene and
# will matter once auth is added. These routes let the user change it from the
# UI instead of hand-editing the systemd unit file.

@app.route("/api/settings/secret_key")
def api_get_secret_key():
    return jsonify({
        "is_default":         SECRET_KEY in DEFAULT_SECRET_KEYS,
        "service_file":       SERVICE_FILE_PATH,
        "service_file_exists": os.path.exists(SERVICE_FILE_PATH),
    })


@app.route("/api/settings/secret_key", methods=["POST"])
def api_set_secret_key():
    """
    Write a new SECRET_KEY into the systemd unit file's Environment line,
    then reload+restart the service so it takes effect. The response is
    sent before the restart is triggered (from a background thread) so the
    browser actually receives it before the worker process is replaced.
    """
    data    = request.json or {}
    new_key = str(data.get("secret_key", "")).strip()

    if new_key and len(new_key) < 16:
        return jsonify({"error": "Secret key must be at least 16 characters"}), 400
    if not new_key:
        new_key = secrets.token_hex(32)
        log("INFO", "[SETTINGS] Generated a new random SECRET_KEY")

    if not os.path.exists(SERVICE_FILE_PATH):
        return jsonify({"error": f"Service file not found: {SERVICE_FILE_PATH}",
                        "hint": "Set SERVICE_FILE_PATH if the unit file lives elsewhere."}), 404

    try:
        with open(SERVICE_FILE_PATH) as f:
            content = f.read()

        # Matches both quoted (Environment="SECRET_KEY=...") and unquoted
        # (Environment=SECRET_KEY=...) forms used across this project's files.
        pattern  = re.compile(r'^Environment="?SECRET_KEY=[^"\n]*"?[ \t]*$', re.MULTILINE)
        new_line = f'Environment="SECRET_KEY={new_key}"'
        if pattern.search(content):
            content = pattern.sub(new_line, content, count=1)
        else:
            content = re.sub(r'(\[Service\]\s*\n)', r'\1' + new_line + "\n", content, count=1)

        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(SERVICE_FILE_PATH), prefix=".asl3ez_svc_")
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, SERVICE_FILE_PATH)
        log("INFO", f"[SETTINGS] SECRET_KEY updated in {SERVICE_FILE_PATH}")

        subprocess.run([SYSTEMCTL_PATH, "daemon-reload"], capture_output=True, text=True, timeout=15)

        def _delayed_restart():
            time.sleep(1.0)
            log("INFO", f"[SETTINGS] Restarting {SERVICE_NAME} to apply new SECRET_KEY")
            subprocess.run([SYSTEMCTL_PATH, "restart", SERVICE_NAME], capture_output=True, text=True, timeout=30)

        threading.Thread(target=_delayed_restart, daemon=True).start()

        return jsonify({
            "success": True,
            "message": f"SECRET_KEY updated. Restarting {SERVICE_NAME} now — "
                       "the page will briefly disconnect, then reload it.",
        })
    except PermissionError as e:
        log("ERROR", f"[SETTINGS] Permission denied writing {SERVICE_FILE_PATH}: {e}")
        return jsonify({"error": str(e),
                        "hint": "Service must run as root to edit the systemd unit file."}), 403
    except Exception as e:
        log("ERROR", f"[SETTINGS] secret_key update failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/lookup/<node>")
def api_lookup(node):
    if not node.isdigit():
        return jsonify({"error": "Invalid node"}), 400
    info = lookup_node(node)
    source = "unknown"
    with _allmondb_lock:
        if node in _allmondb_cache:
            source = "allmondb_api"
    if source == "unknown" and node in _astdb_cache:
        source = "local_astdb.txt"
    if source == "unknown" and not info.get("callsign"):
        source = "not_found"
    log("INFO", f"[LOOKUP] node={node} source={source} callsign={info.get('callsign','')!r}")
    return jsonify({"node": node, "source": source, **info})


@app.route("/api/debug/nodedb")
def api_debug_nodedb():
    astdb_status = {}
    for path in ASTDB_PATHS:
        astdb_status[path] = os.path.exists(path)

    sample = {}
    with _allmondb_lock:
        keys = list(_allmondb_cache.keys())[:5]
        for k in keys:
            sample[k] = _allmondb_cache[k]

    return jsonify({
        "allmondb_loaded":  _allmondb_loaded,
        "allmondb_entries": len(_allmondb_cache),
        "astdb_loaded":     _astdb_loaded,
        "astdb_entries":    len(_astdb_cache),
        "astdb_paths":      astdb_status,
        "sample_entries":   sample,
        "allmondb_url":     ALLMONDB_URL,
    })


# ── AMI connectivity test ─────────────────────────────────────────────────────

@app.route("/api/ami/test")
def api_ami_test():
    log("INFO", "[API] /api/ami/test")
    creds = parse_manager_conf()
    result = {
        "ami_host":    f"{creds.get('host')}:{creds.get('port')}",
        "ami_user":    creds.get("user") or "NOT CONFIGURED",
        "creds_found": bool(creds.get("user") and creds.get("secret")),
        "connected":   False,
        "error":       None,
    }
    if not result["creds_found"]:
        result["error"] = (
            "AMI credentials not found in manager.conf. "
            "Run: sudo bash /opt/ASL3-EZ/ami-setup.sh"
        )
        return jsonify(result), 500

    try:
        subprocess.run([ASTERISK_PATH, "-rx", "module reload manager"],
                       capture_output=True, text=True, timeout=8)
        time.sleep(0.5)
        log("INFO", "[AMI-TEST] Reloaded manager module before test")
    except Exception as reload_err:
        log("WARN", f"[AMI-TEST] Could not reload manager module: {reload_err}")

    try:
        # Test against the pool connection if already up, else fresh
        with _ami_pool_lock:
            try:
                ami = _ami_ensure_connected()
                out = ami.command("core show version")
            except Exception:
                _ami_invalidate()
                raise
        result["connected"]     = True
        result["asterisk_info"] = out[0] if out else "connected"
        result["pool_active"]   = _ami_connected
        result["creds_source"]  = (
            "env vars"
            if os.environ.get("AMI_SECRET", "").strip().lower()
               not in {"yourpassword", "your_secret_here", "changeme", "amp111", ""}
            else "manager.conf"
        )
    except Exception as e:
        result["error"] = str(e)
        result["hint"]  = (
            "Run: sudo bash /opt/ASL3-EZ/ami-setup.sh\n"
            "This script reads manager.conf directly, tests the connection, "
            "and updates the service file automatically."
        )
        return jsonify(result), 500
    return jsonify(result)


# ── Raw AMI debug ─────────────────────────────────────────────────────────────

@app.route("/api/ami/raw_test")
def api_ami_raw_test():
    log("INFO", "[API] /api/ami/raw_test")
    creds = parse_manager_conf()
    transcript = []

    def note(s):
        transcript.append(s)
        log("DEBUG", f"[RAW-TEST] {s}")

    if not creds.get("user") or not creds.get("secret"):
        return jsonify({
            "error":      "Credentials not configured",
            "transcript": transcript,
            "fix":        "Set AMI_USER and AMI_SECRET in /etc/systemd/system/ASL3-EZ.service"
        }), 500

    host, port   = creds["host"], creds["port"]
    user, secret = creds["user"], creds["secret"]

    note(f"Connecting to {host}:{port}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8)
        s.connect((host, port))
        note("TCP connect: OK")
    except Exception as e:
        return jsonify({"error": str(e), "transcript": transcript}), 500

    buf      = b""
    deadline = time.time() + 4
    while time.time() < deadline:
        s.settimeout(0.5)
        try:
            chunk = s.recv(256)
            if chunk:
                buf += chunk
                if b"\r\n" in buf:
                    break
        except socket.timeout:
            continue

    banner = buf.decode("utf-8", errors="replace").strip()
    note(f"Banner: {banner!r}")

    login = (
        f"Action: Login\r\n"
        f"Username: {user}\r\n"
        f"Secret: {secret}\r\n"
        f"Events: off\r\n"
        f"\r\n"
    )
    note(f"Sending Login (user='{user}')")
    try:
        s.sendall(login.encode("utf-8"))
    except Exception as e:
        s.close()
        return jsonify({"error": f"Send failed: {e}", "transcript": transcript}), 500

    resp_buf = b""
    deadline = time.time() + 6
    s.settimeout(0.5)
    while time.time() < deadline:
        try:
            chunk = s.recv(1024)
            if chunk:
                resp_buf += chunk
                if b"\r\n\r\n" in resp_buf:
                    break
        except socket.timeout:
            continue

    resp_str = resp_buf.decode("utf-8", errors="replace")
    note(f"Raw response ({len(resp_buf)} bytes): {resp_str!r}")

    success, message = False, ""
    for line in resp_str.splitlines():
        line = line.strip()
        if line.startswith("Response:"):
            val = line.split(":", 1)[1].strip()
            note(f"Parsed Response: {val!r}")
            success = (val == "Success")
        elif line.startswith("Message:"):
            message = line.split(":", 1)[1].strip()
            note(f"Parsed Message: {message!r}")

    cmd_output = []
    if success:
        note("Auth OK — testing 'core show version'")
        try:
            s.sendall(b"Action: Command\r\nCommand: core show version\r\n\r\n")
            cmd_buf  = b""
            deadline = time.time() + 5
            s.settimeout(0.5)
            while time.time() < deadline:
                try:
                    chunk = s.recv(2048)
                    if chunk:
                        cmd_buf += chunk
                        if b"--END COMMAND--" in cmd_buf:
                            break
                except socket.timeout:
                    continue
            for line in cmd_buf.decode("utf-8", errors="replace").splitlines():
                if line.startswith("Output:"):
                    cmd_output.append(line[7:].strip())
            note(f"core show version: {cmd_output}")
        except Exception as e:
            note(f"Command error: {e}")

    try:
        s.sendall(b"Action: Logoff\r\n\r\n")
        s.close()
    except Exception:
        pass

    return jsonify({
        "success":      success,
        "banner":       banner,
        "response_raw": resp_str,
        "message":      message,
        "ami_user":     user,
        "ami_host":     f"{host}:{port}",
        "cmd_output":   cmd_output,
        "transcript":   transcript,
    })


# ---------------------------------------------------------------------------
# Startup — load node DB and start background AMI poller
# These run regardless of whether we're under gunicorn or direct python.
# ---------------------------------------------------------------------------
load_astdb()
start_poller()

if __name__ == "__main__":
    log("INFO", "Starting in direct-run mode (not via gunicorn)")
    app.run(host=HOST, port=PORT, debug=False)
