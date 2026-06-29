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
import fcntl
import sys
import pwd
import grp
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, Response, stream_with_context
import difflib
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import urllib.request as urlreq
    import urllib.parse as urlparse
except ImportError:
    import urllib2 as urlreq
    import urllib as urlparse

from collections import deque

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
SOUNDS_DIR      = os.environ.get("SOUNDS_DIR",       "/var/lib/asterisk/sounds/asl3ez")
SERVICE_FILE_PATH = os.environ.get("SERVICE_FILE_PATH",
                                    f"/etc/systemd/system/{SERVICE_NAME}.service")

# SECRET_KEY values that ship with the app/installer — used to warn the user
# in the dashboard that they're still on the default and should change it.
DEFAULT_SECRET_KEYS = {"", "asl3-ez-change-me", "asl3-ez-change-me-in-production"}

# Persistent AMI poller settings (tunable via service file env vars)
# 1s poll for near-real-time keyed-status updates in the UI. This used to
# be 3s, tuned around AMIClient.command()'s old 12s-per-call bug (waiting
# on a "--END COMMAND--" sentinel this AMI build never sends) — now that
# every AMI command completes in under a millisecond, polling this often
# costs nothing. 10s TTL still gives a generous buffer before flagging
# cached data as stale.
POLL_INTERVAL   = float(os.environ.get("AMI_POLL_INTERVAL", "1.0"))   # seconds between polls
CACHE_TTL       = float(os.environ.get("AMI_CACHE_TTL",     "10.0"))  # seconds before cache is stale

# Favorites keyed/connected-count polling hits the public AllStarLink stats
# API (external, shared infrastructure), unlike the AMI poller above which
# talks to the local Asterisk instance — so this interval is deliberately
# much longer. Clamped to a 5s floor regardless of env override so a typo
# can't turn this into a hammer against stats.allstarlink.org. Confirmed
# live that this API rate-limits (HTTP 429) and will outright refuse
# connections from an IP that exceeds it for a while afterward — the
# poller also backs off exponentially on failures (see _favstats_poll_loop).
FAVORITES_POLL_INTERVAL = max(5.0, float(os.environ.get("FAVORITES_POLL_INTERVAL", "30.0")))

# Log verbosity: DEBUG shows all messages; INFO (default) suppresses DEBUG noise.
# Set LOG_LEVEL=DEBUG in the service file Environment= lines for full verbose output.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}

# Asterisk log file path (used by the Asterisk Console log viewer)
ASTERISK_LOG_PATH = os.environ.get("ASTERISK_LOG_PATH", "/var/log/asterisk/messages.log")

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
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ---------------------------------------------------------------------------
# Logging  (verbose, timestamp-prefixed, written to stdout for journald)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()

def log(level, msg):
    if _LOG_LEVELS.get(level, 1) < _LOG_LEVELS.get(LOG_LEVEL, 1):
        return
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
log("INFO", f"  LOG_LEVEL      = {LOG_LEVEL}")


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
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        node    TEXT    NOT NULL,
        label   TEXT    DEFAULT '',
        added   TEXT    DEFAULT (datetime('now')),
        UNIQUE(user_id, node)
    )""")
    # Migrate old schema (no user_id) to per-user schema
    cols = [r[1] for r in conn.execute("PRAGMA table_info(favorites)").fetchall()]
    if 'user_id' not in cols:
        conn.execute("ALTER TABLE favorites RENAME TO _favorites_v1")
        conn.execute("""CREATE TABLE favorites (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            node    TEXT    NOT NULL,
            label   TEXT    DEFAULT '',
            added   TEXT    DEFAULT (datetime('now')),
            UNIQUE(user_id, node)
        )""")
        first = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first:
            conn.execute("""INSERT OR IGNORE INTO favorites (user_id, node, label, added)
                SELECT ?, node, label, added FROM _favorites_v1""", (first["id"],))
        conn.execute("DROP TABLE IF EXISTS _favorites_v1")
        conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS announcements (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        slug          TEXT    NOT NULL UNIQUE,
        node          TEXT    NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        interval_min  INTEGER NOT NULL DEFAULT 60,
        window_start  TEXT    NOT NULL DEFAULT '07:30',
        window_end    TEXT    NOT NULL DEFAULT '19:30',
        play_cmd      TEXT    NOT NULL DEFAULT 'localplay',
        last_played   TEXT,
        source_type   TEXT    NOT NULL DEFAULT 'upload',
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS connectors (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        local_node      TEXT    NOT NULL,
        target_node     TEXT    NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        connect_time    TEXT,
        idle_limit_sec  INTEGER NOT NULL DEFAULT 180,
        settle_sec      INTEGER NOT NULL DEFAULT 300,
        state           TEXT    NOT NULL DEFAULT 'idle',
        state_msg       TEXT    NOT NULL DEFAULT '',
        state_updated   TEXT,
        connected_at    TEXT,
        last_activity   TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS id_configs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        node            TEXT    NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        sound_path      TEXT    NOT NULL DEFAULT 'asl3ez/my-id',
        interval_sec    INTEGER NOT NULL DEFAULT 600,
        idle_delay_sec  INTEGER NOT NULL DEFAULT 120,
        initial_id      INTEGER NOT NULL DEFAULT 1,
        last_id_time    TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS connection_history (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        local_node        TEXT    NOT NULL,
        peer_node         TEXT    NOT NULL,
        peer_callsign     TEXT    DEFAULT '',
        peer_location     TEXT    DEFAULT '',
        direction         TEXT    DEFAULT '',
        connected_at      REAL    NOT NULL,
        disconnected_at   REAL,
        duration_seconds  REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_config (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        enabled            INTEGER NOT NULL DEFAULT 0,
        provider           TEXT    NOT NULL DEFAULT 'ntfy',
        ntfy_topic         TEXT    NOT NULL DEFAULT '',
        pushover_token     TEXT    NOT NULL DEFAULT '',
        pushover_user      TEXT    NOT NULL DEFAULT '',
        on_ami_disconnect  INTEGER NOT NULL DEFAULT 1,
        on_ami_reconnect   INTEGER NOT NULL DEFAULT 0,
        on_cpu_temp_high   INTEGER NOT NULL DEFAULT 1,
        cpu_temp_threshold INTEGER NOT NULL DEFAULT 80,
        on_node_connect    INTEGER NOT NULL DEFAULT 0,
        on_node_disconnect INTEGER NOT NULL DEFAULT 0,
        watch_nodes        TEXT    NOT NULL DEFAULT ''
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    UNIQUE NOT NULL,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'user',
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")
    # Migrate legacy single-user auth to users table (pre-roles era)
    legacy_user = conn.execute("SELECT value FROM settings WHERE key='auth_user'").fetchone()
    legacy_hash = conn.execute("SELECT value FROM settings WHERE key='auth_password_hash'").fetchone()
    if legacy_user and legacy_hash:
        existing_super = conn.execute("SELECT id FROM users WHERE role='superuser'").fetchone()
        if not existing_super:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?,?,?)",
                (legacy_user["value"], legacy_hash["value"], "superuser")
            )
            conn.execute("DELETE FROM settings WHERE key IN ('auth_user','auth_password_hash')")
            conn.commit()
    # Migrate two-tier roles (admin→superuser, kiosk→user) to three-tier system
    needs_role_migration = conn.execute(
        "SELECT 1 FROM settings WHERE key='roles_v3_migrated'"
    ).fetchone()
    if not needs_role_migration:
        conn.execute("UPDATE users SET role='superuser' WHERE role='admin'")
        conn.execute("UPDATE users SET role='user'      WHERE role='kiosk'")
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('roles_v3_migrated','1')")
        conn.commit()
    # Seed kiosk defaults
    for _k, _v in [
        ('kiosk_idle_timeout_sec', '600'),
        ('kiosk_clock_format',     '12'),
        ('kiosk_timezone',         'UTC'),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (_k, _v))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# App settings (DB-backed key/value store)
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    try:
        row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    db.commit()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def is_auth_configured():
    try:
        row = get_db().execute("SELECT id FROM users WHERE role='superuser'").fetchone()
        return row is not None
    except Exception:
        return False


@app.before_request
def check_auth():
    _PUBLIC          = {'login', 'logout', 'static', None,
                        'status_board', 'status_board_redirect',
                        'api_status_board', 'api_status_weather', 'api_status_activity',
                        'api_login', 'api_session',
                        'api_favorites', 'api_favorites_status',
                        'api_kiosk_settings_get'}
    # Any logged-in user (superuser / admin / user)
    _USER_OR_ABOVE = {'api_status_connect', 'api_status_disconnect',
                      'api_fav_add', 'api_fav_delete', 'api_fav_label'}

    endpoint = request.endpoint
    if endpoint in _PUBLIC:
        return None

    if not is_auth_configured():
        if request.path.startswith('/api/'):
            return jsonify({"error": "Setup required", "setup_url": "/login"}), 503
        return redirect(url_for('login'))

    logged_in = session.get('logged_in')
    role      = session.get('role', '')

    # Sessions created before role tracking was added won't have 'role'.
    # Look it up from the DB and patch the session so subsequent requests are fast.
    if logged_in and not role:
        username = session.get('username', '')
        if username:
            row = get_db().execute("SELECT role FROM users WHERE username=?",
                                   (username,)).fetchone()
            if row:
                role = row['role']
                session['role'] = role

    if endpoint in _USER_OR_ABOVE:
        if not logged_in:
            return jsonify({"error": "Authentication required"}), 401
        return None

    # Everything else requires admin or superuser
    if not logged_in:
        if request.path.startswith('/api/'):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for('login'))

    if role not in ('admin', 'superuser'):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Admin access required"}), 403
        return redirect(url_for('status_board'))

    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    setup_mode = not is_auth_configured()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if setup_mode:
            confirm = request.form.get("confirm_password", "")
            if not username:
                return render_template("login.html", setup_mode=True,
                                       error="Username is required.")
            if len(password) < 8:
                return render_template("login.html", setup_mode=True,
                                       error="Password must be at least 8 characters.")
            if password != confirm:
                return render_template("login.html", setup_mode=True,
                                       error="Passwords do not match.")
            db = get_db()
            db.execute("INSERT OR REPLACE INTO users (username, password_hash, role) VALUES (?,?,?)",
                       (username, generate_password_hash(password), "superuser"))
            db.commit()
            session["logged_in"] = True
            session["username"]  = username
            session["role"]      = "superuser"
            session["user_id"]   = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            log("INFO", f"[AUTH] Initial account created for '{username}'")
            return redirect(url_for('index'))

        # Normal login
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["logged_in"] = True
            session["username"]  = username
            session["role"]      = user["role"]
            session["user_id"]   = user["id"]
            log("INFO", f"[AUTH] Login: '{username}' (role={user['role']})")
            if user["role"] == "user":
                return redirect(url_for('status_board'))
            return redirect(url_for('index'))
        log("WARN", f"[AUTH] Failed login attempt for '{username}'")
        return render_template("login.html", setup_mode=False,
                               error="Invalid username or password.")

    # GET
    if not setup_mode and session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template("login.html", setup_mode=setup_mode, error=None)


@app.route("/logout")
def logout():
    username = session.get("username", "unknown")
    session.clear()
    log("INFO", f"[AUTH] Logout: '{username}'")
    return redirect(url_for('login'))


@app.route("/api/login", methods=["POST"])
def api_login():
    """JSON login for the Node Kiosk inline modal."""
    data     = request.json or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    user     = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        session["logged_in"] = True
        session["username"]  = username
        session["role"]      = user["role"]
        session["user_id"]   = user["id"]
        log("INFO", f"[AUTH] API Login: '{username}' (role={user['role']})")
        return jsonify({"ok": True, "role": user["role"], "username": username})
    log("WARN", f"[AUTH] API Login failed for '{username}'")
    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/session")
def api_session():
    """Public endpoint — returns current session state for the kiosk page."""
    if session.get("logged_in"):
        return jsonify({
            "logged_in": True,
            "username":  session.get("username", ""),
            "role":      session.get("role", ""),
        })
    return jsonify({"logged_in": False})


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
        Return keyed state, connected node list, and per-link keyed state
        for `node`.

        `keyed` reflects RPT_RXKEYED — the LOCAL radio receiver only. It
        does NOT go true when a linked node is talking and being repeated
        (that shows up as RPT_TXKEYED instead). Per-link keyed state comes
        from RPT_ALINKS, which app_rpt only populates for nodes currently
        linked to this one — 'rpt show variables <node>' returns "Unknown
        node number" for any node that isn't either local or currently
        connected, so this can't be queried for arbitrary remote nodes,
        only ones already in the connected list.

        RPT_ALINKS format (confirmed live): "<count>,<node><mode><K|U>,..."
        e.g. "2,2324RU,666380TK" — node 2324 in mode R(monitor), Unkeyed;
        node 666380 in mode T(transceive), Keyed.

        Connected nodes come from 'rpt lstats <node>' which gives one line
        per connected node containing the remote node number.
        """
        status = {"keyed": False, "connected": [], "links": {}, "raw": [], "lstats": [],
                  "link_connect_time": {}, "link_direction": {}, "link_connect_state": {}}

        # Primary: rpt show variables — RPT_RXKEYED for local keyed state,
        # RPT_ALINKS for per-link keyed state of already-connected nodes.
        lines = self.command(f"rpt show variables {node}")
        status["raw"] = lines
        log("DEBUG", f"[AMI] rpt show variables {node} -> {lines}")
        for line in lines:
            if re.search(r'\bRPT_RXKEYED\s*=\s*1\b', line, re.IGNORECASE):
                status["keyed"] = True
            m = re.search(r'\bRPT_ALINKS\s*=\s*\d+,(.+)$', line)
            if m:
                for entry in m.group(1).split(","):
                    em = re.match(r'^(\d{4,7})([A-Za-z]*)$', entry.strip())
                    if em:
                        link_node, flags = em.group(1), em.group(2)
                        status["links"][link_node] = {
                            "keyed": "K" in flags,
                            "mode":  flags[:-1] if flags.endswith(("K", "U")) else flags,
                        }

        # Secondary: rpt lstats — definitive connected node list + connect time
        # Format: NODE  PEER  RECONNECTS  DIRECTION  CONNECT_TIME  CONNECT_STATE
        # CONNECT_STATE values: ESTABLISHED (fully up), CONNECTING (handshake pending),
        # DISCONNECTING, etc.  We include all peers in "connected" so they show in the
        # UI, but expose the state so the UI can distinguish CONNECTING from ESTABLISHED.
        lstats = self.command(f"rpt lstats {node}")
        status["lstats"] = lstats
        log("DEBUG", f"[AMI] rpt lstats {node} -> {lstats}")
        for line in lstats:
            parts = line.split()
            if len(parts) >= 5 and re.match(r'^\d{4,7}$', parts[0]):
                cn = parts[0]
                if cn != str(node) and cn not in status["connected"]:
                    status["connected"].append(cn)
                # DIRECTION is the 4th column (index 3), "IN" or "OUT"
                status["link_direction"][cn] = parts[3]
                # CONNECT_TIME is the 5th column (index 4), format HH:MM:SS
                if re.match(r'^\d+:\d{2}:\d{2}$', parts[4]):
                    status["link_connect_time"][cn] = parts[4]
                # CONNECT_STATE is the 6th column (index 5)
                if len(parts) >= 6:
                    status["link_connect_state"][cn] = parts[5].upper()
            else:
                # Fallback: extract any node number from the line
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

            _alert_events = []  # (ev_type, local_node_str, peer_str) — processed outside lock

            with _ami_pool_lock:
                try:
                    ami = _ami_ensure_connected()
                    for node in nodes:
                        status = ami.get_node_status(node)
                        _ami_cache[node]    = status
                        _ami_cache_ts[node] = time.time()

                        # Track keyed transitions for activity feed + per-link stats
                        now_ts  = time.time()
                        own_key = f"own:{node}"
                        if status.get("keyed") and not _keyed_prev_states.get(own_key):
                            _record_keyed(node)
                        _keyed_prev_states[own_key] = bool(status.get("keyed"))

                        current_linked = set(status.get("connected", []))
                        with _link_stats_lock:
                            # Remove stats for nodes that are no longer connected
                            for gone in set(_link_stats) - current_linked:
                                del _link_stats[gone]
                            # Ensure entry exists for each connected node
                            for cn in current_linked:
                                if cn not in _link_stats:
                                    _link_stats[cn] = {"keyups": 0, "last_keyed": None}

                        for cn, lnk in status.get("links", {}).items():
                            lnk_key = f"lnk:{cn}"
                            now_keyed = bool(lnk.get("keyed"))
                            was_keyed = _keyed_prev_states.get(lnk_key, False)
                            if now_keyed and not was_keyed:
                                _record_keyed(cn)
                                with _link_stats_lock:
                                    if cn in _link_stats:
                                        _link_stats[cn]["keyups"]     += 1
                                        _link_stats[cn]["last_keyed"]  = now_ts
                            _keyed_prev_states[lnk_key] = now_keyed

                        # Connection history: only record fully-established links.
                        # Nodes in CONNECTING state are shown in the UI but do not
                        # create a history entry or trigger alerts until the handshake
                        # completes and CONNECT_STATE reaches ESTABLISHED.
                        node_str    = str(node)
                        conn_states = status.get("link_connect_state", {})
                        all_set     = set(status.get("connected", []))
                        # Default to ESTABLISHED when state field is absent (older ASL builds)
                        est_set     = {cn for cn in all_set
                                       if conn_states.get(cn, "ESTABLISHED").upper() == "ESTABLISHED"}
                        prev_est    = _prev_connected_map.get(node_str, set())
                        directions  = status.get("link_direction", {})
                        for peer in est_set - prev_est:
                            info = lookup_node(peer)
                            _db_conn_open(node_str, peer,
                                          info.get("callsign", ""),
                                          info.get("location", ""),
                                          directions.get(peer, ""))
                            _alert_events.append(("connect", node_str, peer))
                        for peer in prev_est - all_set:
                            # Was established before and has now fully gone
                            _db_conn_close(node_str, peer)
                            _alert_events.append(("disconnect", node_str, peer))
                            with _kiosk_temp_lock:
                                _kiosk_temp_conns.pop((node_str, peer), None)
                        _prev_connected_map[node_str] = est_set

                        # Kiosk idle-timeout: update last_active when local or peer is keyed
                        local_keyed = bool(status.get("keyed"))
                        with _kiosk_temp_lock:
                            for (ln, pn), info in list(_kiosk_temp_conns.items()):
                                if ln != node_str or info.get('permanent'):
                                    continue
                                peer_keyed = bool(status.get("links", {}).get(pn, {}).get("keyed"))
                                if local_keyed or peer_keyed:
                                    info['last_active'] = now_ts

                        # Fire idle disconnects (outside _kiosk_temp_lock to avoid deadlock)
                        idle_timeout = int(get_setting('kiosk_idle_timeout_sec', '600') or 600)
                        _idle_dc = []
                        with _kiosk_temp_lock:
                            for (ln, pn), info in list(_kiosk_temp_conns.items()):
                                if ln != node_str or info.get('permanent'):
                                    continue
                                if now_ts - info.get('last_active', now_ts) > idle_timeout:
                                    _idle_dc.append((ln, pn))
                                    del _kiosk_temp_conns[(ln, pn)]
                        for (ln, pn) in _idle_dc:
                            try:
                                ami.rpt_cmd(ln, f"ilink 1 {pn}")
                                log("INFO", f"[KIOSK] Idle timeout: disconnected {pn} from {ln}")
                            except Exception as _e:
                                log("ERROR", f"[KIOSK] Idle timeout disconnect failed: {_e}")

                except Exception as e:
                    _ami_last_error = str(e)
                    log("ERROR", f"[AMI-POLL] Error during poll: {e}")
                    _ami_invalidate()

            # Process node connect/disconnect alerts outside lock (may do network I/O)
            for ev_type, local, peer in _alert_events:
                try:
                    cfg = _get_alert_config()
                    if cfg and cfg["enabled"]:
                        watches = [w.strip() for w in cfg["watch_nodes"].split(",") if w.strip()]
                        if ev_type == "connect" and cfg["on_node_connect"]:
                            if not watches or peer in watches or local in watches:
                                _send_alert("ASL3-EZ: Node Connected",
                                            f"Node {peer} connected to {local}")
                        elif ev_type == "disconnect" and cfg["on_node_disconnect"]:
                            if not watches or peer in watches or local in watches:
                                _send_alert("ASL3-EZ: Node Disconnected",
                                            f"Node {peer} disconnected from {local}")
                except Exception:
                    pass
            # AMI state + CPU temp alerts — outside lock, OK to do network I/O here
            _check_alerts(_ami_connected, get_cpu_temp())

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


# ---------------------------------------------------------------------------
# Favorites keyed/connected-count polling — public AllStarLink stats API
#
# Favorites are arbitrary node numbers, not necessarily linked to this
# node, so their keyed/connected state can't come from local AMI (app_rpt
# only knows about nodes it's currently connected to). This polls
# stats.allstarlink.org instead, on its own background thread, caching
# results so any number of browser tabs/users share one set of outbound
# requests rather than each polling the external API independently.
# ---------------------------------------------------------------------------
_favstats_cache    = {}   # {node: {"keyed": bool, "connected_count": int, "error": str|None}}
_favstats_cache_ts = {}   # {node: float unix timestamp}
_favstats_lock     = threading.Lock()


def _fetch_node_stats(node: str) -> dict:
    """
    urlopen(timeout=...) does not reliably bound DNS resolution time — a
    stalled resolver can block past that timeout indefinitely (confirmed
    live: the favstats poller thread sat blocked for 50+ minutes despite a
    240s backoff sleep already having elapsed, with no exception raised).
    socket.setdefaulttimeout() does cover the resolution phase too, so it's
    set around just this call as a hard backstop. AMIClient's sockets are
    unaffected — they always call settimeout() explicitly immediately after
    creation, overriding whatever the default was at that instant.
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    try:
        url = ASL_STATS_URL.format(node)
        req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0"})
        with urlreq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
    finally:
        socket.setdefaulttimeout(old_timeout)
    stats  = (data.get("stats") or {}).get("data") or {}
    linked = stats.get("linkedNodes") or []
    return {"keyed": bool(stats.get("keyed", False)), "connected_count": len(linked), "error": None}


_favstats_backoff_level = 0  # consecutive bad cycles; resets to 0 on any clean success
_FAVSTATS_MAX_SLEEP     = 600.0  # cap backoff at 10 minutes between cycles


def _favstats_poll_loop():
    global _favstats_backoff_level
    log("INFO", f"[FAVSTATS-POLL] Background poller started (interval={FAVORITES_POLL_INTERVAL}s)")
    while True:
        any_success = False
        any_429     = False
        try:
            db    = get_db()
            nodes = [r["node"] for r in db.execute("SELECT DISTINCT node FROM favorites").fetchall()]
            for node in nodes:
                try:
                    result = _fetch_node_stats(node)
                    any_success = True
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str:
                        any_429 = True
                    log("WARN", f"[FAVSTATS-POLL] {node}: {e}")
                    result = {"keyed": False, "connected_count": 0, "error": err_str}
                with _favstats_lock:
                    _favstats_cache[node]    = result
                    _favstats_cache_ts[node] = time.time()
                # Pacing gap between nodes within a cycle, on top of the
                # interval between whole cycles. Confirmed live that a tight
                # burst (originally 0.3s apart) across just 6 nodes was
                # enough to trigger 429s and then outright connection
                # refusal, even though the aggregate rate (a handful of
                # requests per 30s+) was low — an isolated single request
                # succeeded immediately after a burst got blocked. The
                # burst itself looks like abuse, not the average rate.
                time.sleep(2.0)
        except Exception as outer:
            log("ERROR", f"[FAVSTATS-POLL] Unexpected outer error: {outer}")

        # Confirmed live that this API both rate-limits (429) and will then
        # refuse connections outright from an offending IP for a while.
        # Back off exponentially whenever a cycle hits a 429 or fails
        # entirely, instead of continuing to hammer it on the fixed
        # interval — and reset to normal the moment a cycle succeeds.
        if any_429 or (nodes and not any_success):
            _favstats_backoff_level = min(_favstats_backoff_level + 1, 6)
        else:
            _favstats_backoff_level = 0

        sleep_time = min(FAVORITES_POLL_INTERVAL * (2 ** _favstats_backoff_level), _FAVSTATS_MAX_SLEEP)
        if _favstats_backoff_level:
            log("WARN", f"[FAVSTATS-POLL] backing off — next poll in {sleep_time:.0f}s (level {_favstats_backoff_level})")
        time.sleep(sleep_time)


def start_favstats_poller():
    t = threading.Thread(target=_favstats_poll_loop, name="favstats-poller", daemon=True)
    t.start()
    log("INFO", "[FAVSTATS-POLL] Poller thread launched")


# ── Keyed history (for Status Board activity feed) ────────────────────────────
_keyed_history      = deque(maxlen=500)   # enough for 60-min window at high activity
_keyed_history_lock = threading.Lock()
_keyed_prev_states  = {}   # {key: bool}  — track transitions per node/link

# ── Per-link live stats (keyup count + last keyed time) ───────────────────────
_link_stats      = {}   # {node_str: {"keyups": int, "last_keyed": float|None}}
_link_stats_lock = threading.Lock()

# ── Connection history tracking ────────────────────────────────────────────────
_prev_connected_map = {}  # {local_node_str: set(peer_str)}

# ── Kiosk idle-timeout tracking ────────────────────────────────────────────────
# key: (local_node_str, peer_str)
# val: {'permanent': bool, 'last_active': float (epoch)}
_kiosk_temp_conns = {}
_kiosk_temp_lock  = threading.Lock()

# ── Alert state ───────────────────────────────────────────────────────────────
_alert_prev_ami    = None   # None=unknown, True=was connected, False=was disconnected
_alert_cpu_alerted = False


def _record_keyed(node: str):
    """Prepend a node to the keyed history (dedup consecutive same-node entries)."""
    info  = lookup_node(node)
    entry = {
        "node":     node,
        "callsign": info.get("callsign", ""),
        "location": info.get("location", ""),
        "ts":       time.time(),
        "lat":      None,
        "lon":      None,
    }
    with _keyed_history_lock:
        if _keyed_history and _keyed_history[0]["node"] == node:
            _keyed_history[0]["ts"] = entry["ts"]
        else:
            _keyed_history.appendleft(entry)


# ── Connection history DB helpers ──────────────────────────────────────────────

def _db_conn_open(local_node, peer, callsign, location, direction):
    """Insert a connection record only if none is already open for this pair."""
    try:
        db = get_db()
        existing = db.execute(
            "SELECT id FROM connection_history "
            "WHERE local_node=? AND peer_node=? AND disconnected_at IS NULL",
            (local_node, peer)
        ).fetchone()
        if existing:
            return
        db.execute(
            "INSERT INTO connection_history "
            "(local_node, peer_node, peer_callsign, peer_location, direction, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (local_node, peer, callsign, location, direction, time.time())
        )
        db.commit()
        log("DEBUG", f"[CONNHIST] Opened: {local_node} <-> {peer} ({direction})")
    except Exception as e:
        log("ERROR", f"[CONNHIST] _db_conn_open error: {e}")


def _db_conn_close(local_node, peer):
    """Close the most recent open record for this pair with duration."""
    try:
        db  = get_db()
        now = time.time()
        row = db.execute(
            "SELECT id, connected_at FROM connection_history "
            "WHERE local_node=? AND peer_node=? AND disconnected_at IS NULL "
            "ORDER BY connected_at DESC LIMIT 1",
            (local_node, peer)
        ).fetchone()
        if not row:
            return
        duration = now - row["connected_at"]
        db.execute(
            "UPDATE connection_history SET disconnected_at=?, duration_seconds=? WHERE id=?",
            (now, duration, row["id"])
        )
        db.commit()
        log("DEBUG", f"[CONNHIST] Closed: {local_node} <-> {peer} (duration={duration:.0f}s)")
    except Exception as e:
        log("ERROR", f"[CONNHIST] _db_conn_close error: {e}")


def _db_conn_startup_cleanup():
    """Close any records left open from a previous run."""
    try:
        db   = get_db()
        now  = time.time()
        rows = db.execute(
            "SELECT id, connected_at FROM connection_history WHERE disconnected_at IS NULL"
        ).fetchall()
        for row in rows:
            duration = now - row["connected_at"]
            db.execute(
                "UPDATE connection_history SET disconnected_at=?, duration_seconds=? WHERE id=?",
                (now, duration, row["id"])
            )
        db.commit()
        if rows:
            log("INFO", f"[CONNHIST] Startup cleanup: closed {len(rows)} open record(s)")
    except Exception as e:
        log("ERROR", f"[CONNHIST] _db_conn_startup_cleanup error: {e}")


# ── Alert helpers ──────────────────────────────────────────────────────────────

def _get_alert_config():
    """Return alert_config row as dict, or None if not configured."""
    try:
        db  = get_db()
        row = db.execute("SELECT * FROM alert_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception as e:
        log("ERROR", f"[ALERTS] _get_alert_config error: {e}")
        return None


def _send_alert(title, message, priority="default"):
    """Dispatch a push notification via ntfy or Pushover."""
    try:
        cfg = _get_alert_config()
        if not cfg or not cfg["enabled"]:
            return
        if cfg["provider"] == "ntfy":
            if not cfg["ntfy_topic"]:
                return
            url     = f"https://ntfy.sh/{cfg['ntfy_topic']}"
            headers = {"Title": title}
            if priority == "high":
                headers["Priority"] = "high"
            req = urlreq.Request(url, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
            with urlreq.urlopen(req, timeout=8) as resp:
                log("INFO", f"[ALERTS] ntfy sent: {title!r} -> HTTP {resp.status}")
        elif cfg["provider"] == "pushover":
            if not cfg["pushover_token"] or not cfg["pushover_user"]:
                return
            body = urlparse.urlencode({
                "token":    cfg["pushover_token"],
                "user":     cfg["pushover_user"],
                "title":    title,
                "message":  message,
                "priority": 1 if priority == "high" else 0,
            }).encode("utf-8")
            req = urlreq.Request(
                "https://api.pushover.net/1/messages.json",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST"
            )
            with urlreq.urlopen(req, timeout=8) as resp:
                log("INFO", f"[ALERTS] pushover sent: {title!r} -> HTTP {resp.status}")
    except Exception as e:
        log("ERROR", f"[ALERTS] _send_alert error: {e}")


def _check_alerts(ami_ok, cpu_temp):
    """Check AMI state and CPU temp conditions, send alerts when thresholds cross."""
    global _alert_prev_ami, _alert_cpu_alerted
    cfg = _get_alert_config()
    if not cfg or not cfg["enabled"]:
        _alert_prev_ami = ami_ok
        return
    # AMI disconnect
    if cfg["on_ami_disconnect"] and _alert_prev_ami is True and not ami_ok:
        _send_alert("ASL3-EZ: AMI Offline", "Connection to Asterisk lost", "high")
    # AMI reconnect
    if cfg["on_ami_reconnect"] and _alert_prev_ami is False and ami_ok:
        _send_alert("ASL3-EZ: AMI Reconnected", "Connection to Asterisk restored", "default")
    _alert_prev_ami = ami_ok
    # CPU temp — alert once when threshold is crossed, reset when it drops back
    if cfg["on_cpu_temp_high"] and cpu_temp is not None:
        thr = cfg["cpu_temp_threshold"]
        if cpu_temp > thr and not _alert_cpu_alerted:
            _send_alert("ASL3-EZ: High CPU Temp",
                        f"CPU is {cpu_temp}C (threshold {thr}C)", "high")
            _alert_cpu_alerted = True
        elif cpu_temp <= thr:
            _alert_cpu_alerted = False


# ── Nominatim geocoding cache ─────────────────────────────────────────────────
NOMINATIM_URL   = "https://nominatim.openstreetmap.org/search?q={}&format=json&limit=1"
_geocode_cache  = {}         # {location_str: {"lat": float, "lon": float} | None}
_geocode_lock   = threading.Lock()
_geocode_last   = [0.0]      # time of last Nominatim request (rate-limit: 1 req/s)
_geocode_rlock  = threading.Lock()


def _geocode(location: str):
    """Return {"lat": float, "lon": float} for a location string, or None. Cached forever."""
    if not location or not location.strip():
        return None
    loc = location.strip()
    with _geocode_lock:
        if loc in _geocode_cache:
            return _geocode_cache[loc]

    # Nominatim: at most 1 request per 1.1 seconds
    with _geocode_rlock:
        wait = 1.1 - (time.time() - _geocode_last[0])
        if wait > 0:
            time.sleep(wait)
        _geocode_last[0] = time.time()
        try:
            url = NOMINATIM_URL.format(urlparse.quote_plus(loc))
            req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0 (ham radio node manager)"})
            with urlreq.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            result = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])} if results else None
        except Exception as e:
            log("WARN", f"[GEOCODE] '{loc}': {e}")
            result = None

    with _geocode_lock:
        _geocode_cache[loc] = result
    return result


# ── Global ASL activity (nodes connected to major public hubs) ────────────────
# Instead of the 9.7 MB full-node-list endpoint (which triggers rate limits),
# we poll a small curated list of major public hub nodes. Each request is ~3 KB.
# From each hub's linkedNodes list we extract nodes with server lat/lon.
# Polled every 5 minutes; 1-second gap between hub requests.
ASL_HUB_STATS_URL     = "https://stats.allstarlink.org/api/stats/{}"
GLOBAL_ACTIVITY_INTERVAL = 300.0   # 5 minutes between full hub sweeps

# Well-known public hubs with confirmed active linkedNodes populations.
# The poller gracefully skips any that return no data.
_ASL_HUBS = [27339, 41522, 2000, 55143, 3109050, 9050, 436000, 460220]

_global_nodes_cache = []   # list[dict] — up to 10 nodes from linked hub nodes
_global_nodes_ts    = 0.0
_global_nodes_lock  = threading.Lock()


def _fetch_hub_linked_nodes():
    """
    Query each hub in _ASL_HUBS for its linkedNodes list.
    Returns up to 10 nodes sorted by most-recently registered, with lat/lon
    taken directly from the server{} sub-object — no geocoding required.
    """
    seen  = set()
    nodes = []
    for hub in _ASL_HUBS:
        try:
            url = ASL_HUB_STATS_URL.format(hub)
            req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0 (hub monitor)"})
            with urlreq.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            linked = (d.get("stats") or {}).get("data") or {}
            linked = linked.get("linkedNodes") or []
            for n in linked:
                name = str(n.get("name", ""))
                if not name or name in seen:
                    continue
                seen.add(name)
                srv = n.get("server") or {}
                lat = lon = None
                try:
                    if srv.get("Latitude") and srv.get("Logitude"):
                        lat = float(srv["Latitude"])
                        lon = float(srv["Logitude"])  # AllStarLink API typo
                except (ValueError, TypeError):
                    pass
                nodes.append({
                    "node":     name,
                    "callsign": n.get("callsign", ""),
                    "location": srv.get("Location", "") or n.get("node_frequency", ""),
                    "lat":      lat,
                    "lon":      lon,
                    "ts":       n.get("regseconds", 0),
                })
        except Exception as e:
            log("DEBUG", f"[GLOBAL-ACTIVITY] Hub {hub}: {e}")
        time.sleep(1.0)   # 1 s gap between hub requests

    nodes.sort(key=lambda x: x["ts"], reverse=True)
    return nodes[:10]


def _global_activity_poll_loop():
    global _global_nodes_ts
    log("INFO", "[GLOBAL-ACTIVITY] Poller started — first fetch in 30s")
    time.sleep(30)   # short startup delay
    backoff = 0
    while True:
        try:
            nodes = _fetch_hub_linked_nodes()
            with _global_nodes_lock:
                _global_nodes_cache.clear()
                _global_nodes_cache.extend(nodes)
                _global_nodes_ts = time.time()
            log("INFO", f"[GLOBAL-ACTIVITY] Fetched {len(nodes)} nodes from hub sweep")
            backoff = 0
            time.sleep(GLOBAL_ACTIVITY_INTERVAL)
        except Exception as e:
            backoff = min(backoff + 1, 5)
            sleep_s = GLOBAL_ACTIVITY_INTERVAL * (2 ** (backoff - 1))
            log("WARN", f"[GLOBAL-ACTIVITY] Loop error ({e}) — retry in {sleep_s:.0f}s")
            time.sleep(sleep_s)


def start_global_activity_poller():
    t = threading.Thread(target=_global_activity_poll_loop, name="global-activity", daemon=True)
    t.start()
    log("INFO", "[GLOBAL-ACTIVITY] Poller thread launched")


# ── Weather cache (wttr.in) ───────────────────────────────────────────────────
WTTR_URL         = "https://wttr.in/{}?format=j1"
WEATHER_INTERVAL = 600.0   # 10 minutes per location

_weather_cache = {}   # {location: {"data": dict, "ts": float}}
_weather_lock  = threading.Lock()


def _fetch_weather(location: str) -> dict:
    """Return current weather for a location from wttr.in. Cached 10 minutes."""
    if not location or not location.strip():
        return {"error": "No location configured for this node"}
    loc = location.strip()

    with _weather_lock:
        entry = _weather_cache.get(loc)
        if entry and (time.time() - entry["ts"]) < WEATHER_INTERVAL:
            return entry["data"]

    try:
        url = WTTR_URL.format(urlparse.quote_plus(loc))
        req = urlreq.Request(url, headers={"User-Agent": "ASL3-EZ/1.0"})
        with urlreq.urlopen(req, timeout=12) as resp:
            raw = json.loads(resp.read())
        cc   = (raw.get("current_condition") or [{}])[0]
        data = {
            "location": loc,
            "temp_f":   cc.get("temp_F", ""),
            "temp_c":   cc.get("temp_C", ""),
            "desc":     ((cc.get("weatherDesc") or [{}])[0]).get("value", ""),
            "humidity": cc.get("humidity", ""),
            "wind_mph": cc.get("windspeedMiles", ""),
            "wind_dir": cc.get("winddir16Point", ""),
            "error":    None,
        }
    except Exception as e:
        data = {"error": str(e), "location": loc}

    with _weather_lock:
        _weather_cache[loc] = {"data": data, "ts": time.time()}
    return data


def get_cached_favstats(node: str) -> dict:
    node   = str(node)
    with _favstats_lock:
        entry = _favstats_cache.get(node)
        ts    = _favstats_cache_ts.get(node, 0)
    age = time.time() - ts
    if entry is None:
        return {"node": node, "keyed": False, "connected_count": 0, "stale": True, "age": None}
    return {**entry, "node": node, "stale": age > FAVORITES_POLL_INTERVAL * 3, "age": round(age, 2)}


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
            #
            # Mode is 644 (world-readable), not 640, because other local
            # tools commonly installed alongside ASL3 (AllScan, Supermon,
            # etc.) read rpt.conf directly as their own web server user
            # (e.g. www-data), which typically isn't in the asterisk group.
            # rpt.conf doesn't hold secrets itself (those live in
            # manager.conf, which stays 640), so the broader read access
            # here is a reasonable tradeoff for that compatibility.
            if _have_asterisk_ids:
                try:
                    os.chown(path, uid, gid)
                    os.chmod(path, 0o644)
                    log("INFO", f"[CONF] Restored {path} owner=asterisk:asterisk mode=644")
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


def _collect_stanzas(content):
    """
    Pass 1 of stanza parsing, shared by parse_stanza_settings() and the
    template-discovery helpers below.

    Returns { name: {"is_template": bool, "template": str|None, "lines": [...]} }
    "template" is the raw, un-split header field — may be a single name or a
    comma-separated list (Asterisk supports multiple inheritance).
    """
    stanzas = {}
    current = None
    for line in content.splitlines():
        s = line.strip()
        # A commented-out stanza header (e.g. ";[daq-cham-1]", from example
        # config shown disabled by default) must still count as a stanza
        # boundary, even though it and everything under it is inactive.
        # Otherwise its entirely-commented example content (device=,
        # hwtype=, tag=, etc.) gets misattributed to whatever real stanza
        # came before it — confirmed live: rpt.conf's disabled [daq-cham-1]/
        # [meter-faces]/[alarms] example blocks were bleeding their example
        # keys into [macro]'s entries, since none of those headers were
        # recognized as boundaries.
        header_candidate = s[1:].strip() if s.startswith(";") else s
        hdr = re.match(r'^\[([^\]]+)\](?:\(([^)]+)\))?', header_candidate)
        if hdr:
            raw_name = hdr.group(1).strip()
            raw_tmpl = (hdr.group(2) or "").strip()
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
    return stanzas


def _parse_kv_lines(lines_list):
    """Parse key=value pairs from a list of raw stanza lines."""
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
        m = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9_]*)\s*=\s*([^;]*?)(?:\s*;.*)?$', stripped)
        if m:
            k = m.group(1).strip()
            v = m.group(2).strip()
            result[k] = {"value": v, "commented": commented, "raw_line": line}
    return result


def get_template_names(content):
    """Names of all template-definition stanzas, i.e. [name](!)."""
    stanzas = _collect_stanzas(content)
    return [name for name, s in stanzas.items() if s["is_template"]]


def get_node_template_usage(content):
    """
    Map of template name -> sorted list of node numbers that use it,
    restricted to templates actually referenced by a node-number stanza
    (so functions/telemetry/morse/etc. templates used only by non-node
    stanzas don't show up as "node templates"). Only templates that
    actually exist as a [name](!) definition are included — a node
    referencing a missing template (e.g. allscan-uci in the sample
    config) just doesn't contribute it here, same as Asterisk has
    nothing to inherit from it either.
    """
    stanzas   = _collect_stanzas(content)
    templates = set(get_template_names(content))
    usage     = {}
    for node in get_node_numbers(content):
        stanza = stanzas.get(node)
        if not stanza or not stanza["template"]:
            continue
        for tmpl_name in (t.strip() for t in stanza["template"].split(",")):
            if tmpl_name in templates:
                usage.setdefault(tmpl_name, []).append(node)
    for node_list in usage.values():
        node_list.sort()
    return usage


def get_referenced_stanza_usage(content, setting_key, default_name):
    """
    For each node, resolve which stanza it points to for a given
    name-override setting (e.g. setting_key='macro' -> the stanza holding
    its DTMF macros, defaulting to 'macro' if the node/template chain
    never sets one — that default matches app_rpt's own behavior).
    Restricted to stanza names that actually exist in the file.
    Returns {stanza_name: [node_numbers...]}.
    """
    stanzas = _collect_stanzas(content)
    usage   = {}
    for node in get_node_numbers(content):
        settings = parse_stanza_settings(content, node)
        name = (settings.get(setting_key) or {}).get("value") or default_name
        if name in stanzas:
            usage.setdefault(name, []).append(node)
    for node_list in usage.values():
        node_list.sort()
    return usage


def get_macro_stanza_usage(content):
    return get_referenced_stanza_usage(content, "macro", "macro")


def get_schedule_stanza_usage(content):
    return get_referenced_stanza_usage(content, "scheduler", "schedule")


def parse_stanza_settings(content, stanza_name):
    """
    Parse key=value pairs from a specific named stanza in rpt.conf.

    ASL3 uses Asterisk config templates — a node stanza like [64393](node-main)
    inherits all settings from [node-main](!) but can override them.
    This function returns the *effective* settings for the requested stanza by:
      1. Collecting settings from the template stanza(s) it inherits from
      2. Overlaying settings from the named stanza itself (overrides win)

    This matches how Asterisk actually reads the file and fixes the bug where
    the old flat parser would return the wrong value when the same key appeared
    in multiple stanzas (e.g. duplex=3 in [node-main] but duplex=0 somewhere else).

    Returns dict: { key: {"value": str, "commented": bool, "raw_line": str,
                           "source": "own" | <template name>} }
    "source" lets callers (and the UI) distinguish a node's own setting from
    one it only has because a shared template provides it.
    """
    stanzas = _collect_stanzas(content)

    target = stanzas.get(stanza_name)
    if target is None:
        log("WARN", f"[CONF] Stanza [{stanza_name}] not found in rpt.conf")
        return {}

    # Start with template settings if this stanza inherits one.
    # Asterisk templates support multiple comma-separated parents, e.g.
    # [643930](node-main,allscan-uci) — applied left to right, so a later
    # template's keys override an earlier one's. A single un-split lookup
    # here ("node-main,allscan-uci" as one literal name) never matches any
    # real stanza, so multi-template stanzas silently inherited nothing at
    # all — this is what was hiding [node-main]'s settings for nodes using
    # more than one template. Templates that don't actually exist in the
    # file (e.g. allscan-uci, referenced but never defined) are skipped —
    # Asterisk does the same: nothing to inherit from a missing template.
    effective = {}
    tmpl_field = target.get("template") or ""
    for tmpl_name in (t.strip() for t in tmpl_field.split(",")):
        if not tmpl_name:
            continue
        if tmpl_name not in stanzas:
            log("WARN", f"[CONF] [{stanza_name}] references template [{tmpl_name}] which doesn't exist in rpt.conf — skipping")
            continue
        tmpl_settings = _parse_kv_lines(stanzas[tmpl_name]["lines"])
        for k, v in tmpl_settings.items():
            v["source"] = tmpl_name
        effective.update(tmpl_settings)
        log("DEBUG", f"[CONF] [{stanza_name}] inherits from [{tmpl_name}]: {len(tmpl_settings)} settings")

    # Overlay with the stanza's own settings (these override the template)
    own = _parse_kv_lines(target["lines"])
    for k, v in own.items():
        v["source"] = "own"
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
        m = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9_]*)\s*=\s*([^;]*?)(?:\s*;.*)?$', stripped)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            settings[k] = {"value": v, "commented": commented, "raw_line": line}
    return settings


def _section_header_match(s):
    """
    Match a section header, real or commented-out. Mirrors the same fix in
    _collect_stanzas(): a commented header like ";[daq-cham-1]" must still
    count as a boundary, or everything under it (itself all comments, but
    documentation/examples for a different, inactive stanza) gets treated
    as still being inside whatever real section preceded it. Confirmed
    live: saving a new key into [macro] silently overwrote an unrelated
    commented DAQ pin example (";10 = inp" under [daq-cham-1]) because
    this function didn't recognize ";[daq-cham-1]" as leaving [macro].
    """
    header_candidate = s[1:].strip() if s.startswith(";") else s
    return re.match(r'^\[([^\]\(]+)', header_candidate)


def update_setting_in_content(content, section, key, value, enable=True):
    """Update or insert a key=value in the given section of the config."""
    lines   = content.splitlines(keepends=True)
    result  = []
    in_sec  = False
    found   = False

    for line in lines:
        s = line.strip()
        sec_m = _section_header_match(s)
        if sec_m:
            in_sec = (sec_m.group(1).strip() == section)

        if in_sec and not found:
            test = s.lstrip(";").strip()
            km   = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9_]*)\s*=', test)
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
            sec_m = _section_header_match(s)
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
# Macro / schedule entry validation
#
# Per https://allstarlink.github.io/config/rpt_conf/ :
#   [macro]    1 = *32000*32001     ; key = macro slot number, value = DTMF
#                                   ; command sequence(s), space-separated,
#                                   ; "p"/"P" for a ~500ms pause
#   [schedule] 2 = 00 00 * * *      ; key = macro slot to run, value = cron-
#                                   ; style "m h dom mon dow", star implied.
# Only plain numbers and "*" are documented for schedule fields — no
# confirmed support for ranges/lists/steps, so those are rejected here
# rather than silently accepted and possibly mis-parsed by app_rpt.
# ---------------------------------------------------------------------------
MACRO_KEY_RE         = re.compile(r'^\d+$')
MACRO_VALUE_RE       = re.compile(r'^[0-9*#A-Da-dPp\s]*$')
SCHEDULE_FIELD_RE    = re.compile(r'^(\*|\d{1,2})$')


def validate_macro_entry(key, value):
    if not MACRO_KEY_RE.match(key):
        return f"macro slot {key!r} must be a number"
    if value and not MACRO_VALUE_RE.match(value):
        return f"macro command {value!r} contains invalid characters (allowed: 0-9 * # A-D p, spaces)"
    return None


def validate_schedule_entry(key, value):
    if not MACRO_KEY_RE.match(key):
        return f"schedule key {key!r} must be a number (the macro slot to run)"
    if value:
        fields = value.split()
        if len(fields) != 5:
            return f"schedule value {value!r} must have exactly 5 fields: minute hour day-of-month month day-of-week"
        for f in fields:
            if not SCHEDULE_FIELD_RE.match(f):
                return f"schedule field {f!r} is invalid — only a number or * is supported"
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
@app.route("/asl3-ez-manager")
def index():
    content   = read_conf_file(RPT_CONF_PATH)
    nodes     = get_node_numbers(content) if content else []
    templates = sorted(get_node_template_usage(content).keys()) if content else []
    macros    = sorted(get_macro_stanza_usage(content).keys()) if content else []
    schedules = sorted(get_schedule_stanza_usage(content).keys()) if content else []
    return render_template("asl3-ez-manager.html",
                           conf_exists=content is not None,
                           nodes=nodes,
                           templates=templates,
                           macros=macros,
                           schedules=schedules,
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
    usage     = get_node_template_usage(content)
    templates = [t for t, nodes in usage.items() if node in nodes]
    return jsonify({"node": node, "settings": settings, "templates": templates})


@app.route("/api/conf/templates")
def api_get_templates():
    """
    List node-level templates (e.g. [node-main](!)) and which node numbers
    use each one. A "node-level" template is one actually referenced by a
    node-number stanza — this excludes functions/telemetry/morse/etc.
    templates, which use the same (!) syntax but aren't node settings.
    """
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    usage = get_node_template_usage(content)
    return jsonify({"templates": usage})


@app.route("/api/conf/template/<name>")
def api_get_template_conf(name):
    """
    Return the effective settings of a template stanza itself, e.g.
    [node-main](!). Editing these settings here changes them for every
    node that inherits from this template — unlike /api/conf/node/<node>,
    where edits only ever create a node-specific override.
    """
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    if name not in get_template_names(content):
        return jsonify({"error": f"Template [{name}] not found in rpt.conf"}), 404
    settings = parse_stanza_settings(content, name)
    usage    = get_node_template_usage(content)
    return jsonify({"template": name, "settings": settings, "used_by": usage.get(name, [])})


@app.route("/api/conf/macros")
def api_get_macros():
    """List DTMF macro stanzas actually referenced by a node, and which nodes use each."""
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    return jsonify({"stanzas": get_macro_stanza_usage(content)})


@app.route("/api/conf/macro/<name>")
def api_get_macro_conf(name):
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    if name not in _collect_stanzas(content):
        return jsonify({"error": f"Stanza [{name}] not found in rpt.conf"}), 404
    # Macro slots are always digit-keyed (per the documented format) — this
    # also filters out non-numeric documentation lines like the stock
    # config's own format-example comments, which are valid key=value
    # syntax but aren't real macro entries.
    entries = {k: v for k, v in parse_stanza_settings(content, name).items() if MACRO_KEY_RE.match(k)}
    usage   = get_macro_stanza_usage(content)
    return jsonify({"name": name, "entries": entries, "used_by": usage.get(name, [])})


@app.route("/api/conf/schedules")
def api_get_schedules():
    """List scheduler stanzas actually referenced by a node, and which nodes use each."""
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    return jsonify({"stanzas": get_schedule_stanza_usage(content)})


@app.route("/api/conf/schedule/<name>")
def api_get_schedule_conf(name):
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return jsonify({"error": "Cannot read rpt.conf"}), 404
    if name not in _collect_stanzas(content):
        return jsonify({"error": f"Stanza [{name}] not found in rpt.conf"}), 404
    # Schedule keys are always digit-keyed (the macro slot to run) — see
    # the same filtering note in api_get_macro_conf.
    entries = {k: v for k, v in parse_stanza_settings(content, name).items() if MACRO_KEY_RE.match(k)}
    usage   = get_schedule_stanza_usage(content)
    return jsonify({"name": name, "entries": entries, "used_by": usage.get(name, [])})


@app.route("/api/save", methods=["POST"])
def api_save():
    data    = request.json or {}
    content = read_conf_file(RPT_CONF_PATH) or ""
    raw     = data.get("raw_content")

    if raw is not None:
        if session.get('role') != 'superuser':
            return jsonify({"error": "Superuser access required for raw editor"}), 403
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

    is_macro_stanza    = section in get_macro_stanza_usage(content)
    is_schedule_stanza = section in get_schedule_stanza_usage(content)

    errors = []
    for key, info in changes.items():
        if not info.get("enabled", True):
            continue
        value = info.get("value", "")
        if is_macro_stanza:
            err = validate_macro_entry(key, value)
        elif is_schedule_stanza:
            err = validate_schedule_entry(key, value)
        else:
            err = validate_setting(key, value)
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
    names = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".bak")],
        reverse=True
    )
    result = []
    for name in names:
        fpath = os.path.join(BACKUP_DIR, name)
        try:
            size = os.path.getsize(fpath)
        except Exception:
            size = 0
        result.append({"name": name, "size": size})
    return jsonify({"backups": result, "backup_dir": BACKUP_DIR})


@app.route("/api/backup/<filename>")
def api_get_backup(filename):
    # Legacy endpoint kept for backward compatibility
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', filename):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        return jsonify({"content": f.read(), "filename": filename})


@app.route("/api/backups/<name>/download")
def api_backup_download(name):
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', name):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/api/backups/<name>/diff")
def api_backup_diff(name):
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', name):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    try:
        with open(path) as f:
            backup_lines = f.readlines()
        current = read_conf_file(RPT_CONF_PATH)
        if current is None:
            return jsonify({"error": "Cannot read current rpt.conf"}), 500
        current_lines = current.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            backup_lines, current_lines,
            fromfile=name, tofile="rpt.conf (current)",
            lineterm=""
        ))
        return jsonify({"diff": "".join(diff), "name": name})
    except Exception as e:
        log("ERROR", f"[API] /api/backups/{name}/diff error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<name>/restore", methods=["POST"])
def api_backup_restore(name):
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', name):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    try:
        with open(path) as f:
            content = f.read()
        backup = write_conf_file(RPT_CONF_PATH, content)
        log("INFO", f"[API] Restored backup: {name}")
        return jsonify({"ok": True, "backup": backup})
    except PermissionError as e:
        return jsonify({"error": str(e),
                        "hint": "Service must run as root to write rpt.conf"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/backups/{name}/restore error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<name>", methods=["DELETE"])
def api_backup_delete(name):
    if not re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', name):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    try:
        os.unlink(path)
        log("INFO", f"[API] Deleted backup: {name}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Kiosk Settings API ────────────────────────────────────────────────────────

@app.route("/api/kiosk/settings")
def api_kiosk_settings_get():
    return jsonify({
        "idle_timeout_sec":    int(get_setting('kiosk_idle_timeout_sec', '600') or 600),
        "clock_format":        get_setting('kiosk_clock_format', '12'),
        "timezone":            get_setting('kiosk_timezone', 'UTC'),
        "map_pin_duration_min": int(get_setting('kiosk_map_pin_duration_min', '60') or 60),
    })


@app.route("/api/kiosk/settings", methods=["PUT"])
def api_kiosk_settings_put():
    data = request.json or {}
    if "idle_timeout_sec" in data:
        try:
            val = int(data["idle_timeout_sec"])
            if not (60 <= val <= 86400):
                return jsonify({"error": "idle_timeout_sec must be 60–86400 seconds"}), 400
            set_setting('kiosk_idle_timeout_sec', str(val))
        except (TypeError, ValueError):
            return jsonify({"error": "idle_timeout_sec must be an integer"}), 400
    if "clock_format" in data:
        val = str(data["clock_format"])
        if val not in ('12', '24'):
            return jsonify({"error": "clock_format must be '12' or '24'"}), 400
        set_setting('kiosk_clock_format', val)
    if "timezone" in data:
        tz = str(data["timezone"]).strip()
        if not tz:
            return jsonify({"error": "timezone cannot be empty"}), 400
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(tz)
        except Exception:
            return jsonify({"error": f"Unknown timezone: {tz}"}), 400
        set_setting('kiosk_timezone', tz)
    if "map_pin_duration_min" in data:
        try:
            val = int(data["map_pin_duration_min"])
            if not (1 <= val <= 480):
                return jsonify({"error": "map_pin_duration_min must be 1–480 minutes"}), 400
            set_setting('kiosk_map_pin_duration_min', str(val))
        except (TypeError, ValueError):
            return jsonify({"error": "map_pin_duration_min must be an integer"}), 400
    return jsonify({"ok": True})


# ── Connection History API ─────────────────────────────────────────────────────

@app.route("/api/connection-history")
def api_conn_history():
    node   = request.args.get("node",   "").strip()
    search = request.args.get("search", "").strip().lower()
    try:
        limit  = min(int(request.args.get("limit",  50)), 200)
    except (ValueError, TypeError):
        limit  = 50
    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    db     = get_db()
    where  = []
    params = []
    if node:
        where.append("local_node = ?")
        params.append(node)
    if search:
        where.append("(peer_node LIKE ? OR peer_callsign LIKE ? OR peer_location LIKE ?)")
        pat = "%" + search + "%"
        params.extend([pat, pat, pat])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM connection_history {where_sql}", params
    ).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM connection_history {where_sql} "
        "ORDER BY connected_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    return jsonify({
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "rows":   [dict(r) for r in rows],
    })


@app.route("/api/connection-history", methods=["DELETE"])
def api_conn_history_clear():
    db = get_db()
    db.execute("DELETE FROM connection_history")
    db.commit()
    log("INFO", "[API] Connection history cleared")
    return jsonify({"ok": True})


# ── Alerts API ─────────────────────────────────────────────────────────────────

@app.route("/api/alerts/config")
def api_alerts_get_config():
    cfg = _get_alert_config()
    if cfg is None:
        # Return defaults before any config is saved
        return jsonify({
            "enabled": 0, "provider": "ntfy", "ntfy_topic": "",
            "pushover_token": "", "pushover_user": "",
            "on_ami_disconnect": 1, "on_ami_reconnect": 0,
            "on_cpu_temp_high": 1, "cpu_temp_threshold": 80,
            "on_node_connect": 0, "on_node_disconnect": 0, "watch_nodes": "",
        })
    return jsonify(dict(cfg))


@app.route("/api/alerts/config", methods=["POST"])
def api_alerts_save_config():
    data = request.json or {}
    db   = get_db()
    db.execute(
        """INSERT OR REPLACE INTO alert_config
           (id, enabled, provider, ntfy_topic, pushover_token, pushover_user,
            on_ami_disconnect, on_ami_reconnect, on_cpu_temp_high, cpu_temp_threshold,
            on_node_connect, on_node_disconnect, watch_nodes)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            1 if data.get("enabled")           else 0,
            str(data.get("provider",           "ntfy")),
            str(data.get("ntfy_topic",         "")),
            str(data.get("pushover_token",     "")),
            str(data.get("pushover_user",      "")),
            1 if data.get("on_ami_disconnect") else 0,
            1 if data.get("on_ami_reconnect")  else 0,
            1 if data.get("on_cpu_temp_high")  else 0,
            int(data.get("cpu_temp_threshold", 80)),
            1 if data.get("on_node_connect")   else 0,
            1 if data.get("on_node_disconnect") else 0,
            str(data.get("watch_nodes",        "")),
        )
    )
    db.commit()
    log("INFO", "[API] Alert config saved")
    return jsonify({"ok": True})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    try:
        cfg = _get_alert_config()
        if not cfg:
            return jsonify({"error": "No alert config saved yet"}), 400
        if not cfg["enabled"]:
            return jsonify({"error": "Alerts are disabled — enable them first"}), 400
        _send_alert("ASL3-EZ: Test Alert",
                    "This is a test notification from ASL3-EZ", "default")
        return jsonify({"ok": True, "message": "Test alert sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── User Management API ───────────────────────────────────────────────────────

@app.route("/api/users")
def api_users_list():
    rows = get_db().execute(
        "SELECT id, username, role, created_at FROM users ORDER BY role DESC, username"
    ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/users", methods=["POST"])
def api_users_create():
    data        = request.json or {}
    username    = str(data.get("username", "")).strip()
    password    = str(data.get("password", ""))
    role        = str(data.get("role", "user")).strip()
    caller_role = session.get('role', '')
    if not re.match(r'^[A-Za-z0-9_.-]{2,32}$', username):
        return jsonify({"error": "Username must be 2-32 chars: letters, digits, _ . -"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if role not in ("superuser", "admin", "user"):
        return jsonify({"error": "Role must be superuser, admin, or user"}), 400
    # Admins can only create user-level accounts
    if caller_role == 'admin' and role in ('superuser', 'admin'):
        return jsonify({"error": "Admins can only create user accounts"}), 403
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                   (username, generate_password_hash(password), role))
        db.commit()
        log("INFO", f"[USERS] Created user '{username}' role={role} by {caller_role}")
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"error": f"Username '{username}' already exists."}), 409


@app.route("/api/users/<int:uid>", methods=["PUT"])
def api_users_update(uid):
    data        = request.json or {}
    db          = get_db()
    user        = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    caller_role = session.get('role', '')
    if not user:
        return jsonify({"error": "User not found"}), 404
    new_role = str(data.get("role", user["role"])).strip()
    # Admins cannot elevate accounts to admin/superuser, or edit existing elevated accounts
    if caller_role == 'admin' and (user["role"] in ('superuser', 'admin') or
                                    new_role in ('superuser', 'admin')):
        return jsonify({"error": "Admins can only manage user accounts"}), 403
    # Prevent removing the last superuser
    if user["role"] == "superuser" and new_role != "superuser":
        su_count = db.execute("SELECT COUNT(*) FROM users WHERE role='superuser'").fetchone()[0]
        if su_count <= 1:
            return jsonify({"error": "Cannot change the last superuser account."}), 400
    updates = []
    params  = []
    if "role" in data:
        if new_role not in ("superuser", "admin", "user"):
            return jsonify({"error": "Role must be superuser, admin, or user"}), 400
        updates.append("role=?"); params.append(new_role)
    if "password" in data:
        pw = data["password"]
        if len(pw) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        updates.append("password_hash=?"); params.append(generate_password_hash(pw))
    if not updates:
        return jsonify({"ok": True, "note": "Nothing to update"})
    params.append(uid)
    db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
    db.commit()
    log("INFO", f"[USERS] Updated user id={uid}: {', '.join(k for k in data if k != 'password')}")
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
def api_users_delete(uid):
    db          = get_db()
    user        = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    caller_role = session.get('role', '')
    if not user:
        return jsonify({"error": "User not found"}), 404
    if session.get("user_id") == uid:
        return jsonify({"error": "Cannot delete your own account."}), 400
    if caller_role == 'admin' and user["role"] in ('superuser', 'admin'):
        return jsonify({"error": "Admins can only manage user accounts"}), 403
    if user["role"] == "superuser":
        su_count = db.execute("SELECT COUNT(*) FROM users WHERE role='superuser'").fetchone()[0]
        if su_count <= 1:
            return jsonify({"error": "Cannot delete the last superuser account."}), 400
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    log("INFO", f"[USERS] Deleted user '{user['username']}' (id={uid})")
    return jsonify({"ok": True})


# ── Favorites API ─────────────────────────────────────────────────────────────

@app.route("/api/favorites")
def api_favorites():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"favorites": []})
    try:
        db   = get_db()
        rows = db.execute("SELECT * FROM favorites WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        favs = [dict(r) for r in rows]
        for fav in favs:
            fav.update(lookup_node(fav["node"]))
        return jsonify({"favorites": favs})
    except Exception as e:
        log("ERROR", f"[API] /api/favorites: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/status")
def api_favorites_status():
    """
    Cached keyed/connected-count status for the current user's favorites,
    sourced from the background favstats poller. Always reads from cache.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"favorites": {}, "poll_interval": FAVORITES_POLL_INTERVAL})
    try:
        db    = get_db()
        nodes = [r["node"] for r in db.execute(
            "SELECT node FROM favorites WHERE user_id=?", (user_id,)).fetchall()]
        return jsonify({
            "favorites":     {n: get_cached_favstats(n) for n in nodes},
            "poll_interval": FAVORITES_POLL_INTERVAL,
        })
    except Exception as e:
        log("ERROR", f"[API] /api/favorites/status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/add", methods=["POST"])
def api_fav_add():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
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
        db.execute("INSERT OR IGNORE INTO favorites (user_id, node, label) VALUES (?,?,?)",
                   (user_id, node, label))
        db.commit()
        log("INFO", f"[API] Favorite added: user_id={user_id} node={node} label={label!r}")
        return jsonify({"success": True, "node": node, "label": label})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/delete", methods=["POST"])
def api_fav_delete():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    data = request.json or {}
    node = str(data.get("node", "")).strip()
    try:
        db = get_db()
        db.execute("DELETE FROM favorites WHERE user_id=? AND node=?", (user_id, node))
        db.commit()
        log("INFO", f"[API] Favorite deleted: user_id={user_id} node={node}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorites/label", methods=["POST"])
def api_fav_label():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    data  = request.json or {}
    node  = str(data.get("node",  "")).strip()
    label = str(data.get("label", "")).strip()
    try:
        db = get_db()
        db.execute("UPDATE favorites SET label=? WHERE user_id=? AND node=?",
                   (label, user_id, node))
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
        "auth_configured":       is_auth_configured(),
        "auth_user":             session.get("username", "") if is_auth_configured() else "",
    })


@app.route("/api/status/board")
def api_status_board():
    """
    Single aggregated endpoint for the Status Board page.
    Returns everything needed in one call so the board only issues one
    HTTP request per refresh cycle.
    """
    content    = read_conf_file(RPT_CONF_PATH)
    nodes      = get_node_numbers(content) if content else []
    ast_status = get_asterisk_status()

    node_data = []
    for node in nodes:
        info     = lookup_node(node)
        cached   = get_cached_status(node)
        connected_details = []
        lct = cached.get("link_connect_time", {})
        with _link_stats_lock:
            ls_snapshot = dict(_link_stats)
        node_str_local = str(node)
        idle_timeout   = int(get_setting('kiosk_idle_timeout_sec', '600') or 600)
        for cn in cached.get("connected", []):
            cn_info    = lookup_node(cn)
            cn_loc     = cn_info.get("location", "")
            cn_coords  = _geocode(cn_loc) if cn_loc else None
            ls = ls_snapshot.get(cn, {})
            # Idle-timeout info for kiosk display
            with _kiosk_temp_lock:
                tc = _kiosk_temp_conns.get((node_str_local, cn))
            if tc and not tc.get('permanent'):
                idle_remaining = max(0, int(idle_timeout - (time.time() - tc['last_active'])))
                is_permanent   = False
            elif tc and tc.get('permanent'):
                idle_remaining = None
                is_permanent   = True
            else:
                idle_remaining = None
                is_permanent   = None  # not tracked (pre-existing connection)
            connected_details.append({
                "node":           cn,
                "callsign":       cn_info.get("callsign", ""),
                "desc":           cn_info.get("desc", ""),
                "location":       cn_loc,
                "keyed":          cached.get("links", {}).get(cn, {}).get("keyed", False),
                "connect_time":   lct.get(cn, ""),
                "keyups":         ls.get("keyups", 0),
                "last_keyed":     ls.get("last_keyed"),
                "lat":            cn_coords["lat"] if cn_coords else None,
                "lon":            cn_coords["lon"] if cn_coords else None,
                "connect_state":  cached.get("link_connect_state", {}).get(cn, "ESTABLISHED"),
                "idle_remaining": idle_remaining,
                "permanent":      is_permanent,
            })
        location = info.get("location", "")
        coords   = _geocode(location) if location else None
        node_data.append({
            "node":      node,
            "callsign":  info.get("callsign", ""),
            "desc":      info.get("desc", ""),
            "location":  location,
            "lat":       coords["lat"] if coords else None,
            "lon":       coords["lon"] if coords else None,
            "keyed":     cached.get("keyed", False),
            "connected": connected_details,
            "stale":     cached.get("stale", True),
        })

    return jsonify({
        "nodes":            node_data,
        "asterisk_active":  ast_status["active"],
        "asterisk_version": ast_status["version"],
        "asl_version":      get_asl_version(),
        "uptime":           get_uptime(),
        "cpu_temp":         get_cpu_temp(),
        "disk":             get_disk_usage(),
        "ami_connected":    _ami_connected,
    })


@app.route("/api/status/weather")
def api_status_weather():
    """Current weather for the primary node's location. Cached 10 min."""
    content  = read_conf_file(RPT_CONF_PATH)
    nodes    = get_node_numbers(content) if content else []
    location = ""
    if nodes:
        location = lookup_node(nodes[0]).get("location", "")
    if not location:
        location = request.args.get("location", "")
    return jsonify(_fetch_weather(location))


@app.route("/api/status/activity")
def api_status_activity():
    """
    Combined activity feed for the Status Board:
    - recently_keyed: nodes observed keying via this node's AMI (real-time)
    - global_nodes:   top 10 most recently registered ASL nodes worldwide
                      (polled every 5 min from stats.allstarlink.org)
    """
    pin_min = int(get_setting('kiosk_map_pin_duration_min', '60') or 60)
    cutoff  = time.time() - pin_min * 60

    with _keyed_history_lock:
        keyed = [e for e in _keyed_history if e["ts"] >= cutoff]

    # Attach geocoordinates to recently-keyed entries (cache hit = instant)
    for entry in keyed:
        if entry["lat"] is None and entry["location"]:
            coords = _geocode(entry["location"])
            if coords:
                entry["lat"] = coords["lat"]
                entry["lon"] = coords["lon"]

    with _global_nodes_lock:
        global_nodes = list(_global_nodes_cache[:10])
        global_ts    = _global_nodes_ts

    return jsonify({
        "recently_keyed":    keyed[:10],
        "global_nodes":      global_nodes,
        "global_updated":    global_ts,
        "pin_duration_min":  pin_min,
    })


@app.route("/api/status/connect", methods=["POST"])
def api_status_connect():
    """Connect a remote node to a local node."""
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    # Only admin/superuser may mark a connection as permanent (no idle timeout)
    caller_role = session.get('role', '')
    permanent   = bool(data.get("permanent", False)) and caller_role in ('admin', 'superuser')
    monitor     = bool(data.get("monitor", False))
    ilink_mode  = "2" if monitor else "3"   # 2=monitor (listen-only), 3=transceive
    try:
        with _ami_pool_lock:
            ami = _ami_ensure_connected()
            result = ami.rpt_cmd(local_node, f"ilink {ilink_mode} {remote_node}")
        with _kiosk_temp_lock:
            _kiosk_temp_conns[(local_node, remote_node)] = {
                'permanent':   permanent,
                'last_active': time.time(),
            }
        log("INFO", f"[API] /api/status/connect {local_node} -> {remote_node} permanent={permanent}")
        return jsonify({"ok": True, "output": result})
    except Exception as e:
        log("ERROR", f"[API] /api/status/connect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status/disconnect", methods=["POST"])
def api_status_disconnect():
    """Disconnect a remote node from a local node — public endpoint for the Status Board."""
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    try:
        with _ami_pool_lock:
            ami = _ami_ensure_connected()
            result = ami.rpt_cmd(local_node, f"ilink 1 {remote_node}")
        log("INFO", f"[API] /api/status/disconnect {local_node} -> {remote_node}")
        return jsonify({"ok": True, "output": result})
    except Exception as e:
        log("ERROR", f"[API] /api/status/disconnect error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Audio monitoring — one ffmpeg per node, broadcast to N simultaneous clients
#
# Approach: find the node's existing Asterisk channel (e.g. SimpleUSB/643930)
# and apply MixMonitor directly to it via AMI.  No Local channel, no dialplan,
# no Originate — we tap the channel that is already live.
# ---------------------------------------------------------------------------
import queue as _queue_mod

_audio_lock   = threading.Lock()
_audio_active = {}   # node -> _AudioBroadcast


def _find_node_channel(node):
    """
    Return the Asterisk channel name for a local node by scanning
    'core show channels' for any channel whose name contains '/<node>'.
    Returns None if not found.
    """
    def _cmd(ami):
        return {'lines': ami.command('core show channels')}
    try:
        lines = ami_send_command(_cmd).get('lines', [])
        for line in lines:
            parts = line.split()
            if parts and f'/{node}' in parts[0]:
                return parts[0]
    except Exception as e:
        log('WARN', f'[AUDIO] channel search failed: {e}')
    return None


class _AudioBroadcast:
    """
    Owns one ffmpeg process and fans its Ogg/Opus output to any number of
    simultaneous HTTP streaming clients, each backed by its own Queue.

    Lifecycle:
      - Created by the first listener for a node.
      - A background thread reads ffmpeg stdout and puts chunks into every
        client queue (unlimited size; a slow/dead client's queue grows until
        gunicorn detects the disconnect and the generator finally-block fires).
      - Each client's generate() calls q.get() (blocks until a chunk arrives).
      - When a client disconnects, remove_client() is called; if it was the
        last one, shutdown() tears everything down.
      - shutdown() can also be called directly from /api/audio/stop, in which
        case a None sentinel is fanned out so every blocked q.get() unblocks.
    """

    def __init__(self, node, channel, ffmpeg_proc, fifo_path, placeholder_fd):
        self.node           = node
        self.channel        = channel   # Asterisk channel MixMonitor was applied to
        self.ffmpeg_proc    = ffmpeg_proc
        self.fifo_path      = fifo_path
        self.placeholder_fd = placeholder_fd
        self._lock          = threading.Lock()
        self._clients       = []
        self._dead          = False
        self._reader        = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ── client management ────────────────────────────────────────────────────

    def add_client(self):
        q = _queue_mod.Queue()   # unlimited — sentinel always lands
        with self._lock:
            self._clients.append(q)
        log('INFO', f'[AUDIO] client added for node {self.node} '
                    f'(total {len(self._clients)})')
        return q

    def remove_client(self, q):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass
            remaining = len(self._clients)
        log('INFO', f'[AUDIO] client removed for node {self.node} '
                    f'({remaining} remaining)')
        if remaining == 0:
            self.shutdown()

    # ── internal ─────────────────────────────────────────────────────────────

    def _fanout(self, item):
        with self._lock:
            for q in self._clients:
                q.put(item)

    def _read_loop(self):
        try:
            while True:
                # read1() returns whatever is available immediately rather than
                # blocking until the full buffer is filled — critical for low latency
                chunk = self.ffmpeg_proc.stdout.read1(512)
                if not chunk:
                    break
                self._fanout(chunk)
        finally:
            self._fanout(None)   # unblock every waiting generate()

    def shutdown(self):
        with self._lock:
            if self._dead:
                return
            self._dead = True

        # Remove from global registry so a new stream can start fresh
        with _audio_lock:
            if _audio_active.get(self.node) is self:
                del _audio_active[self.node]

        # Unblock any clients still waiting on q.get()
        self._fanout(None)
        with self._lock:
            self._clients.clear()

        try:
            self.ffmpeg_proc.terminate()
            self.ffmpeg_proc.wait(timeout=2)
        except Exception:
            pass
        try:
            os.close(self.placeholder_fd)
        except Exception:
            pass
        try:
            os.unlink(self.fifo_path)
        except Exception:
            pass

        def _stop_mm(ami):
            ami._send_action({'Action': 'StopMixMonitor', 'Channel': self.channel})
            ami._recv_until('\r\n\r\n', timeout=ami.timeout)
            return {'ok': True}
        try:
            ami_send_command(_stop_mm)
        except Exception:
            pass
        log('INFO', f'[AUDIO] broadcast for node {self.node} shut down')


def _start_broadcast(node):
    """
    Apply MixMonitor to the node's existing Asterisk channel, pipe the
    slin audio through ffmpeg → Ogg/Opus, and return an _AudioBroadcast.
    Raises on error.
    """
    channel = _find_node_channel(node)
    if not channel:
        raise RuntimeError(
            f'No active Asterisk channel found for node {node}. '
            'Is the node running?'
        )
    log('INFO', f'[AUDIO] found channel {channel!r} for node {node}')

    fifo_path = f'/tmp/asl3ez_audio_{node}.sln'
    if os.path.exists(fifo_path):
        os.unlink(fifo_path)
    os.mkfifo(fifo_path)
    os.chmod(fifo_path, 0o666)   # asterisk user must be able to write

    # Hold write-end open (O_RDWR) so ffmpeg's blocking O_RDONLY open
    # succeeds immediately and doesn't get EOF before MixMonitor connects.
    pfd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
    flags = fcntl.fcntl(pfd, fcntl.F_GETFL)
    fcntl.fcntl(pfd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    ffmpeg_proc = subprocess.Popen(
        [
            'ffmpeg', '-loglevel', 'quiet',
            '-fflags', '+nobuffer',
            '-f', 's16le', '-ar', '8000', '-ac', '1',
            '-i', fifo_path,
            '-c:a', 'libopus', '-b:a', '24k',
            '-frame_duration', '20',
            '-f', 'webm',
            '-cluster_time_limit', '200',  # flush WebM cluster every 200ms
            'pipe:1',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    broadcast = _AudioBroadcast(node, channel, ffmpeg_proc, fifo_path, pfd)

    # Ensure app_mixmonitor.so is loaded (it's not in the default ASL3 module list)
    def _ensure_mm_module(ami):
        lines = ami.command('module show like app_mixmonitor')
        if not any('app_mixmonitor' in l for l in lines):
            ami.command('module load app_mixmonitor.so')
            log('INFO', '[AUDIO] Loaded app_mixmonitor.so on demand')
        return {'ok': True}
    try:
        ami_send_command(_ensure_mm_module)
    except Exception as e:
        log('WARN', f'[AUDIO] module load check failed: {e}')

    # Apply MixMonitor directly to the existing node channel
    def _start_mm(ami):
        ami._send_action({
            'Action':  'MixMonitor',
            'Channel': channel,
            'File':    fifo_path,
            'Options': '',
        })
        raw = ami._recv_until('\r\n\r\n', timeout=ami.timeout)
        pkt = ami._parse_packet(raw)
        if pkt.get('Response') == 'Error':
            raise RuntimeError(pkt.get('Message', 'MixMonitor failed'))
        return pkt

    try:
        ami_send_command(_start_mm)
    except Exception as e:
        broadcast.shutdown()
        raise RuntimeError(f'MixMonitor failed: {e}')

    log('INFO', f'[AUDIO] MixMonitor started on {channel} for node {node}')
    return broadcast


@app.route('/api/audio/stream/<node>')
def api_audio_stream(node):
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({'error': 'invalid node'}), 400

    try:
        with _audio_lock:
            broadcast = _audio_active.get(node)
            if broadcast is None or broadcast._dead:
                broadcast = _start_broadcast(node)
                _audio_active[node] = broadcast
            client_q = broadcast.add_client()
    except Exception as e:
        log('ERROR', f'[AUDIO] stream setup for {node}: {e}')
        return jsonify({'error': str(e)}), 500

    def generate():
        try:
            while True:
                chunk = client_q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            broadcast.remove_client(client_q)

    return Response(
        stream_with_context(generate()),
        mimetype='audio/webm',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/audio/stop', methods=['POST'])
def api_audio_stop():
    node = str((request.json or {}).get('node', '')).strip()
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({'error': 'invalid node'}), 400
    with _audio_lock:
        broadcast = _audio_active.get(node)
    if broadcast:
        broadcast.shutdown()
    return jsonify({'ok': True})


@app.route('/api/audio/check/<node>')
def api_audio_check(node):
    """Diagnostic: verify prerequisites for audio streaming."""
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({'error': 'invalid node'}), 400
    issues = []
    if not shutil.which('ffmpeg'):
        issues.append('ffmpeg not found in PATH')
    else:
        try:
            r = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, timeout=5)
            if 'libopus' not in r.stdout:
                issues.append('ffmpeg lacks libopus encoder')
        except Exception as e:
            issues.append(f'ffmpeg probe failed: {e}')
    def _check_module(ami):
        lines = ami.command('module show like app_mixmonitor')
        return {'loaded': any('app_mixmonitor' in l for l in lines)}
    try:
        if not ami_send_command(_check_module).get('loaded'):
            issues.append('app_mixmonitor.so not loaded in Asterisk')
    except Exception as e:
        issues.append(f'module check failed: {e}')
    channel = _find_node_channel(node)
    if not channel:
        issues.append(f'No active Asterisk channel found for node {node}')
    with _audio_lock:
        active = node in _audio_active
    return jsonify({
        'ok':      len(issues) == 0,
        'issues':  issues,
        'active':  active,
        'channel': channel,
        'node':    node,
    })


@app.route("/")
def status_board():
    return render_template("status.html")


@app.route("/status")
def status_board_redirect():
    return redirect(url_for('status_board'), 301)


# ── Asterisk console log viewer ───────────────────────────────────────────────

@app.route("/api/asterisk/log")
def api_asterisk_log():
    """
    Return the last N lines from the Asterisk log file.
    Query params:
      lines  (int, default 100, max 2000)
      filter (str, optional) — case-insensitive substring filter
    """
    try:
        n = min(int(request.args.get("lines", 100)), 2000)
    except (ValueError, TypeError):
        n = 100
    filt = request.args.get("filter", "").lower().strip()

    if not os.path.exists(ASTERISK_LOG_PATH):
        return jsonify({"lines": [], "path": ASTERISK_LOG_PATH,
                        "error": f"Log file not found: {ASTERISK_LOG_PATH}"}), 404

    try:
        with open(ASTERISK_LOG_PATH, "rb") as f:
            # Efficient tail: seek near the end, read, decode
            f.seek(0, 2)
            size = f.tell()
            # Read up to 512 KB from the end — enough for thousands of lines
            read_size = min(size, 512 * 1024)
            f.seek(size - read_size)
            raw = f.read(read_size).decode("utf-8", errors="replace")

        all_lines = raw.splitlines()
        if filt:
            all_lines = [l for l in all_lines if filt in l.lower()]
        result = all_lines[-n:]
        log("DEBUG", f"[API] /api/asterisk/log: returning {len(result)} lines (filter={filt!r})")
        return jsonify({"lines": result, "path": ASTERISK_LOG_PATH, "total_returned": len(result)})
    except PermissionError:
        return jsonify({"lines": [], "path": ASTERISK_LOG_PATH,
                        "error": f"Permission denied reading {ASTERISK_LOG_PATH}"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/asterisk/log error: {e}")
        return jsonify({"lines": [], "path": ASTERISK_LOG_PATH, "error": str(e)}), 500


@app.route("/api/asterisk/verbose", methods=["POST"])
def api_asterisk_verbose():
    """
    Set Asterisk console verbosity via AMI.
    Body: {"level": N}  where N is 0–9.
    Equivalent to running 'asterisk -rvvv' (level=3) from the command line.
    """
    data = request.get_json(force=True)
    try:
        level = int(data.get("level", 3))
        if not 0 <= level <= 9:
            return jsonify({"error": "level must be 0–9"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "level must be an integer 0–9"}), 400

    def _set_verbose(ami):
        lines = ami.command(f"core set verbose {level}")
        return {"ok": True, "level": level, "output": lines}

    try:
        result = ami_send_command(_set_verbose)
        log("INFO", f"[API] Asterisk verbosity set to {level}")
        return jsonify(result)
    except Exception as e:
        log("ERROR", f"[API] /api/asterisk/verbose error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/asterisk/command", methods=["POST"])
def api_asterisk_command():
    """
    Run an arbitrary Asterisk CLI command via AMI and return its output.
    A small blocklist prevents commands that could stop or restart Asterisk
    from the CLI page (dedicated buttons exist for restart/reload).
    """
    data = request.get_json(force=True)
    cmd  = str(data.get("command", "")).strip()

    if not cmd:
        return jsonify({"error": "command is required"}), 400
    if len(cmd) > 512:
        return jsonify({"error": "command too long (max 512 chars)"}), 400

    # Block commands that would stop/restart Asterisk from this endpoint —
    # those operations have their own dedicated buttons and confirmation flow.
    _BLOCKED = ("core stop", "core shutdown", "core restart now",
                "core restart gracefully", "core restart when convenient")
    if any(cmd.lower().startswith(b) for b in _BLOCKED):
        return jsonify({"error": f"Use the Restart/Reload button on the Dashboard for that operation."}), 403

    def _run(ami):
        lines = ami.command(cmd)
        return {"ok": True, "command": cmd, "output": lines}

    try:
        result = ami_send_command(_run)
        log("INFO", f"[API] CLI command: {cmd!r} -> {len(result['output'])} lines")
        return jsonify(result)
    except Exception as e:
        log("ERROR", f"[API] /api/asterisk/command error: {e}")
        return jsonify({"error": str(e)}), 500


# ── App settings (SECRET_KEY) ─────────────────────────────────────────────────
#
# SECRET_KEY signs Flask session cookies, which is how authentication state is
# maintained after login. A weak or well-known key lets an attacker forge a
# valid session cookie without knowing the password. These routes let the user
# rotate it from the UI instead of hand-editing the systemd unit file.

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


@app.route("/api/auth/change-password", methods=["POST"])
def api_change_password():
    data       = request.json or {}
    current_pw = data.get("current_password", "")
    new_pw     = data.get("new_password", "")
    username   = session.get("username", "")
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current_pw):
        return jsonify({"error": "Current password is incorrect."}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    db.execute("UPDATE users SET password_hash=? WHERE username=?",
               (generate_password_hash(new_pw), username))
    db.commit()
    log("INFO", f"[AUTH] Password changed for '{username}'")
    return jsonify({"success": True, "message": "Password updated."})


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
# Announcements — audio file upload, ULAW conversion, scheduler
# ---------------------------------------------------------------------------

def _ann_slug(name: str) -> str:
    """Turn a user-supplied name into a filesystem/Asterisk-safe slug."""
    slug = re.sub(r'[^a-zA-Z0-9_-]', '_', name.strip().lower())
    slug = re.sub(r'_+', '_', slug).strip('_') or "announcement"
    return slug[:48]


def _ann_unique_slug(base: str, exclude_id: int = None) -> str:
    """Append a counter suffix until the slug is unique in the DB."""
    db = get_db()
    slug = base
    i = 1
    while True:
        q = "SELECT id FROM announcements WHERE slug=?"
        row = db.execute(q, (slug,)).fetchone()
        if row is None or (exclude_id and row["id"] == exclude_id):
            return slug
        slug = f"{base}_{i}"
        i += 1


def _convert_to_ulaw(src: str, dest: str) -> None:
    """Convert src (mp3/wav/etc.) to 8 kHz mono ULAW and write to dest."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ar", "8000", "-ac", "1", "-f", "mulaw", dest],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")


def _ann_sound_path(slug: str) -> str:
    return os.path.join(SOUNDS_DIR, f"{slug}.ulaw")


def _run_due_announcements():
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    db = get_db()
    rows = db.execute("SELECT * FROM announcements WHERE enabled=1").fetchall()

    for row in rows:
        try:
            ws_h, ws_m = map(int, row["window_start"].split(":"))
            we_h, we_m = map(int, row["window_end"].split(":"))
        except Exception:
            continue

        start_min = ws_h * 60 + ws_m
        end_min   = we_h * 60 + we_m
        if not (start_min <= now_min <= end_min):
            continue

        if row["last_played"]:
            try:
                last = datetime.strptime(row["last_played"], "%Y-%m-%d %H:%M:%S")
                elapsed_min = (now - last).total_seconds() / 60.0
                if elapsed_min < row["interval_min"]:
                    continue
            except Exception:
                pass

        node = row["node"]
        cached = _ami_cache.get(node, {})
        if cached.get("keyed", False):
            log("INFO", f"[ANNOUNCE] Node {node} busy — deferring '{row['name']}'")
            continue

        sound_arg = f"asl3ez/{row['slug']}"
        cmd = f"rpt {row['play_cmd']} {node} {sound_arg}"
        log("INFO", f"[ANNOUNCE] Firing '{row['name']}' on {node}: {cmd!r}")
        try:
            def _play(ami, _cmd=cmd):
                out = ami.command(_cmd)
                return {"output": out}
            ami_send_command(_play)
            db.execute("UPDATE announcements SET last_played=? WHERE id=?",
                       (now_str, row["id"]))
            db.commit()
        except Exception as e:
            log("ERROR", f"[ANNOUNCE] Playback failed for '{row['name']}': {e}")


def _announce_loop():
    log("INFO", "[ANNOUNCE] Scheduler started (30s interval)")
    while True:
        time.sleep(30)
        try:
            _run_due_announcements()
        except Exception as e:
            log("ERROR", f"[ANNOUNCE] Scheduler error: {e}")


def start_announcer():
    t = threading.Thread(target=_announce_loop, name="announcer", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Announcement API routes
# ---------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


@app.route("/api/announcements")
def api_ann_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM announcements ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/announcements", methods=["POST"])
def api_ann_create():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f    = request.files["file"]
    name = request.form.get("name", "").strip()
    node = request.form.get("node", "").strip()

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    try:
        interval_min = int(request.form.get("interval_min", 60))
        if interval_min < 1:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "interval_min must be a positive integer"}), 400

    window_start = request.form.get("window_start", "07:30").strip()
    window_end   = request.form.get("window_end",   "19:30").strip()
    play_cmd     = request.form.get("play_cmd",     "localplay").strip()
    if play_cmd not in ("localplay", "playback"):
        play_cmd = "localplay"

    if not re.match(r'^\d{2}:\d{2}$', window_start) or not re.match(r'^\d{2}:\d{2}$', window_end):
        return jsonify({"error": "window_start and window_end must be HH:MM"}), 400

    os.makedirs(SOUNDS_DIR, exist_ok=True)

    base_slug = _ann_slug(name)
    slug      = _ann_unique_slug(base_slug)
    dest      = _ann_sound_path(slug)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        _convert_to_ulaw(tmp_path, dest)
    except Exception as e:
        os.unlink(tmp_path)
        return jsonify({"error": f"Audio conversion failed: {e}"}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    try:
        shutil.chown(dest, user="asterisk", group="asterisk")
        os.chmod(dest, 0o640)
    except Exception:
        pass

    db = get_db()
    db.execute(
        """INSERT INTO announcements
           (name, slug, node, enabled, interval_min, window_start, window_end, play_cmd)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (name, slug, node, interval_min, window_start, window_end, play_cmd),
    )
    db.commit()
    row = db.execute("SELECT * FROM announcements WHERE slug=?", (slug,)).fetchone()
    log("INFO", f"[ANNOUNCE] Created announcement '{name}' (slug={slug}, node={node})")
    return jsonify(dict(row)), 201


@app.route("/api/announcements/<int:ann_id>", methods=["PATCH"])
def api_ann_update(ann_id):
    db   = get_db()
    row  = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}

    name         = str(data.get("name",         row["name"])).strip()
    node         = str(data.get("node",         row["node"])).strip()
    interval_min = int(data.get("interval_min", row["interval_min"]))
    window_start = str(data.get("window_start", row["window_start"])).strip()
    window_end   = str(data.get("window_end",   row["window_end"])).strip()
    play_cmd     = str(data.get("play_cmd",     row["play_cmd"])).strip()

    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400
    if interval_min < 1:
        return jsonify({"error": "interval_min must be >= 1"}), 400
    if not re.match(r'^\d{2}:\d{2}$', window_start) or not re.match(r'^\d{2}:\d{2}$', window_end):
        return jsonify({"error": "window times must be HH:MM"}), 400
    if play_cmd not in ("localplay", "playback"):
        play_cmd = "localplay"

    db.execute(
        """UPDATE announcements
           SET name=?, node=?, interval_min=?, window_start=?, window_end=?, play_cmd=?
           WHERE id=?""",
        (name, node, interval_min, window_start, window_end, play_cmd, ann_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/announcements/<int:ann_id>", methods=["DELETE"])
def api_ann_delete(ann_id):
    db  = get_db()
    row = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    sound = _ann_sound_path(row["slug"])
    try:
        if os.path.exists(sound):
            os.unlink(sound)
    except Exception as e:
        log("WARN", f"[ANNOUNCE] Could not delete sound file {sound}: {e}")

    db.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
    db.commit()
    log("INFO", f"[ANNOUNCE] Deleted announcement id={ann_id} '{row['name']}'")
    return jsonify({"ok": True})


@app.route("/api/announcements/<int:ann_id>/toggle", methods=["POST"])
def api_ann_toggle(ann_id):
    db  = get_db()
    row = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["enabled"] else 1
    db.execute("UPDATE announcements SET enabled=? WHERE id=?", (new_val, ann_id))
    db.commit()
    return jsonify({"id": ann_id, "enabled": new_val})


@app.route("/api/announcements/<int:ann_id>/play", methods=["POST"])
def api_ann_play(ann_id):
    db  = get_db()
    row = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    sound = _ann_sound_path(row["slug"])
    if not os.path.exists(sound):
        return jsonify({"error": "Sound file not found on disk"}), 404

    sound_arg = f"asl3ez/{row['slug']}"
    cmd       = f"rpt {row['play_cmd']} {row['node']} {sound_arg}"
    log("INFO", f"[ANNOUNCE] Test play '{row['name']}': {cmd!r}")

    try:
        def _play(ami, _cmd=cmd):
            out = ami.command(_cmd)
            return {"output": out}
        result = ami_send_command(_play)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE announcements SET last_played=? WHERE id=?", (now_str, ann_id))
        db.commit()
        return jsonify({"ok": True, "output": result.get("output", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Smart Connector — scheduled node connect/disconnect with idle monitoring
# ---------------------------------------------------------------------------

def _node_active(node: str) -> bool:
    """Return True if the node is keyed (RX or any linked TX) per AMI cache."""
    cached = _ami_cache.get(node, {})
    if cached.get("keyed", False):
        return True
    return any(l.get("keyed", False) for l in cached.get("links", {}).values())


def _connector_do_connect(local: str, target: str):
    def _cmd(ami, ln=local, tn=target):
        return ami.rpt_cmd(ln, f"ilink 3 {tn}")
    return ami_send_command(_cmd)


def _connector_do_disconnect(local: str, target: str):
    def _cmd(ami, ln=local, tn=target):
        return ami.rpt_cmd(ln, f"ilink 1 {tn}")
    return ami_send_command(_cmd)


def _connector_link_present(local: str, target: str) -> bool:
    """
    Return True if target appears in the AMI cache's connected list for local.
    Returns None (unknown) if the cache has no data yet for this node.
    """
    cached = _ami_cache.get(local)
    if not cached:
        return None  # no data yet — don't assume anything
    return target in cached.get("connected", [])


def _run_connectors():
    now     = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    now_hm  = now.strftime("%H:%M")

    db   = get_db()
    rows = db.execute("SELECT * FROM connectors WHERE enabled=1").fetchall()

    for row in rows:
        cid    = row["id"]
        state  = row["state"]
        local  = row["local_node"]
        target = row["target_node"]

        # idle → check if scheduled connect_time has arrived
        if state == "idle" and row["connect_time"] and now_hm == row["connect_time"]:
            db.execute(
                "UPDATE connectors SET state='waiting', state_msg='Waiting for node to be idle', "
                "state_updated=? WHERE id=?", (now_str, cid)
            )
            db.commit()
            state = "waiting"
            log("INFO", f"[CONNECTOR] '{row['name']}' entered waiting state at {now_hm}")

        # waiting → connect when node is idle (or force after 2 min)
        if state == "waiting":
            node_idle = not _node_active(local)
            forced    = False
            if row["state_updated"]:
                try:
                    su     = datetime.strptime(row["state_updated"], "%Y-%m-%d %H:%M:%S")
                    forced = (now - su).total_seconds() > 120
                except Exception:
                    pass

            if node_idle or forced:
                label = "forced" if forced else "node idle"
                try:
                    result = _connector_do_connect(local, target)
                    if not result.get("success", True):
                        raise RuntimeError(
                            f"ilink 3 rejected by Asterisk: {result.get('raw', '')[:120]}"
                        )
                    db.execute(
                        "UPDATE connectors SET state='connected', state_msg='Connected', "
                        "state_updated=?, connected_at=?, last_activity=? WHERE id=?",
                        (now_str, now_str, now_str, cid)
                    )
                    db.commit()
                    log("INFO", f"[CONNECTOR] '{row['name']}' connected ({label})")
                except Exception as e:
                    db.execute(
                        "UPDATE connectors SET state='error', state_msg=?, state_updated=? WHERE id=?",
                        (str(e)[:200], now_str, cid)
                    )
                    db.commit()
                    log("ERROR", f"[CONNECTOR] Connect failed for '{row['name']}': {e}")

        # connected → verify link still exists, settle, then monitor idle
        elif state == "connected":
            # Check AMI cache: if we have fresh data and target is gone, reset to idle
            link_present = _connector_link_present(local, target)
            if link_present is False:
                db.execute(
                    "UPDATE connectors SET state='idle', "
                    "state_msg='Link ended (remote node disconnected)', "
                    "state_updated=?, connected_at=NULL, last_activity=NULL WHERE id=?",
                    (now_str, cid)
                )
                db.commit()
                log("INFO", f"[CONNECTOR] '{row['name']}' — target {target} no longer in lstats, resetting to idle")
                continue

            try:
                connected_at = datetime.strptime(row["connected_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                connected_at = now

            if (now - connected_at).total_seconds() < row["settle_sec"]:
                continue  # still in settle window

            if _node_active(local):
                db.execute("UPDATE connectors SET last_activity=? WHERE id=?", (now_str, cid))
                db.commit()
            else:
                # Use connected_at as the fallback if last_activity is somehow NULL
                last_act_str = row["last_activity"] or row["connected_at"] or now_str
                try:
                    last_act = datetime.strptime(last_act_str, "%Y-%m-%d %H:%M:%S")
                    idle_sec = (now - last_act).total_seconds()
                except Exception:
                    idle_sec = row["idle_limit_sec"]  # safe: treat as timed-out

                log("DEBUG", f"[CONNECTOR] '{row['name']}' idle for {idle_sec:.0f}s / {row['idle_limit_sec']}s")

                if idle_sec >= row["idle_limit_sec"]:
                    try:
                        result = _connector_do_disconnect(local, target)
                        # ilink 1 on an already-gone node returns no-error output
                        # so treat both success and "node not connected" as done
                        raw = result.get("raw", "")
                        hard_fail = any(w in raw for w in ["permission denied", "not permitted", "unknown command"])
                        if hard_fail:
                            raise RuntimeError(f"ilink 1 rejected: {raw[:120]}")
                        db.execute(
                            "UPDATE connectors SET state='idle', "
                            "state_msg='Auto-disconnected after idle timeout', "
                            "state_updated=?, connected_at=NULL, last_activity=NULL WHERE id=?",
                            (now_str, cid)
                        )
                        db.commit()
                        log("INFO", f"[CONNECTOR] '{row['name']}' auto-disconnected after {idle_sec:.0f}s idle")
                    except Exception as e:
                        log("ERROR", f"[CONNECTOR] Disconnect failed for '{row['name']}': {e}")
                        db.execute(
                            "UPDATE connectors SET state_msg=? WHERE id=?",
                            (f"Disconnect error: {str(e)[:150]}", cid)
                        )
                        db.commit()


def _connector_loop():
    log("INFO", "[CONNECTOR] Scheduler started (15s interval)")
    while True:
        time.sleep(15)
        try:
            _run_connectors()
        except Exception as e:
            log("ERROR", f"[CONNECTOR] Scheduler error: {e}")


def start_connector_scheduler():
    t = threading.Thread(target=_connector_loop, name="connector", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Connector API routes
# ---------------------------------------------------------------------------

@app.route("/api/connectors")
def api_conn_list():
    db   = get_db()
    rows = db.execute("SELECT * FROM connectors ORDER BY name COLLATE NOCASE").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/connectors/<int:cid>")
def api_conn_get(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route("/api/connectors", methods=["POST"])
def api_conn_create():
    data = request.json or {}
    name        = str(data.get("name",           "")).strip()
    local_node  = str(data.get("local_node",     "")).strip()
    target_node = str(data.get("target_node",    "")).strip()
    connect_time = data.get("connect_time") or None
    idle_limit_sec = int(data.get("idle_limit_sec", 180))
    settle_sec     = int(data.get("settle_sec",     300))

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local node number"}), 400
    if not re.match(r'^\d{4,7}$', target_node):
        return jsonify({"error": "Invalid target node number"}), 400
    if connect_time and not re.match(r'^\d{2}:\d{2}$', connect_time):
        return jsonify({"error": "connect_time must be HH:MM"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO connectors (name, local_node, target_node, connect_time, idle_limit_sec, settle_sec) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, local_node, target_node, connect_time, idle_limit_sec, settle_sec)
    )
    db.commit()
    row = db.execute("SELECT * FROM connectors WHERE rowid=last_insert_rowid()").fetchone()
    log("INFO", f"[CONNECTOR] Created '{name}' ({local_node} → {target_node})")
    return jsonify(dict(row)), 201


@app.route("/api/connectors/<int:cid>", methods=["PATCH"])
def api_conn_update(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    name           = str(data.get("name",           row["name"])).strip()
    local_node     = str(data.get("local_node",     row["local_node"])).strip()
    target_node    = str(data.get("target_node",    row["target_node"])).strip()
    connect_time   = data.get("connect_time") or None
    idle_limit_sec = int(data.get("idle_limit_sec", row["idle_limit_sec"]))
    settle_sec     = int(data.get("settle_sec",     row["settle_sec"]))

    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local node number"}), 400
    if not re.match(r'^\d{4,7}$', target_node):
        return jsonify({"error": "Invalid target node number"}), 400
    if connect_time and not re.match(r'^\d{2}:\d{2}$', connect_time):
        return jsonify({"error": "connect_time must be HH:MM"}), 400

    db.execute(
        "UPDATE connectors SET name=?, local_node=?, target_node=?, connect_time=?, "
        "idle_limit_sec=?, settle_sec=? WHERE id=?",
        (name, local_node, target_node, connect_time, idle_limit_sec, settle_sec, cid)
    )
    db.commit()
    updated = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/connectors/<int:cid>", methods=["DELETE"])
def api_conn_delete(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM connectors WHERE id=?", (cid,))
    db.commit()
    log("INFO", f"[CONNECTOR] Deleted '{row['name']}'")
    return jsonify({"ok": True})


@app.route("/api/connectors/<int:cid>/toggle", methods=["POST"])
def api_conn_toggle(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["enabled"] else 1
    db.execute("UPDATE connectors SET enabled=? WHERE id=?", (new_val, cid))
    db.commit()
    return jsonify({"id": cid, "enabled": new_val})


@app.route("/api/connectors/<int:cid>/connect", methods=["POST"])
def api_conn_manual_connect(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Set to waiting — the scheduler will connect on its next tick when node is idle
    db.execute(
        "UPDATE connectors SET state='waiting', state_msg='Waiting for node to be idle', "
        "state_updated=? WHERE id=?", (now_str, cid)
    )
    db.commit()
    log("INFO", f"[CONNECTOR] Manual connect requested for '{row['name']}'")
    return jsonify({"ok": True, "state": "waiting"})


@app.route("/api/connectors/<int:cid>/disconnect", methods=["POST"])
def api_conn_manual_disconnect(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _connector_do_disconnect(row["local_node"], row["target_node"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    db.execute(
        "UPDATE connectors SET state='idle', state_msg='Manually disconnected', "
        "state_updated=?, connected_at=NULL, last_activity=NULL WHERE id=?",
        (now_str, cid)
    )
    db.commit()
    log("INFO", f"[CONNECTOR] Manually disconnected '{row['name']}'")
    return jsonify({"ok": True, "state": "idle"})


@app.route("/api/connectors/<int:cid>/reset", methods=["POST"])
def api_conn_reset(cid):
    """Clear error state back to idle."""
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE connectors SET state='idle', state_msg='', state_updated=?, "
        "connected_at=NULL, last_activity=NULL WHERE id=?",
        (now_str, cid)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/connectors/diagnose", methods=["POST"])
def api_conn_diagnose():
    """
    Run real smoke-tests for Smart Connector prerequisites on a given node.
    Every test actually exercises the AMI command path — no dry-run mode.
    """
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    target_node = str(data.get("target_node", "")).strip()

    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local node number"}), 400

    results = []

    def _pass(name, detail):
        results.append({"name": name, "pass": True,  "detail": detail})

    def _fail(name, detail, fix=None):
        results.append({"name": name, "pass": False, "detail": detail, "fix": fix or ""})

    def _run(ami):
        # ── Test 1: AMI command permission ──────────────────────────────────
        try:
            lines = ami.command("core show version")
            ver   = next((l for l in lines if "asterisk" in l.lower()), None)
            if ver:
                _pass("AMI command permission", ver)
            else:
                _fail("AMI command permission",
                      "No Asterisk version in response — AMI user may lack 'command' write permission.",
                      "Add 'write = command' to the AMI user in manager.conf and reload.")
        except Exception as e:
            _fail("AMI command permission", f"Command failed: {e}",
                  "Check manager.conf: user needs write = system,call,command,...")

        # ── Test 2: Node variable read (proves node exists + rpt works) ─────
        try:
            lines = ami.command(f"rpt show variables {local_node}")
            raw   = " ".join(lines)
            has_rx = "RPT_RXKEYED" in raw
            has_tx = "RPT_TXKEYED" in raw
            if has_rx and has_tx:
                rxval = next((l.split("=")[-1].strip() for l in lines if "RPT_RXKEYED" in l), "?")
                txval = next((l.split("=")[-1].strip() for l in lines if "RPT_TXKEYED" in l), "?")
                _pass(f"Node {local_node} variable read",
                      f"RPT_RXKEYED={rxval}, RPT_TXKEYED={txval} — node exists and idle detection will work")
            elif "unknown node" in raw.lower():
                _fail(f"Node {local_node} variable read",
                      f"Asterisk says 'Unknown node {local_node}'.",
                      f"Make sure [{local_node}] is in rpt.conf and Asterisk is using it.")
            else:
                _fail(f"Node {local_node} variable read",
                      f"RPT_RXKEYED/RPT_TXKEYED not found in output: {raw[:120]}",
                      "Check that app_rpt is loaded and this node is active.")
        except Exception as e:
            _fail(f"Node {local_node} variable read", f"Command error: {e}")

        # ── Test 3: rpt lstats (proves link-list is readable) ───────────────
        try:
            lines = ami.command(f"rpt lstats {local_node}")
            raw   = " ".join(lines)
            if any("NODE" in l and "PEER" in l for l in lines):
                n_links = sum(1 for l in lines
                              if re.search(r'\b\d{4,7}\b', l) and "NODE" not in l and "----" not in l)
                _pass(f"Node {local_node} link stats",
                      f"lstats readable — {n_links} active link(s) currently")
            else:
                _fail(f"Node {local_node} link stats",
                      f"Unexpected lstats output: {raw[:120]}",
                      "The auto-disconnect relies on lstats — check that rpt lstats works from the Asterisk CLI.")
        except Exception as e:
            _fail(f"Node {local_node} link stats", f"Command error: {e}")

        # ── Test 4: iLink command path ───────────────────────────────────────
        # Disconnect a node (0) that is never connected — safe no-op that
        # exercises the exact same code path as the real disconnect command.
        try:
            lines = ami.command(f"rpt cmd {local_node} ilink 1 0")
            raw   = " ".join(lines).lower()
            blocked = any(w in raw for w in
                          ["permission denied", "not permitted", "unknown command", "no permission"])
            if blocked:
                _fail("iLink command path",
                      f"Asterisk rejected the ilink command: {raw[:120]}",
                      "AMI user needs write = command. Also check that app_rpt is loaded.")
            else:
                _pass("iLink command path",
                      "rpt cmd ilink accepted by Asterisk — connect/disconnect commands will work")
        except Exception as e:
            _fail("iLink command path", f"Command error: {e}")

        # ── Test 5: Target node reachable (if provided) ─────────────────────
        if re.match(r'^\d{4,7}$', target_node):
            try:
                lines  = ami.command(f"rpt lstats {local_node}")
                linked = []
                for l in lines:
                    for n in re.findall(r'\b(\d{4,7})\b', l):
                        if n != local_node:
                            linked.append(n)
                if target_node in linked:
                    _pass(f"Target {target_node} link state",
                          f"Node {target_node} is currently connected to {local_node}")
                else:
                    _pass(f"Target {target_node} link state",
                          f"Node {target_node} is not currently connected (expected — this is a pre-connect check)")
            except Exception as e:
                _fail(f"Target {target_node} link state", f"Could not check lstats: {e}")

        return {"tests": results}

    try:
        outcome = ami_send_command(_run)
        all_pass = all(t["pass"] for t in outcome["tests"])
        return jsonify({"tests": outcome["tests"], "all_pass": all_pass})
    except Exception as e:
        return jsonify({"error": f"AMI connection failed: {e}", "tests": results}), 500


# ---------------------------------------------------------------------------
# Node ID Monitor — FCC-compliant repeater identification
# ---------------------------------------------------------------------------

_id_runtime      = {}             # {config_id: {"state", "last_id_ts", "idle_start_ts"}}
_id_runtime_lock = threading.Lock()


def _play_id_sound(row):
    node = row["node"]
    path = row["sound_path"].strip()
    cmd  = f"rpt localplay {node} {path}"
    log("INFO", f"[ID] Playing ID for '{row['name']}' on {node}: {cmd!r}")
    def _play(ami, _cmd=cmd):
        return {"output": ami.command(_cmd)}
    ami_send_command(_play)


def _run_id_monitors():
    now     = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    db   = get_db()
    rows = db.execute("SELECT * FROM id_configs WHERE enabled=1").fetchall()

    for row in rows:
        cid = row["id"]
        with _id_runtime_lock:
            if cid not in _id_runtime:
                _id_runtime[cid] = {"state": "idle", "last_id_ts": 0.0, "idle_start_ts": 0.0}
            rt = dict(_id_runtime[cid])

        active  = _node_active(row["node"])
        new_rt  = dict(rt)
        played  = False

        if active:
            new_rt["idle_start_ts"] = 0.0
            if rt["state"] == "idle":
                new_rt["state"] = "active"
                if row["initial_id"]:
                    try:
                        _play_id_sound(row)
                        new_rt["last_id_ts"] = now
                        played = True
                    except Exception as e:
                        log("ERROR", f"[ID] Initial ID failed for '{row['name']}': {e}")
            else:
                new_rt["state"] = "active"
                if rt["last_id_ts"] > 0 and (now - rt["last_id_ts"]) >= row["interval_sec"]:
                    try:
                        _play_id_sound(row)
                        new_rt["last_id_ts"] = now
                        played = True
                    except Exception as e:
                        log("ERROR", f"[ID] Interval ID failed for '{row['name']}': {e}")
        else:
            if rt["state"] == "active":
                new_rt["state"]         = "pending_idle"
                new_rt["idle_start_ts"] = now
            elif rt["state"] == "pending_idle":
                if (now - rt["idle_start_ts"]) >= row["idle_delay_sec"]:
                    try:
                        _play_id_sound(row)
                        new_rt["last_id_ts"]    = now
                        new_rt["state"]         = "idle"
                        new_rt["idle_start_ts"] = 0.0
                        played = True
                    except Exception as e:
                        log("ERROR", f"[ID] Final ID failed for '{row['name']}': {e}")

        with _id_runtime_lock:
            _id_runtime[cid] = new_rt

        if played:
            db.execute("UPDATE id_configs SET last_id_time=? WHERE id=?", (now_str, cid))
            db.commit()


def _id_monitor_loop():
    log("INFO", "[ID] Monitor started (2s interval)")
    while True:
        time.sleep(2)
        try:
            _run_id_monitors()
        except Exception as e:
            log("ERROR", f"[ID] Monitor error: {e}")


def start_id_monitor():
    t = threading.Thread(target=_id_monitor_loop, name="id-monitor", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Node ID API routes
# ---------------------------------------------------------------------------

@app.route("/api/id")
def api_id_list():
    db   = get_db()
    rows = db.execute("SELECT * FROM id_configs ORDER BY name COLLATE NOCASE").fetchall()
    now  = time.time()
    out  = []
    for row in rows:
        d = dict(row)
        with _id_runtime_lock:
            rt = dict(_id_runtime.get(row["id"], {"state": "idle", "last_id_ts": 0.0, "idle_start_ts": 0.0}))
        d["runtime_state"]  = rt["state"]
        d["last_id_ts"]     = rt["last_id_ts"]
        d["idle_start_ts"]  = rt["idle_start_ts"]
        if rt["state"] == "active" and rt["last_id_ts"] > 0:
            d["next_id_sec"] = max(0, row["interval_sec"] - int(now - rt["last_id_ts"]))
        else:
            d["next_id_sec"] = None
        if rt["state"] == "pending_idle" and rt["idle_start_ts"] > 0:
            d["final_id_sec"] = max(0, row["idle_delay_sec"] - int(now - rt["idle_start_ts"]))
        else:
            d["final_id_sec"] = None
        out.append(d)
    return jsonify(out)


@app.route("/api/id", methods=["POST"])
def api_id_create():
    data = request.json or {}
    name           = str(data.get("name",           "")).strip()
    node           = str(data.get("node",           "")).strip()
    sound_path     = str(data.get("sound_path",     "")).strip()
    interval_sec   = int(data.get("interval_sec",   600))
    idle_delay_sec = int(data.get("idle_delay_sec", 120))
    initial_id     = 1 if data.get("initial_id", True) else 0

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400
    if not sound_path:
        return jsonify({"error": "Sound path is required"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO id_configs (name, node, sound_path, interval_sec, idle_delay_sec, initial_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, node, sound_path, interval_sec, idle_delay_sec, initial_id)
    )
    db.commit()
    row = db.execute("SELECT * FROM id_configs WHERE rowid=last_insert_rowid()").fetchone()
    log("INFO", f"[ID] Created '{name}' (node={node}, sound={sound_path})")
    return jsonify(dict(row)), 201


@app.route("/api/id/<int:iid>", methods=["PATCH"])
def api_id_update(iid):
    db  = get_db()
    row = db.execute("SELECT * FROM id_configs WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    name           = str(data.get("name",           row["name"])).strip()
    node           = str(data.get("node",           row["node"])).strip()
    sound_path     = str(data.get("sound_path",     row["sound_path"])).strip()
    interval_sec   = int(data.get("interval_sec",   row["interval_sec"]))
    idle_delay_sec = int(data.get("idle_delay_sec", row["idle_delay_sec"]))
    initial_id     = 1 if data.get("initial_id", bool(row["initial_id"])) else 0

    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400
    if not sound_path:
        return jsonify({"error": "Sound path is required"}), 400

    db.execute(
        "UPDATE id_configs SET name=?, node=?, sound_path=?, interval_sec=?, "
        "idle_delay_sec=?, initial_id=? WHERE id=?",
        (name, node, sound_path, interval_sec, idle_delay_sec, initial_id, iid)
    )
    db.commit()
    updated = db.execute("SELECT * FROM id_configs WHERE id=?", (iid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/id/<int:iid>", methods=["DELETE"])
def api_id_delete(iid):
    db  = get_db()
    row = db.execute("SELECT * FROM id_configs WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM id_configs WHERE id=?", (iid,))
    db.commit()
    with _id_runtime_lock:
        _id_runtime.pop(iid, None)
    log("INFO", f"[ID] Deleted '{row['name']}'")
    return jsonify({"ok": True})


@app.route("/api/id/<int:iid>/toggle", methods=["POST"])
def api_id_toggle(iid):
    db  = get_db()
    row = db.execute("SELECT * FROM id_configs WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["enabled"] else 1
    db.execute("UPDATE id_configs SET enabled=? WHERE id=?", (new_val, iid))
    db.commit()
    # Reset runtime state when disabling
    if not new_val:
        with _id_runtime_lock:
            _id_runtime.pop(iid, None)
    return jsonify({"id": iid, "enabled": new_val})


@app.route("/api/id/<int:iid>/play", methods=["POST"])
def api_id_play(iid):
    db  = get_db()
    row = db.execute("SELECT * FROM id_configs WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        _play_id_sound(row)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE id_configs SET last_id_time=? WHERE id=?", (now_str, iid))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/id/upload", methods=["POST"])
def api_id_upload():
    """Upload and convert a sound file; returns the Asterisk-relative path for use in sound_path."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return jsonify({"error": f"Unsupported type: {ext}"}), 400

    name      = request.form.get("name", os.path.splitext(f.filename)[0]).strip() or "id-sound"
    base_slug = _ann_slug(name)
    slug      = _ann_unique_slug(base_slug)
    dest      = _ann_sound_path(slug)

    os.makedirs(SOUNDS_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        _convert_to_ulaw(tmp_path, dest)
    except Exception as e:
        return jsonify({"error": f"Conversion failed: {e}"}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    try:
        shutil.chown(dest, user="asterisk", group="asterisk")
        os.chmod(dest, 0o640)
    except Exception:
        pass

    log("INFO", f"[ID] Uploaded ID sound '{name}' → {dest}")
    return jsonify({"path": f"asl3ez/{slug}", "slug": slug})


# ---------------------------------------------------------------------------
# DTMF Control
# ---------------------------------------------------------------------------

_DTMF_VALID = re.compile(r'^[0-9A-Da-d*#]+$')

@app.route("/api/dtmf/send", methods=["POST"])
def api_dtmf_send():
    data   = request.get_json(force=True)
    node   = str(data.get("node", "")).strip()
    digits = str(data.get("digits", "")).strip().upper()
    if not node.isdigit():
        return jsonify({"error": "node must be numeric"}), 400
    if not digits or not _DTMF_VALID.match(digits):
        return jsonify({"error": "digits must contain only 0-9 A-D * #"}), 400
    def _send(ami):
        lines = ami.command(f"rpt cmd {node} dtmf {digits}")
        raw   = "\n".join(lines)
        return {"ok": True, "raw": raw}
    try:
        result = ami_send_command(_send)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dtmf/cop", methods=["POST"])
def api_dtmf_cop():
    data     = request.get_json(force=True)
    node     = str(data.get("node", "")).strip()
    function = str(data.get("function", "")).strip()
    if not node.isdigit():
        return jsonify({"error": "node must be numeric"}), 400
    if not function.isdigit():
        return jsonify({"error": "function must be a positive integer"}), 400
    def _send(ami):
        lines = ami.command(f"rpt cmd {node} cop {function}")
        raw   = "\n".join(lines)
        return {"ok": True, "raw": raw}
    try:
        result = ami_send_command(_send)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Startup — load node DB and start background AMI poller
# These run regardless of whether we're under gunicorn or direct python.
# ---------------------------------------------------------------------------
load_astdb()
_db_conn_startup_cleanup()
start_poller()
start_favstats_poller()
start_global_activity_poller()
start_announcer()
start_connector_scheduler()
start_id_monitor()

if __name__ == "__main__":
    log("INFO", "Starting in direct-run mode (not via gunicorn)")
    app.run(host=HOST, port=PORT, debug=False)
