#!/usr/bin/env python3
"""
HenWen - AllStarLink 3 rpt.conf Editor + Node Control
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
import html
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
import traceback
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, Response, stream_with_context
from flask.sessions import SecureCookieSessionInterface
import difflib
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

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
SECRET_KEY      = os.environ.get("SECRET_KEY",      "henwen-change-me")
PORT            = int(os.environ.get("PORT",         5000))
HOST            = os.environ.get("HOST",             "0.0.0.0")
DB_PATH         = os.environ.get("DB_PATH",          "/etc/asterisk/henwen.db")
AMI_HOST        = os.environ.get("AMI_HOST",         "127.0.0.1")
AMI_PORT        = int(os.environ.get("AMI_PORT",     5038))
SERVICE_NAME    = os.environ.get("SERVICE_NAME",     "HenWen")
# Subdirectory name under Asterisk's sounds dir — single source of truth,
# used both for SOUNDS_DIR's default and for the "henwen/<slug>" prefix
# baked into every rpt localplay/playback AMI command elsewhere in this
# file, so the two can never silently drift out of sync with each other.
SOUNDS_SUBDIR   = "henwen"
SOUNDS_DIR      = os.environ.get("SOUNDS_DIR",       f"/usr/share/asterisk/sounds/{SOUNDS_SUBDIR}")
# Piper voice models (.onnx/.onnx.json) — NOT under SOUNDS_DIR: these are
# Piper's own assets, not Asterisk sound files, and don't need asterisk's
# sound-format/ownership conventions. Also NOT under /opt/HenWen: that
# directory is root:root and the service runs as User=asterisk, so it can't
# write there. /var/lib/asterisk is already asterisk:asterisk.
TTS_VOICES_DIR  = os.environ.get("TTS_VOICES_DIR",   "/var/lib/asterisk/henwen_tts_voices")
# Resolved via the running interpreter's own directory, not PATH lookup —
# under systemd, venv/bin isn't on PATH the way it is in an interactive
# shell, so a bare "piper" subprocess call would fail with FileNotFoundError.
PIPER_BIN       = os.environ.get("PIPER_BIN",
                                  os.path.join(os.path.dirname(sys.executable), "piper"))
_DEFAULT_SERVICE_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
SERVICE_FILE_PATH = os.environ.get("SERVICE_FILE_PATH", _DEFAULT_SERVICE_FILE_PATH)
SECURE_COOKIES       = os.environ.get("SECURE_COOKIES", "false").lower() == "true"
SESSION_IDLE_TIMEOUT = int(os.environ.get("SESSION_IDLE_TIMEOUT", "1800"))  # 30 minutes

# SECRET_KEY values that ship with the app/installer — used to warn the user
# in the dashboard that they're still on the default and should change it.
DEFAULT_SECRET_KEYS = {"", "asl3-ez-change-me", "asl3-ez-change-me-in-production",
                       "henwen-change-me", "henwen-change-me-in-production"}

GITHUB_REPO = "GooseThings/HenWen"


def _load_henwen_version():
    """Read the top version header (## vYYYY.MM.DD, optionally .N for a
    same-day second release) from CHANGELOG.md, shipped alongside app.py —
    used for the kiosk footer and the Manager's update check. Falls back to
    'unknown' for a dev checkout without a stamped release yet."""
    try:
        changelog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
        with open(changelog_path) as f:
            for line in f:
                m = re.match(r'^##\s+(v\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)\s*$', line.strip())
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


HENWEN_VERSION = _load_henwen_version()

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
SUDO_PATH       = "/usr/bin/sudo"
SYSTEMD_RUN_PATH = "/usr/bin/systemd-run"
if not os.path.exists(SYSTEMD_RUN_PATH):
    SYSTEMD_RUN_PATH = "/bin/systemd-run"
UPDATE_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.sh")
ROTATE_SECRET_KEY_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rotate_secret_key.sh")
UPDATE_SERVICE_PORTS_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_service_ports.sh")


def _systemctl(*args, timeout=30):
    """Run systemctl, elevating via a narrowly-scoped passwordless sudo rule
    when not already root. The service normally runs unprivileged as
    `asterisk` (HenWen.service User=asterisk); install.sh installs a
    sudoers drop-in permitting exactly the daemon-reload/restart
    invocations this function is used for — restarting `asterisk` or this
    service and reloading unit files. `sudo -n` fails fast (no password
    prompt) if that rule is missing, so callers see a clear stderr message
    instead of hanging."""
    if os.geteuid() == 0:
        cmd = [SYSTEMCTL_PATH, *args]
    else:
        cmd = [SUDO_PATH, "-n", SYSTEMCTL_PATH, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

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
app.config['SESSION_COOKIE_SECURE']   = SECURE_COOKIES
app.config['WTF_CSRF_TIME_LIMIT']     = None  # tokens don't expire; rely on session lifetime

csrf    = CSRFProtect(app)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])


# ---------------------------------------------------------------------------
# Local TLS proxy support
#
# Remote HTTPS access runs through an Apache vhost on this same machine that
# proxies to 127.0.0.1:5000. Honor X-Forwarded-For/-Proto only when the TCP
# peer is that local proxy, so the login rate limiter, active-session
# tracking, and [AUDIO] logs see the real client address — while a client
# connecting straight to :5000 can't spoof those headers to dodge the
# limiter.
# ---------------------------------------------------------------------------
class _LocalProxyFix:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if environ.get('REMOTE_ADDR') in ('127.0.0.1', '::1'):
            xff = environ.get('HTTP_X_FORWARDED_FOR', '')
            if xff:
                # The last entry is the one Apache itself appended (its actual
                # TCP peer); anything before it is client-supplied and untrusted.
                environ['REMOTE_ADDR'] = xff.split(',')[-1].strip()
            proto = environ.get('HTTP_X_FORWARDED_PROTO', '')
            if proto in ('http', 'https'):
                environ['wsgi.url_scheme'] = proto
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _LocalProxyFix(app.wsgi_app)


class _SchemeAwareSessionInterface(SecureCookieSessionInterface):
    """Mark the session cookie Secure whenever the *actual* request reached us
    over HTTPS (including via the local Apache TLS proxy, whose scheme
    _LocalProxyFix already restores from X-Forwarded-Proto), even though
    SECURE_COOKIES defaults to false to keep plain-LAN HTTP access working.
    If SECURE_COOKIES is explicitly set, that always wins (HTTPS-only
    deployments)."""
    def get_cookie_secure(self, app):
        if SECURE_COOKIES:
            return True
        return request.is_secure


app.session_interface = _SchemeAwareSessionInterface()

# ---------------------------------------------------------------------------
# Logging  (verbose, timestamp-prefixed, written to stdout for journald)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()

# In-memory ring of recent HenWen log lines, mirrored from log() below, so the
# manager Dashboard's Diagnostics panel can show the service's own diagnostics
# without needing journald read access (the service runs as the unprivileged
# 'asterisk' user, which usually can't read the journal). Holds only lines that
# pass the LOG_LEVEL filter — set LOG_LEVEL=DEBUG in the service file to capture
# DEBUG lines here too. See /api/diagnostics.
_LOG_RING = deque(maxlen=1500)   # each: {"t": epoch_float, "level": str, "msg": str}

def log(level, msg):
    if _LOG_LEVELS.get(level, 1) < _LOG_LEVELS.get(LOG_LEVEL, 1):
        return
    now = time.time()
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = f"[{ts}] [{level}] {msg}"
    with _log_lock:
        print(out, flush=True)
        _LOG_RING.append({"t": now, "level": level, "msg": msg})

log("INFO", "HenWen starting up")
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
_db_ready = False   # flips True after the schema/migrations run once


def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    # timeout= is SQLite's busy wait. Several background threads (AMI poll
    # loop, announcer, connector scheduler, NWS poller) plus request handlers
    # all write to this one file, so a writer routinely queues behind another
    # thread's transaction — wait it out rather than surfacing "database is
    # locked" at the default 5s.
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # The schema/migration statements below are idempotent but not free, and
    # get_db() is called many times per second (poll loop, get_setting(), every
    # request). Run them once, then hand back a bare connection. A cold-start
    # race between first callers is harmless — every statement is CREATE TABLE
    # IF NOT EXISTS or a guarded one-shot migration.
    if _db_ready:
        return conn
    # WAL lets readers proceed while a writer commits and sharply reduces
    # writer-vs-writer contention across this app's many threads. The setting
    # is persistent per database file, so once is enough; it can fail if
    # another connection happens to hold a lock at this exact instant, in
    # which case the next cold start sets it — never worth failing startup.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
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
    cols = [r[1] for r in conn.execute("PRAGMA table_info(favorites)").fetchall()]
    if 'sort_order' not in cols:
        # NULL for pre-existing rows; backfilled immediately below using each
        # row's id order (today's implicit display order) so the new custom
        # drag-to-reorder list starts out matching what users already see
        # instead of every row collapsing to the same rank.
        conn.execute("ALTER TABLE favorites ADD COLUMN sort_order INTEGER")
        conn.execute("""UPDATE favorites SET sort_order = (
            SELECT COUNT(*) FROM favorites f2
            WHERE f2.user_id = favorites.user_id AND f2.id <= favorites.id
        )""")
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
        created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        idle_settle_sec INTEGER NOT NULL DEFAULT 30,
        tts_text      TEXT,
        tts_voice     TEXT    NOT NULL DEFAULT 'en_US-lessac-medium',
        external_id   TEXT,
        nws_expires   TEXT,
        max_defer_sec INTEGER
    )""")
    _ann_cols = {r[1] for r in conn.execute("PRAGMA table_info(announcements)").fetchall()}
    if 'idle_settle_sec' not in _ann_cols:
        conn.execute("ALTER TABLE announcements ADD COLUMN idle_settle_sec INTEGER NOT NULL DEFAULT 30")
    if 'tts_text' not in _ann_cols:
        conn.execute("ALTER TABLE announcements ADD COLUMN tts_text TEXT")
    if 'tts_voice' not in _ann_cols:
        # Must match DEFAULT_TTS_VOICE below.
        conn.execute("ALTER TABLE announcements ADD COLUMN tts_voice TEXT NOT NULL DEFAULT 'en_US-lessac-medium'")
    if 'external_id' not in _ann_cols:
        # Dedup/lifecycle key for auto-sourced rows (e.g. NWS alerts' parsed
        # VTEC identity). NULL for upload/tts rows.
        conn.execute("ALTER TABLE announcements ADD COLUMN external_id TEXT")
    if 'nws_expires' not in _ann_cols:
        # The alert's own expiry time, used as a local/network-independent
        # ceiling so a stale alert can't replay forever if the NWS poller
        # can't reach the API to confirm it has ended.
        conn.execute("ALTER TABLE announcements ADD COLUMN nws_expires TEXT")
    if 'max_defer_sec' not in _ann_cols:
        # NULL (default) preserves today's indefinite idle-settle defer for
        # every existing announcement. Only NWS-sourced rows set this, to
        # force playback after being due for this long even if the channel
        # hasn't gone idle-settled yet — see _run_due_announcements().
        conn.execute("ALTER TABLE announcements ADD COLUMN max_defer_sec INTEGER")
    conn.commit()
    # Durable record of connections made Permanent through this app, so the
    # Status Board's Permanent badge survives a HenWen restart — app_rpt
    # itself keeps the link genuinely permanent across a restart of this web
    # app (Asterisk was never touched), but _kiosk_temp_conns is in-process
    # memory only and would otherwise forget, making the badge vanish even
    # though the underlying link is unchanged. See _rehydrate_permanent_links().
    conn.execute("""CREATE TABLE IF NOT EXISTS permanent_links (
        local_node TEXT    NOT NULL,
        peer_node  TEXT    NOT NULL,
        monitor    INTEGER NOT NULL DEFAULT 0,
        created_at TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (local_node, peer_node)
    )""")
    conn.commit()
    # Same story for temporary (non-permanent) kiosk connections: their
    # idle-timeout state (last_active, plus the No Timeout exemption) lived
    # only in _kiosk_temp_conns, so a HenWen restart both hid the Idle badge
    # and silently exempted the link from ever timing out. See
    # _rehydrate_kiosk_temp_conns().
    conn.execute("""CREATE TABLE IF NOT EXISTS kiosk_temp_conns (
        local_node  TEXT    NOT NULL,
        peer_node   TEXT    NOT NULL,
        monitor     INTEGER NOT NULL DEFAULT 0,
        no_timeout  INTEGER NOT NULL DEFAULT 0,
        last_active REAL    NOT NULL,
        PRIMARY KEY (local_node, peer_node)
    )""")
    conn.commit()
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
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        schedule_type   TEXT    NOT NULL DEFAULT 'daily',
        schedule_days   TEXT    NOT NULL DEFAULT '',
        disconnect_all_first INTEGER NOT NULL DEFAULT 0,
        disconnect_skip_permanent INTEGER NOT NULL DEFAULT 0
    )""")
    # Migrate older rows that lack the new schedule columns
    _conn_cols = {r[1] for r in conn.execute("PRAGMA table_info(connectors)").fetchall()}
    if 'schedule_type' not in _conn_cols:
        conn.execute("ALTER TABLE connectors ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'daily'")
    if 'schedule_days' not in _conn_cols:
        conn.execute("ALTER TABLE connectors ADD COLUMN schedule_days TEXT NOT NULL DEFAULT ''")
    if 'disconnect_all_first' not in _conn_cols:
        conn.execute("ALTER TABLE connectors ADD COLUMN disconnect_all_first INTEGER NOT NULL DEFAULT 0")
    if 'disconnect_skip_permanent' not in _conn_cols:
        conn.execute("ALTER TABLE connectors ADD COLUMN disconnect_skip_permanent INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS id_configs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        node            TEXT    NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        sound_path      TEXT    NOT NULL DEFAULT 'henwen/my-id',
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
        watch_nodes        TEXT    NOT NULL DEFAULT '',
        on_dns_stuck       INTEGER NOT NULL DEFAULT 1
    )""")
    _alert_cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_config)").fetchall()}
    if 'on_dns_stuck' not in _alert_cols:
        conn.execute("ALTER TABLE alert_config ADD COLUMN on_dns_stuck INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    # Singleton config for the NWS severe weather auto-announcement poller —
    # separate from alert_config (that's push notifications; this is
    # auto-playing TTS on the repeater itself, a different concept).
    conn.execute("""CREATE TABLE IF NOT EXISTS nws_alert_config (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        enabled            INTEGER NOT NULL DEFAULT 0,
        zone               TEXT    NOT NULL DEFAULT '',
        zone_area_name     TEXT    NOT NULL DEFAULT '',
        zone_auto_detected INTEGER NOT NULL DEFAULT 0,
        node               TEXT    NOT NULL DEFAULT '',
        play_cmd           TEXT    NOT NULL DEFAULT 'localplay',
        voice              TEXT    NOT NULL DEFAULT 'en_US-lessac-medium',
        interval_min       INTEGER NOT NULL DEFAULT 15,
        idle_settle_sec    INTEGER NOT NULL DEFAULT 10,
        max_defer_sec      INTEGER NOT NULL DEFAULT 120,
        event_types        TEXT    NOT NULL DEFAULT 'Tornado Warning,Severe Thunderstorm Warning',
        last_poll_at       TEXT,
        last_poll_error    TEXT
    )""")
    conn.commit()
    # Per-node lockout: presence of a row means that node is locked by its
    # Owner. Only one lockout state per node, so `node` is the primary key
    # rather than an autoincrement id.
    conn.execute("""CREATE TABLE IF NOT EXISTS node_lockouts (
        node       TEXT PRIMARY KEY,
        locked_by  TEXT NOT NULL DEFAULT '',
        locked_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    # Kiosk-editable scheduled-net reminders — shared across all visitors (not
    # per-user like favorites), since a net schedule is repeater-wide info.
    # A row is either 'weekly' (repeats every week on `weekday`, 0=Monday..
    # 6=Sunday) or 'once' (a single date in `net_date`). `time` is always
    # kiosk-local HH:MM: the frontend already knows the kiosk's configured
    # timezone (same Intl-based logic the clock itself uses), so "is this
    # scheduled for today / how many minutes away" is computed client-side
    # rather than duplicating timezone math here.
    conn.execute("""CREATE TABLE IF NOT EXISTS net_schedules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        node       TEXT    NOT NULL DEFAULT '',
        recurrence TEXT    NOT NULL DEFAULT 'weekly',
        weekday    INTEGER,
        net_date   TEXT,
        time       TEXT    NOT NULL,
        end_time   TEXT,
        notes      TEXT    NOT NULL DEFAULT '',
        created_by TEXT    NOT NULL DEFAULT '',
        created_at TEXT    DEFAULT (datetime('now'))
    )""")
    _net_cols = {r[1] for r in conn.execute("PRAGMA table_info(net_schedules)").fetchall()}
    if 'end_time' not in _net_cols:
        conn.execute("ALTER TABLE net_schedules ADD COLUMN end_time TEXT")
    conn.commit()
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
    # Per-user session idle timeout column (NULL = use global SESSION_IDLE_TIMEOUT)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN session_idle_timeout INTEGER DEFAULT NULL")
        conn.commit()
    except Exception:
        pass  # column already exists
    # Restricted 'user' accounts (User Management → "Restrict disconnect")
    # can listen/TX and make the first connection but can never disconnect a
    # node — 0 (default) preserves today's behavior for every existing account.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN restrict_disconnect INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists

    # Seed kiosk defaults
    for _k, _v in [
        ('kiosk_idle_timeout_sec', '600'),
        ('kiosk_clock_format',     '12'),
        ('kiosk_timezone',         'UTC'),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (_k, _v))
    conn.commit()
    globals()['_db_ready'] = True
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
# Node lockout (Owner-only)
# ---------------------------------------------------------------------------
def get_locked_nodes():
    """{node: {'locked_by': ..., 'locked_at': ...}} for every currently-locked node."""
    rows = get_db().execute("SELECT node, locked_by, locked_at FROM node_lockouts").fetchall()
    return {r["node"]: {"locked_by": r["locked_by"], "locked_at": r["locked_at"]} for r in rows}


def is_any_node_locked():
    return get_db().execute("SELECT 1 FROM node_lockouts LIMIT 1").fetchone() is not None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# Role hierarchy, low to high. Used to stop a caller from assigning or
# managing an account at a role above their own (e.g. a Superuser granting
# themselves or someone else 'owner').
ROLE_RANK = {'user': 0, 'admin': 1, 'superuser': 2, 'owner': 3}


def is_auth_configured():
    try:
        row = get_db().execute(
            "SELECT id FROM users WHERE role IN ('owner', 'superuser')").fetchone()
        return row is not None
    except Exception:
        return False


@app.before_request
def check_auth():
    _PUBLIC          = {'login', 'logout', 'static', None,
                        'status_board', 'status_board_redirect',
                        'api_status_board', 'api_status_weather', 'api_status_activity',
                        'api_status_history',
                        'api_login', 'api_session', 'api_csrf_token',
                        'api_favorites', 'api_favorites_status',
                        'api_kiosk_settings_get',
                        'api_nets_list'}
    # Any logged-in user (superuser / admin / user) — live audio requires a
    # session (previously public, letting anyone on the network listen and
    # spawn server-side encoder/relay processes with no authentication).
    _USER_OR_ABOVE = {'api_status_connect', 'api_status_disconnect',
                      'api_fav_add', 'api_fav_delete', 'api_fav_label',
                      'api_audio_stream', 'api_audio_check', 'api_audio_stop',
                      'api_audio_client_log',
                      'api_nets_create', 'api_nets_update', 'api_nets_delete',
                      'api_echolink_search', 'api_asl_search',
                      'api_tx_config'}

    endpoint  = request.endpoint
    is_public = endpoint in _PUBLIC

    if not is_auth_configured():
        if is_public:
            return None
        if request.path.startswith('/api/'):
            return jsonify({"error": "Setup required", "setup_url": "/login"}), 503
        return redirect(url_for('login'))

    logged_in = session.get('logged_in')
    role      = session.get('role', '')

    # Idle session timeout: expire sessions inactive for too long.
    # idle_timeout is stored in the session at login (per-user value or global default).
    # 0 means never expire.
    #
    # This — and the active-session touch below — must run for every
    # logged-in request, *including* public endpoints. The Status Board's
    # normal polling (api_status_board, api_status_activity, favorites,
    # etc.) is all public, since the board itself needs no login. A kiosk/
    # user account that just sits on the board and never hits a protected
    # endpoint would otherwise never get touched, so it'd never show up in
    # the "Logged in" footer count and would never idle-expire on its own.
    if logged_in:
        now          = time.time()
        last_active  = session.get('last_active', now)
        idle_timeout = session.get('idle_timeout', SESSION_IDLE_TIMEOUT)
        if idle_timeout and now - last_active > idle_timeout:
            remove_active_session(session.get('sid'))
            session.clear()
            logged_in = False
            if not is_public:
                if request.path.startswith('/api/'):
                    return jsonify({"error": "Session expired", "timeout": True}), 401
                return redirect(url_for('login'))
        else:
            session['last_active'] = now
            sid = session.get('sid')
            if not sid:
                sid = secrets.token_hex(16)
                session['sid'] = sid
            touch_active_session(sid, session.get('username', ''), session.get('role', ''))

    if is_public:
        return None

    # Refresh role from DB if missing or if 'admin' (the pre-v3 role that was
    # migrated to 'superuser' in the DB but not in existing sessions).
    if logged_in and (not role or role == 'admin'):
        username = session.get('username', '')
        if username:
            row = get_db().execute("SELECT role FROM users WHERE username=?",
                                   (username,)).fetchone()
            if row and row['role'] != role:
                role = row['role']
                session['role'] = role

    # Node lockout: while the Owner has locked any node, every other role
    # (including Superuser/Admin) is read-only system-wide — only GET/HEAD/
    # OPTIONS requests pass. This is a stricter, cross-cutting rule layered
    # on top of every endpoint's own role check below, so it's enforced here
    # once rather than at each of the ~30 individual gates scattered through
    # the route functions. Public endpoints (including login/logout) already
    # returned above and are never touched by this.
    if logged_in and role != 'owner' and request.method not in ('GET', 'HEAD', 'OPTIONS') \
            and is_any_node_locked():
        return jsonify({"error": "Locked by the node owner", "locked": True}), 423

    if endpoint in _USER_OR_ABOVE:
        if not logged_in:
            return jsonify({"error": "Authentication required"}), 401
        return None

    # Everything else requires admin or superuser
    if not logged_in:
        if request.path.startswith('/api/'):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for('login'))

    if role not in ('admin', 'superuser', 'owner'):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Admin access required"}), 403
        return redirect(url_for('status_board'))

    return None


@app.after_request
def _no_cache_auth_pages(resp):
    """
    Login, logout, and the Manager shell must never be served from the
    browser's cache — a cached copy of the Manager taken while logged in
    would still render as logged in after a real logout (the page itself
    never re-hits the server to notice the session is gone), and a cached
    login page would carry a stale CSRF token tied to a session that no
    longer exists.
    """
    if request.endpoint in ('login', 'logout', 'index'):
        resp.headers['Cache-Control'] = 'no-store'
    elif request.endpoint in ('status_board', 'status_board_redirect'):
        # The Status Board is a self-contained SPA with all its JS inline —
        # served without this, browsers heuristically cache it and keep
        # running stale player code long after a fix is deployed.
        resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour")
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
            # The very first account is the node Owner — top of the role
            # hierarchy, since there's no one else yet to have granted it.
            db.execute("INSERT OR REPLACE INTO users (username, password_hash, role) VALUES (?,?,?)",
                       (username, generate_password_hash(password), "owner"))
            db.commit()
            new_user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            session.clear()
            session["logged_in"]    = True
            session["username"]     = username
            session["role"]         = "owner"
            session["user_id"]      = new_user["id"]
            session["idle_timeout"] = new_user["session_idle_timeout"] if new_user["session_idle_timeout"] is not None else SESSION_IDLE_TIMEOUT
            session["sid"] = secrets.token_hex(16)
            touch_active_session(session["sid"], username)
            _seed_default_favorites(new_user["id"])
            log("INFO", f"[AUTH] Initial account created for '{username}' (role=owner)")
            return redirect(url_for('index'))

        # Normal login
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["logged_in"]    = True
            session["username"]     = username
            session["role"]         = user["role"]
            session["user_id"]      = user["id"]
            session["idle_timeout"] = user["session_idle_timeout"] if user["session_idle_timeout"] is not None else SESSION_IDLE_TIMEOUT
            session["sid"] = secrets.token_hex(16)
            touch_active_session(session["sid"], username)
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


@app.route("/logout", methods=["POST"])
def logout():
    username = session.get("username", "unknown")
    remove_active_session(session.get("sid"))
    session.clear()
    log("INFO", f"[AUTH] Logout: '{username}'")
    return redirect(url_for('login'))


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def api_login():
    """JSON login for the Kiosk inline modal."""
    data     = request.json or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    user     = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        session.clear()
        session["logged_in"] = True
        session["username"]  = username
        session["role"]         = user["role"]
        session["user_id"]      = user["id"]
        session["idle_timeout"] = user["session_idle_timeout"] if user["session_idle_timeout"] is not None else SESSION_IDLE_TIMEOUT
        session["sid"] = secrets.token_hex(16)
        touch_active_session(session["sid"], username)
        log("INFO", f"[AUTH] API Login: '{username}' (role={user['role']})")
        # session.clear() above invalidated the CSRF token that was baked into
        # the already-loaded status page meta tag.  Return the fresh token so
        # the JS can update _csrfToken without requiring a full page reload.
        return jsonify({"ok": True, "role": user["role"], "username": username,
                        "restrict_disconnect": bool(user["restrict_disconnect"]),
                        "csrf_token": generate_csrf()})
    log("WARN", f"[AUTH] API Login failed for '{username}'")
    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/csrf-token")
def api_csrf_token():
    """Return a fresh CSRF token for the current session (GET, so no CSRF check needed).
    Used by the kiosk after logout to refresh _csrfToken without reloading the page."""
    return jsonify({"csrf_token": generate_csrf()})


@app.route("/api/session")
def api_session():
    """Public endpoint — returns current session state for the kiosk page."""
    if session.get("logged_in"):
        row = get_db().execute("SELECT restrict_disconnect FROM users WHERE id=?",
                               (session.get("user_id"),)).fetchone()
        return jsonify({
            "logged_in": True,
            "username":  session.get("username", ""),
            "role":      session.get("role", ""),
            "restrict_disconnect": bool(row["restrict_disconnect"]) if row else False,
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
        "in /etc/systemd/system/HenWen.service then: "
        "systemctl daemon-reload && systemctl restart HenWen")
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
        """Read until `sentinel` arrives, or raise ConnectionError.

        Raising (rather than returning whatever partial data was received by
        the deadline) matters for protocol integrity: after a silent partial
        return, the real response could still straggle in and sit in the
        socket buffer, where it would be consumed as the answer to the NEXT
        action — every response off by one from then on, with nothing ever
        noticing. An exception instead propagates to ami_send_command() /
        _poll_loop(), which invalidate the connection and reconnect,
        resynchronizing the stream. Socket errors propagate for the same
        reason."""
        deadline = time.time() + (timeout or self.timeout)
        buf = ""
        closed = False
        self._sock.settimeout(0.5)
        try:
            while time.time() < deadline:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    closed = True
                    break
                buf += chunk.decode("utf-8", errors="replace")
                if sentinel in buf:
                    return buf
        finally:
            self._sock.settimeout(self.timeout)
        why = "connection closed" if closed else "timed out"
        raise ConnectionError(f"AMI: {why} waiting for response terminator "
                              f"({len(buf)} byte(s) received)")

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

        try:
            banner = self._recv_until("\r\n", timeout=5).strip()
        except ConnectionError:
            banner = ""   # fall through to the friendlier diagnostic below
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
                f"  Fix:    Set AMI_USER and AMI_SECRET in HenWen.service to match\n"
                f"          the [username] and secret= in /etc/asterisk/manager.conf\n"
                f"  Then:   systemctl daemon-reload && systemctl restart HenWen"
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

    def module_load(self, module: str) -> bool:
        """
        Load an Asterisk module via the dedicated ModuleLoad AMI action.

        The generic Command action blacklists CLI-equivalent commands for
        security reasons — "Command: module load <mod>" comes back
        "Response: Error / Message: Command blacklisted" rather than
        actually loading anything, even though "module show"/other
        read-only CLI commands work fine through Command. ModuleLoad is a
        first-class AMI action specifically carved out for this and isn't
        blacklisted. Verified live against this Asterisk build.
        """
        log("INFO", f"[AMI] ModuleLoad: {module!r}")
        self._send_action({'Action': 'ModuleLoad', 'Module': module, 'LoadType': 'load'})
        raw = self._recv_until('\r\n\r\n', timeout=self.timeout)
        pkt = self._parse_packet(raw)
        ok  = pkt.get('Response') == 'Success'
        if not ok:
            log("WARN", f"[AMI] ModuleLoad failed for {module!r}: {pkt.get('Message', raw[:200])}")
        return ok

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
                  "link_connect_seconds": {}, "link_direction": {}, "link_connect_state": {}}

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
                # CONNECT_TIME is the 5th column (index 4). Confirmed against
                # app_rpt's rpt_do_lstats (rpt_cli.c): snprintf(conntime, 20,
                # "%02d:%02d:%02d:%02d", hours, minutes, seconds, <sub-second>)
                # — four colon-separated fields, not three. Asterisk itself
                # tracks this counter from the moment the link came up, so
                # re-reading it fresh on every poll makes elapsed connect time
                # immune to both a HenWen restart and a kiosk browser refresh —
                # there's no HenWen-side or client-side state to lose.
                m_ct = re.match(r'^(\d+):(\d{2}):(\d{2}):\d+$', parts[4])
                if m_ct:
                    h, mnt, s = int(m_ct.group(1)), int(m_ct.group(2)), int(m_ct.group(3))
                    status["link_connect_seconds"][cn] = h * 3600 + mnt * 60 + s
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
            # Deferred DB work — collected while holding the AMI lock, executed
            # only after it's released. A SQLite write can sit on the busy
            # timeout when another thread's transaction is in flight; doing
            # that inside _ami_pool_lock froze every AMI command (kiosk
            # connects, the announcer, Smart Connector) behind a database
            # stall.
            _conn_opens   = []  # (local_node_str, peer, direction)
            _conn_closes  = []  # (local_node_str, peer)
            _link_prunes  = []  # (local_node_str, peer) — permanent/temp tracking rows to drop
            _idle_saves   = []  # (local_node_str, peer, monitor, last_active)
            _idle_deletes = []  # (local_node_str, peer)
            # Read once per cycle, before taking the lock — get_setting() opens
            # a DB connection of its own.
            idle_timeout = int(get_setting('kiosk_idle_timeout_sec', '600') or 600)
            # Union of every locally-hosted node's connected peers this cycle —
            # _link_stats is shared across ALL local nodes, so pruning it must
            # wait until every node has been polled (see below).
            _all_current_linked = set()

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
                            with _own_keyups_lock:
                                _own_keyups[node] = _own_keyups.get(node, 0) + 1
                        _keyed_prev_states[own_key] = bool(status.get("keyed"))

                        # Idle-settle tracking for announcements: "active" means the
                        # node's own RX is keyed OR any linked node is keyed (same
                        # definition as _node_active(), computed here directly from
                        # the status we just fetched instead of re-reading the cache).
                        if status.get("keyed") or any(
                            l.get("keyed", False) for l in status.get("links", {}).values()
                        ):
                            with _node_last_active_lock:
                                _node_last_active[node] = now_ts

                        current_linked = set(status.get("connected", []))
                        _all_current_linked |= current_linked
                        with _link_stats_lock:
                            # Ensure entry exists for each connected node. Stale-entry
                            # pruning happens once after every local node has been
                            # polled this cycle (below) — doing it here, per node,
                            # would delete stats for peers connected to OTHER local
                            # nodes, since this dict is shared across all of them.
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
                            # DB insert (and its lookup_node, which can hit the
                            # network on a cache miss) deferred past the lock.
                            _conn_opens.append((node_str, peer, directions.get(peer, "")))
                            _alert_events.append(("connect", node_str, peer))
                        for peer in prev_est - all_set:
                            # Was established before and has now fully gone
                            _conn_closes.append((node_str, peer))
                            _alert_events.append(("disconnect", node_str, peer))
                            with _kiosk_temp_lock:
                                _kiosk_temp_conns.pop((node_str, peer), None)
                            _link_prunes.append((node_str, peer))
                            _forget_keyed(peer)
                        _prev_connected_map[node_str] = est_set

                        # Kiosk idle-timeout: update last_active when local or peer is keyed.
                        # Skip entries that are genuinely permanent (ilink 12/13) OR have had
                        # their timeout squashed via /api/status/squash-idle — the latter stays
                        # a normal transient link, it's just exempted from this tracking.
                        local_keyed = bool(status.get("keyed"))
                        _idle_touches = []
                        with _kiosk_temp_lock:
                            for (ln, pn), info in list(_kiosk_temp_conns.items()):
                                if ln != node_str or info.get('permanent') or info.get('no_timeout'):
                                    continue
                                peer_keyed = bool(status.get("links", {}).get(pn, {}).get("keyed"))
                                if local_keyed or peer_keyed:
                                    info['last_active'] = now_ts
                                    info['_unsaved']    = True
                                elif info.pop('_unsaved', False):
                                    # Persist last_active once per exchange, when the
                                    # link goes quiet (the only value that matters for
                                    # the idle clock), not on every 1s tick while keyed.
                                    _idle_touches.append((ln, pn, info.get('monitor', False),
                                                          info['last_active']))
                        _idle_saves.extend(_idle_touches)

                        # Fire idle disconnects (outside _kiosk_temp_lock to avoid deadlock)
                        _idle_dc    = []
                        _idle_stale = []
                        with _kiosk_temp_lock:
                            for (ln, pn), info in list(_kiosk_temp_conns.items()):
                                if ln != node_str or info.get('permanent') or info.get('no_timeout'):
                                    continue
                                if now_ts - info.get('last_active', now_ts) > idle_timeout:
                                    # An overdue entry whose peer isn't connected anymore
                                    # is a rehydrated leftover from a connection that
                                    # ended while HenWen was down — nothing to
                                    # disconnect, just drop the tracking. (Checked only
                                    # for overdue entries so a just-made connection the
                                    # AMI cache hasn't caught up to yet is never purged.)
                                    (_idle_dc if pn in all_set else _idle_stale).append((ln, pn))
                                    del _kiosk_temp_conns[(ln, pn)]
                        _idle_deletes.extend(_idle_dc + _idle_stale)
                        for (ln, pn) in _idle_dc:
                            try:
                                # ilink 1, not 11, is correct here (unlike the manual disconnect
                                # endpoints): _idle_dc is built only from _kiosk_temp_conns entries
                                # that already excluded info.get('permanent') above, so nothing
                                # reaching this line can be a permanent link.
                                ami.rpt_cmd(ln, f"ilink 1 {pn}")
                                log("INFO", f"[KIOSK] Idle timeout: disconnected {pn} from {ln}")
                            except Exception as _e:
                                log("ERROR", f"[KIOSK] Idle timeout disconnect failed: {_e}")

                    # Now that every locally-hosted node has been polled this cycle,
                    # prune _link_stats entries for peers no longer connected to ANY
                    # of them (see the per-node loop above for why this can't happen
                    # per-node).
                    with _link_stats_lock:
                        for gone in set(_link_stats) - _all_current_linked:
                            del _link_stats[gone]

                except Exception as e:
                    _ami_last_error = str(e)
                    log("ERROR", f"[AMI-POLL] Error during poll: {e}")
                    _ami_invalidate()

            # Deferred DB writes — the AMI lock is released now, so a busy
            # database can no longer stall AMI commands. All helpers below are
            # individually best-effort (they catch and log their own errors).
            for (ln, pn, direction) in _conn_opens:
                info = lookup_node(pn)   # may hit the network on a cache miss
                _db_conn_open(ln, pn, info.get("callsign", ""),
                              info.get("location", ""), direction)
            for (ln, pn) in _conn_closes:
                _db_conn_close(ln, pn)
            for (ln, pn) in _link_prunes:
                try:
                    pdb = get_db()
                    pdb.execute(
                        "DELETE FROM permanent_links WHERE local_node=? AND peer_node=?",
                        (ln, pn))
                    pdb.execute(
                        "DELETE FROM kiosk_temp_conns WHERE local_node=? AND peer_node=?",
                        (ln, pn))
                    pdb.commit()
                except Exception as _e:
                    log("WARN", f"[KIOSK] Failed to prune link tracking for "
                                f"{ln}->{pn}: {_e}")
            for (ln, pn, _mon, _la) in _idle_saves:
                _db_temp_conn_save(ln, pn, _mon, False, _la)
            for (ln, pn) in _idle_deletes:
                _db_temp_conn_delete(ln, pn)

            # Process node connect/disconnect alerts outside lock (may do network I/O)
            for ev_type, local, peer in _alert_events:
                try:
                    cfg = _get_alert_config()
                    if cfg and cfg["enabled"]:
                        watches = [w.strip() for w in cfg["watch_nodes"].split(",") if w.strip()]
                        if ev_type == "connect" and cfg["on_node_connect"]:
                            if not watches or peer in watches or local in watches:
                                _send_alert("HenWen: Node Connected",
                                            f"Node {peer} connected to {local}")
                        elif ev_type == "disconnect" and cfg["on_node_disconnect"]:
                            if not watches or peer in watches or local in watches:
                                _send_alert("HenWen: Node Disconnected",
                                            f"Node {peer} disconnected from {local}")
                except Exception:
                    pass
            # AMI state + CPU temp alerts — outside lock, OK to do network I/O here
            _check_alerts(_ami_connected, get_cpu_temp())
            _check_asterisk_dns_health()

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
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
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
        nodes       = []   # must be bound before the try — it's referenced after
                           # the except below, so a DB error raised before the
                           # SELECT completes would otherwise turn into a
                           # NameError there, killing this thread for good
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

# ── Hosted node's own keyup count (this repeater's RX, not a linked peer) ─────
_own_keyups      = {}   # {node_str: int}
_own_keyups_lock = threading.Lock()

# ── Connection history tracking ────────────────────────────────────────────────
_prev_connected_map = {}  # {local_node_str: set(peer_str)}

# ── Kiosk idle-timeout tracking ────────────────────────────────────────────────
# key: (local_node_str, peer_str)
# val: {'permanent': bool, 'last_active': float (epoch)}
_kiosk_temp_conns = {}
_kiosk_temp_lock  = threading.Lock()

# ── Per-node idle tracking for announcements' settle period ───────────────────
# {node_str: float (epoch of the last poll tick where the node was keyed or
# had a keyed link)}. Updated every poll cycle (1s) rather than only when
# _run_due_announcements runs (30s), so "quiet for N seconds" is measured to
# ~1s resolution instead of being coarsened by the announcer's own interval.
_node_last_active      = {}
_node_last_active_lock = threading.Lock()

# ── Alert state ───────────────────────────────────────────────────────────────
_alert_prev_ami    = None   # None=unknown, True=was connected, False=was disconnected
_alert_cpu_alerted = False

# ── Asterisk DNS-stuck detection ──────────────────────────────────────────────
# Asterisk's own resolver can get stuck unable to resolve register.allstarlink.org /
# stats.allstarlink.org (seen after a transient network blip — the OS resolver
# recovers but Asterisk's cached state doesn't, and it silently fails every
# ilink connect attempt from then on without any obvious error to the user).
# Detected by tailing Asterisk's log for the failure signature; a plain
# `systemctl restart asterisk` clears it.
_DNS_FAIL_RE          = re.compile(r'Could not resolve host|Temporary failure in name resolution|Unable to lookup')
_dns_log_pos          = None   # byte offset already scanned; None = not yet initialized
_dns_fail_times       = []     # epoch timestamps of recent failure lines
_dns_alert_active     = False
_dns_last_check       = 0.0
_DNS_CHECK_INTERVAL   = 20      # seconds between log scans (self-throttled; poll loop calls this every 1s)
_DNS_FAIL_WINDOW_SEC  = 600     # only count failures within the last 10 minutes
_DNS_FAIL_THRESHOLD   = 3       # this many failures in the window = "stuck", not a one-off blip
_DNS_RECOVER_SEC      = 600     # 10 minutes with no new failures = recovered

# ── Active session tracking (for the "logged in users" footer count) ─────────
# Flask sessions are signed cookies with no server-side store, so we track
# active logins ourselves via a random per-session id stashed in the cookie.
# key: session id, val: {'username': str, 'last_active': float (epoch)}
_active_sessions      = {}
_active_sessions_lock = threading.Lock()
ACTIVE_SESSION_WINDOW = 90  # seconds of inactivity before a session drops out of the count


def touch_active_session(sid, username, role=''):
    with _active_sessions_lock:
        _active_sessions[sid] = {'username': username, 'role': role, 'last_active': time.time()}


def remove_active_session(sid):
    if not sid:
        return
    with _active_sessions_lock:
        _active_sessions.pop(sid, None)


def _active_sessions_snapshot():
    """Prune sessions idle longer than ACTIVE_SESSION_WINDOW and return what's left."""
    cutoff = time.time() - ACTIVE_SESSION_WINDOW
    with _active_sessions_lock:
        stale = [sid for sid, info in _active_sessions.items() if info['last_active'] < cutoff]
        for sid in stale:
            del _active_sessions[sid]
        return list(_active_sessions.values())


def get_active_user_count():
    return len(_active_sessions_snapshot())


def get_active_sessions_detail():
    """Per-username detail (role, concurrent session count, seconds idle) behind
    the superuser-only "who's logged in" panel — everyone else just sees the
    count from get_active_user_count(). Multiple sessions under the same
    account (e.g. several kiosk displays sharing one login) collapse into a
    single row with a session count rather than duplicate entries."""
    now = time.time()
    by_user = {}
    for info in _active_sessions_snapshot():
        uname = info.get('username') or '(unknown)'
        entry = by_user.setdefault(uname, {
            'username': uname, 'role': info.get('role', ''),
            'sessions': 0, 'idle_sec': None,
        })
        entry['sessions'] += 1
        idle = now - info['last_active']
        if entry['idle_sec'] is None or idle < entry['idle_sec']:
            entry['idle_sec'] = idle
    return sorted(by_user.values(), key=lambda u: u['username'].lower())


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


def _forget_keyed(node: str):
    """Drop a node's entry from the keyed history, if present.

    Called on disconnect so its green activity pin disappears immediately
    instead of lingering on the map for the rest of the configured pin
    duration (Map Pin Duration setting) after it's no longer linked.
    """
    with _keyed_history_lock:
        for i, e in enumerate(_keyed_history):
            if e["node"] == node:
                del _keyed_history[i]
                break


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


def _rehydrate_permanent_links():
    """Restore _kiosk_temp_conns' knowledge of Permanent connections from the
    durable permanent_links table on startup.

    _kiosk_temp_conns is in-process memory, wiped on every HenWen restart —
    but app_rpt itself was never restarted, so a link made Permanent through
    this app is still genuinely permanent in Asterisk regardless. Without
    this, the Status Board's Permanent badge would incorrectly disappear
    after any HenWen restart even though the underlying link is unchanged.
    A row for a peer that's no longer actually connected is harmless here —
    the Status Board only ever displays permanence for nodes currently in
    the live AMI connected list, and the poll loop prunes the row for real
    the next time it observes that peer's connection end while running.
    """
    try:
        db   = get_db()
        rows = db.execute("SELECT local_node, peer_node, monitor FROM permanent_links").fetchall()
        with _kiosk_temp_lock:
            for row in rows:
                key = (row["local_node"], row["peer_node"])
                if key not in _kiosk_temp_conns:
                    _kiosk_temp_conns[key] = {
                        'permanent':   True,
                        'monitor':     bool(row["monitor"]),
                        'no_timeout':  False,
                        'last_active': time.time(),
                    }
        if rows:
            log("INFO", f"[KIOSK] Rehydrated {len(rows)} permanent link(s) from the database")
    except Exception as e:
        log("ERROR", f"[KIOSK] _rehydrate_permanent_links error: {e}")


def _db_temp_conn_save(local_node, peer_node, monitor, no_timeout, last_active):
    """Persist one temporary connection's idle-tracking state. Best-effort:
    a failed write only degrades restart persistence, never live tracking."""
    try:
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO kiosk_temp_conns "
            "(local_node, peer_node, monitor, no_timeout, last_active) VALUES (?, ?, ?, ?, ?)",
            (local_node, peer_node, 1 if monitor else 0, 1 if no_timeout else 0, last_active)
        )
        db.commit()
    except Exception as e:
        log("WARN", f"[KIOSK] Failed to persist idle state for {local_node}->{peer_node}: {e}")


def _db_temp_conn_delete(local_node, peer_node):
    try:
        db = get_db()
        db.execute("DELETE FROM kiosk_temp_conns WHERE local_node=? AND peer_node=?",
                   (local_node, peer_node))
        db.commit()
    except Exception as e:
        log("WARN", f"[KIOSK] Failed to prune kiosk_temp_conns for {local_node}->{peer_node}: {e}")


def _rehydrate_kiosk_temp_conns():
    """Restore idle-timeout tracking for temporary kiosk connections from the
    durable kiosk_temp_conns table on startup, the same way
    _rehydrate_permanent_links() does for Permanent ones.

    last_active is restored exactly as stored, so time spent with HenWen down
    counts as idle time — a temporary connection is supposed to time out, and
    the fail-safe direction is to keep that promise rather than restart the
    clock (or worse, forget the link entirely and never disconnect it, which
    is what happened before this table existed). An entry whose connection
    actually ended while HenWen was down is dropped by the poll loop's idle
    pass once it's overdue, without issuing any disconnect command.

    Runs after _rehydrate_permanent_links(); a key already present from that
    pass is genuinely permanent and wins."""
    try:
        db   = get_db()
        rows = db.execute(
            "SELECT local_node, peer_node, monitor, no_timeout, last_active FROM kiosk_temp_conns"
        ).fetchall()
        with _kiosk_temp_lock:
            for row in rows:
                key = (row["local_node"], row["peer_node"])
                if key not in _kiosk_temp_conns:
                    _kiosk_temp_conns[key] = {
                        'permanent':   False,
                        'monitor':     bool(row["monitor"]),
                        'no_timeout':  bool(row["no_timeout"]),
                        'last_active': float(row["last_active"]),
                    }
        if rows:
            log("INFO", f"[KIOSK] Rehydrated {len(rows)} temporary connection(s) from the database")
    except Exception as e:
        log("ERROR", f"[KIOSK] _rehydrate_kiosk_temp_conns error: {e}")


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
        _send_alert("HenWen: AMI Offline", "Connection to Asterisk lost", "high")
    # AMI reconnect
    if cfg["on_ami_reconnect"] and _alert_prev_ami is False and ami_ok:
        _send_alert("HenWen: AMI Reconnected", "Connection to Asterisk restored", "default")
    _alert_prev_ami = ami_ok
    # CPU temp — alert once when threshold is crossed, reset when it drops back
    if cfg["on_cpu_temp_high"] and cpu_temp is not None:
        thr = cfg["cpu_temp_threshold"]
        if cpu_temp > thr and not _alert_cpu_alerted:
            _send_alert("HenWen: High CPU Temp",
                        f"CPU is {cpu_temp}C (threshold {thr}C)", "high")
            _alert_cpu_alerted = True
        elif cpu_temp <= thr:
            _alert_cpu_alerted = False


def _check_asterisk_dns_health():
    """
    Tail Asterisk's own log for its DNS-resolution failure signature and alert
    if it looks stuck (repeated failures rather than one transient blip).
    Self-throttled to _DNS_CHECK_INTERVAL even though the caller may run every
    1s; cheap no-op the rest of the time (just an mtime-free size check).
    """
    global _dns_log_pos, _dns_fail_times, _dns_alert_active, _dns_last_check
    now = time.time()
    if now - _dns_last_check < _DNS_CHECK_INTERVAL:
        return
    _dns_last_check = now

    cfg = _get_alert_config()
    if not cfg or not cfg["enabled"] or not cfg["on_dns_stuck"]:
        return
    try:
        if not os.path.exists(ASTERISK_LOG_PATH):
            return
        size = os.path.getsize(ASTERISK_LOG_PATH)
        if _dns_log_pos is None or size < _dns_log_pos:
            # First check since startup, or the log was rotated/truncated —
            # start from the current end so we don't replay old history as
            # if it just happened.
            _dns_log_pos = size
            return
        if size == _dns_log_pos:
            new_text = ""
        else:
            with open(ASTERISK_LOG_PATH, "rb") as f:
                f.seek(_dns_log_pos)
                new_text = f.read(size - _dns_log_pos).decode("utf-8", errors="replace")
            _dns_log_pos = size

        for line in new_text.splitlines():
            if "allstarlink.org" in line and _DNS_FAIL_RE.search(line):
                _dns_fail_times.append(now)

        _dns_fail_times = [t for t in _dns_fail_times if now - t < _DNS_FAIL_WINDOW_SEC]

        if not _dns_alert_active and len(_dns_fail_times) >= _DNS_FAIL_THRESHOLD:
            _dns_alert_active = True
            log("WARN", "[ALERTS] Asterisk DNS resolution appears stuck "
                        f"({len(_dns_fail_times)} failures in the last "
                        f"{_DNS_FAIL_WINDOW_SEC // 60} min)")
            _send_alert(
                "HenWen: Asterisk DNS Stuck",
                "Asterisk can't resolve register.allstarlink.org / stats.allstarlink.org — "
                "node connects will silently fail. Restarting the Asterisk service "
                "usually clears it (sudo systemctl restart asterisk).",
                "high")
        elif _dns_alert_active and (not _dns_fail_times or now - _dns_fail_times[-1] > _DNS_RECOVER_SEC):
            _dns_alert_active = False
            _dns_fail_times   = []
            log("INFO", "[ALERTS] Asterisk DNS resolution recovered")
            _send_alert("HenWen: Asterisk DNS Recovered",
                        "Asterisk is resolving AllStarLink hostnames again.", "default")
    except Exception as e:
        log("ERROR", f"[ALERTS] _check_asterisk_dns_health error: {e}")


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
            req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0 (ham radio node manager)"})
            with urlreq.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            result = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])} if results else None
        except Exception as e:
            log("WARN", f"[GEOCODE] '{loc}': {e}")
            result = None

    with _geocode_lock:
        _geocode_cache[loc] = result
    return result


# Non-blocking geocode for request hot paths (the Status Board). Returns the
# cached coordinates immediately, or None if not yet known, and queues the
# location for the background worker below to resolve. This keeps Nominatim's
# 1.1s rate-limit sleep and network I/O out of the kiosk's board request, which
# is polled frequently — the map pin just appears a second or two after a
# location is first seen, instead of stalling the whole board response.
_geocode_queue      = deque()
_geocode_queue_seen = set()
_geocode_queue_lock = threading.Lock()


def _geocode_nonblocking(location: str):
    if not location or not location.strip():
        return None
    loc = location.strip()
    with _geocode_lock:
        if loc in _geocode_cache:
            return _geocode_cache[loc]
    with _geocode_queue_lock:
        if loc not in _geocode_queue_seen:
            _geocode_queue_seen.add(loc)
            _geocode_queue.append(loc)
    return None


def _geocode_worker_loop():
    while True:
        loc = None
        with _geocode_queue_lock:
            if _geocode_queue:
                loc = _geocode_queue.popleft()
        if loc is None:
            time.sleep(1.0)
            continue
        try:
            _geocode(loc)   # populates _geocode_cache (honors the rate limit)
        except Exception as e:
            log("WARN", f"[GEOCODE] background resolve failed for '{loc}': {e}")
        finally:
            with _geocode_queue_lock:
                _geocode_queue_seen.discard(loc)


def start_geocode_worker():
    t = threading.Thread(target=_geocode_worker_loop, name="geocoder", daemon=True)
    t.start()
    log("INFO", "[GEOCODE] Background geocoder thread launched")


# ── Global ASL activity (currently-keyed nodes network-wide) ──────────────────
# Scrapes the public "Keyed Nodes" page, which lists every node presently
# keyed up anywhere on the AllStarLink network — no arbitrary top-N cap,
# we take every row the page lists. Polled every 2 minutes.
ASL_KEYED_STATS_URL      = "https://stats.allstarlink.org/stats/keyed"
GLOBAL_ACTIVITY_INTERVAL = 120.0   # 2 minutes between sweeps

# Only the most recent activity matters for display — the keyed page is a
# live snapshot, not a history, so there's no reason to retain entries past
# the display window itself.
_GLOBAL_DISPLAY_WINDOW_SEC = 15 * 60   # 15 minutes
_GLOBAL_MAX_AGE_SEC        = _GLOBAL_DISPLAY_WINDOW_SEC

_global_nodes_cache = []   # rolling history, newest first, de-duped by node
_global_nodes_ts    = 0.0
_global_nodes_lock  = threading.Lock()

_KEYED_ROW_RE = re.compile(r'<tr>(.*?)</tr>', re.S)
_KEYED_TD_RE  = re.compile(r'<td>(.*?)</td>', re.S)
_KEYED_NODE_RE     = re.compile(r'>([^<]+)</a>')
_KEYED_CALLSIGN_RE = re.compile(r'callsign=([^"]+)"')


def _fetch_keyed_nodes():
    """
    Scrape https://stats.allstarlink.org/stats/keyed for every node
    currently keyed up network-wide. Returns one entry per row on the
    page — the page itself only lists presently-active nodes, so there
    is no separate "top N" cap to apply here.
    """
    now  = time.time()
    pool = []
    # Fetch failures propagate to _global_activity_poll_loop's handler.
    # Swallowing them here and returning [] made a network outage look like
    # a successful "0 nodes keyed" sweep: the cache timestamp got stamped
    # fresh (so the kiosk claimed up-to-date data while pins silently aged
    # out) and the loop's exponential backoff could never trigger.
    req = urlreq.Request(ASL_KEYED_STATS_URL, headers={"User-Agent": "HenWen/1.0 (keyed monitor)"})
    with urlreq.urlopen(req, timeout=10) as resp:
        page = resp.read().decode("utf-8", "replace")

    for row in _KEYED_ROW_RE.findall(page):
        tds = _KEYED_TD_RE.findall(row)
        if len(tds) < 6:
            continue
        node_m = _KEYED_NODE_RE.search(tds[0])
        node   = node_m.group(1).strip() if node_m else ""
        if not node:
            continue
        call_m   = _KEYED_CALLSIGN_RE.search(tds[2])
        callsign = html.unescape(urlparse.unquote(call_m.group(1))).strip() if call_m else ""
        location = html.unescape(re.sub(r'<[^>]+>', '', tds[5])).strip()
        pool.append({
            "node":     node,
            "callsign": callsign,
            "location": location,
            "lat":      None,
            "lon":      None,
            "ts":       now,   # observation time — used for retention/display
        })
    return pool


def _global_activity_poll_loop():
    global _global_nodes_ts
    log("INFO", "[GLOBAL-ACTIVITY] Poller started — first fetch in 30s")
    time.sleep(30)   # short startup delay
    backoff = 0
    while True:
        try:
            nodes = _fetch_keyed_nodes()
            # Geocode locations to lat/lon (cached — only new locations hit Nominatim)
            for n in nodes:
                if n["location"]:
                    coords = _geocode(n["location"])
                    if coords:
                        n["lat"] = coords["lat"]
                        n["lon"] = coords["lon"]
            now    = time.time()
            cutoff = now - _GLOBAL_MAX_AGE_SEC
            new_ids = {n["node"] for n in nodes}
            with _global_nodes_lock:
                # Retain old entries that are still within the window and
                # weren't refreshed in this sweep (those get the new entry)
                retained = [e for e in _global_nodes_cache
                            if e["ts"] >= cutoff and e["node"] not in new_ids]
                # New batch at the front, retained history behind
                _global_nodes_cache.clear()
                _global_nodes_cache.extend(nodes + retained)
                _global_nodes_ts = now
            log("INFO", f"[GLOBAL-ACTIVITY] Sweep: {len(nodes)} new/refreshed, {len(retained)} retained, {len(nodes)+len(retained)} total")
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


# ── EchoLink directory cache ─────────────────────────────────────────────────
# echolink.org/logins.jsp is a public, unauthenticated snapshot of every
# currently-logged-in EchoLink station (repeaters, links, conferences, and
# individual softphone users) — the only source available since EchoLink has
# no public API. It's a presence list, not a historical registry: a node not
# currently online won't appear. The page itself only regenerates server-side
# every 2 minutes (its own <meta http-equiv="Refresh" content="120;">), so we
# poll every 5 to be a good citizen; it's also ~800KB, much bigger than the
# AllStar keyed-nodes page, hence the longer timeout below.
ECHOLINK_LOGINS_URL    = "https://www.echolink.org/logins.jsp"
ECHOLINK_POLL_INTERVAL = 300.0   # 5 minutes

_echolink_cache      = []   # [{call, location, status, node, kind}] full-replace each successful poll
_echolink_by_node    = {}   # {echolink_id: row} — same rows, indexed for O(1) lookup by lookup_node()
_echolink_cache_ts   = 0.0
_echolink_cache_lock = threading.Lock()

# AllStarLink reserves 3000000-3999999 as pseudo-node numbers for EchoLink
# peers, encoded as '3' + the EchoLink station's own ID zero-padded to 6
# digits (see the browser-side connect encoding in status.html) — never a
# real repeater/hub node, so this range is a safe, unambiguous test.
_ECHOLINK_PSEUDO_NODE_RE = re.compile(r'^3(\d{6})$')


def _echolink_station_id(node: str):
    """Extract the raw EchoLink station ID from an AllStarLink pseudo-node
    number (e.g. '3151197' -> '151197'), or None if node isn't one."""
    m = _ECHOLINK_PSEUDO_NODE_RE.match(node)
    return str(int(m.group(1))) if m else None

# Individual softphone "Users" aren't stations a repeater would bridge to, so
# that section is intentionally not scraped here.
_ECHOLINK_SECTION_KINDS = {"rpt": "repeater", "link": "link", "conf": "conference"}
_ECHOLINK_ANCHOR_RE = re.compile(r'<a name="(rpt|link|user|conf)">', re.I)
_ECHOLINK_ROW_RE    = re.compile(r'<tr>(.*?)</tr>', re.S)
_ECHOLINK_TD_RE     = re.compile(r'<td[^>]*>(.*?)</td>', re.S)


def _fetch_echolink_directory():
    """Scrape echolink.org/logins.jsp for currently-online repeaters, links,
    and conferences. Raises on fetch failure — the poll loop catches it,
    backs off exponentially, and leaves the existing cache in place rather
    than blanking it on a transient outage. (A swallowed failure returning
    [] also skipped the cache update, but made the backoff unreachable.)"""
    rows_out = []
    req = urlreq.Request(ECHOLINK_LOGINS_URL, headers={"User-Agent": "HenWen/1.0 (echolink directory)"})
    with urlreq.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", "replace")

    anchors = list(_ECHOLINK_ANCHOR_RE.finditer(page))
    for i, m in enumerate(anchors):
        kind = _ECHOLINK_SECTION_KINDS.get(m.group(1).lower())
        if not kind:
            continue
        section_html = page[m.end():(anchors[i + 1].start() if i + 1 < len(anchors) else len(page))]
        for row in _ECHOLINK_ROW_RE.findall(section_html):
            tds = _ECHOLINK_TD_RE.findall(row)
            if len(tds) < 5:
                continue
            call     = html.unescape(re.sub(r'<[^>]+>', '', tds[0])).strip()
            location = html.unescape(re.sub(r'<[^>]+>', '', tds[1])).strip()
            status   = html.unescape(re.sub(r'<[^>]+>', '', tds[2])).strip()
            node     = html.unescape(re.sub(r'<[^>]+>', '', tds[4])).strip()
            if not call or not re.match(r'^\d{1,7}$', node):
                continue
            rows_out.append({"call": call, "location": location, "status": status, "node": node, "kind": kind})
    return rows_out


def _echolink_poll_loop():
    global _echolink_cache_ts
    log("INFO", "[ECHOLINK] Directory poller started — first fetch in 30s")
    time.sleep(30)
    backoff = 0
    while True:
        try:
            rows = _fetch_echolink_directory()
            if rows:
                with _echolink_cache_lock:
                    _echolink_cache[:] = rows
                    _echolink_by_node.clear()
                    _echolink_by_node.update({r["node"]: r for r in rows})
                    _echolink_cache_ts = time.time()
                log("INFO", f"[ECHOLINK] Directory refreshed: {len(rows)} stations")
                backoff = 0
            time.sleep(ECHOLINK_POLL_INTERVAL)
        except Exception as e:
            backoff = min(backoff + 1, 5)
            sleep_s = ECHOLINK_POLL_INTERVAL * (2 ** (backoff - 1))
            log("WARN", f"[ECHOLINK] Loop error ({e}) — retry in {sleep_s:.0f}s")
            time.sleep(sleep_s)


def start_echolink_directory_poller():
    t = threading.Thread(target=_echolink_poll_loop, name="echolink-directory", daemon=True)
    t.start()
    log("INFO", "[ECHOLINK] Directory poller thread launched")


# ── Weather cache (wttr.in) ───────────────────────────────────────────────────
WTTR_URL         = "https://wttr.in/{}?format=j1"
WEATHER_INTERVAL = 600.0   # 10 minutes per location

_weather_cache      = {}   # {location: {"data": dict, "ts": float}}
_weather_last_good  = {}   # {location: dict}  — last successful fetch, never overwritten by errors
_weather_lock       = threading.Lock()


def _fetch_weather(location: str) -> dict:
    """Return current weather for a location from wttr.in. Cached 10 minutes.

    On failure, returns the last successful data with stale=True rather than
    an error, so the weather bar stays useful during transient outages.
    """
    if not location or not location.strip():
        return {"error": "No location configured for this node"}
    loc = location.strip()

    with _weather_lock:
        entry = _weather_cache.get(loc)
        if entry and (time.time() - entry["ts"]) < WEATHER_INTERVAL:
            if entry["data"] is not None:
                return entry["data"]
            # A recent fetch failed and we're inside its shortened retry
            # window (the error path below caches data=None to schedule the
            # retry). Serve the same degraded response that failing request
            # returned — previously this handed back the raw None, i.e. the
            # endpoint responded with a literal JSON null.
            good = _weather_last_good.get(loc)
            if good:
                return {**good, "stale": True, "error": None}
            return {"error": "Weather unavailable", "location": loc, "stale": True}

    try:
        url = WTTR_URL.format(urlparse.quote_plus(loc))
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
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
            "stale":    False,
        }
        with _weather_lock:
            _weather_cache[loc]     = {"data": data, "ts": time.time()}
            _weather_last_good[loc] = data
        return data
    except Exception as e:
        log("WARN", f"[WEATHER] fetch failed for '{loc}': {e}")
        with _weather_lock:
            good = _weather_last_good.get(loc)
            # Schedule a retry after a short interval rather than waiting the full 10 min
            _weather_cache[loc] = {"data": None, "ts": time.time() - WEATHER_INTERVAL + 60}
        if good:
            return {**good, "stale": True, "error": None}
        return {"error": "Weather unavailable", "location": loc, "stale": True}


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

    # Prune oldest backups, keeping only the most recent MAX_BACKUPS
    MAX_BACKUPS = 20
    try:
        baks = sorted(
            [f for f in os.listdir(BACKUP_DIR)
             if re.match(r'^rpt\.conf\.\d{8}_\d{6}\.bak$', f)],
        )
        for old in baks[:-MAX_BACKUPS]:
            os.unlink(os.path.join(BACKUP_DIR, old))
            log("INFO", f"[CONF] Pruned old backup: {old}")
    except Exception as e:
        log("WARN", f"[CONF] Backup pruning failed: {e}")

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
                     f"Running as {pwd.getpwuid(os.getuid()).pw_name}. "
                     f"Ensure {path} is writable by the service user.")
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
# Mirrors the GENERAL_META / NODE_SECS metadata in templates/henwen-manager.html
# (sourced from https://allstarlink.github.io/config/rpt_conf/). The web UI
# already restricts these fields to dropdowns/number inputs, but a request
# can bypass the UI entirely, so the same constraints are enforced here
# before anything is written to rpt.conf. Keys not listed are free-form
# (paths, stanza names, etc.) and are not validated.
# ---------------------------------------------------------------------------
SETTINGS_SCHEMA = {
    # [general] stanza
    "node_lookup_method": {"type": "enum",   "options": ["both", "dns", "file"]},
    "max_dns_node_length":{"type": "number", "min": 1, "max": 10},
    "dns_node_domain":    {"type": "nonempty"},

    # Basic / Required
    "rxchannel":   {"type": "nonempty"},          # driver/channel — can't be blank when live
    "duplex":      {"type": "enum",   "options": ["0", "1", "2", "3", "4"]},
    "context":     {"type": "identifier"},        # Asterisk dialplan context name
    "callerid":    {},                            # "Name" <number> — flexible, blank OK
    "accountcode": {},                            # CDR code — blank OK
    "linktolink":  {"type": "boolean"},

    # Station Identification
    "idrecording": {"type": "id_sound"},          # |iCALLSIGN or sound filename
    "idtalkover":  {"type": "id_sound"},          # same; must have a value when enabled
    "idtime":      {"type": "number", "min": 0, "max": 2400000},
    "politeid":    {"type": "number", "min": 0},
    "beaconing":   {"type": "boolean"},

    # Timers
    "hangtime":    {"type": "number", "min": 0},
    "althangtime": {"type": "number", "min": 0},
    "totime":      {"type": "number", "min": 0, "max": 9999999},
    "time_out_reset_unkey_interval":    {"type": "number", "min": 0, "max": 3000},
    "time_out_reset_kerchunk_interval": {"type": "number", "min": 0},
    "sleeptime":   {"type": "number", "min": 0},

    # Telemetry
    "telemdefault":  {"type": "enum",    "options": ["0", "1", "2"]},
    "telemdynamic":  {"type": "boolean"},
    "holdofftelem":  {"type": "boolean"},
    "telemduckdb":   {"type": "number"},
    "telemnomdb":    {"type": "number"},
    "unlinkedct":    {},                          # courtesy tone name — blank OK (no tone)
    "remotect":      {},
    "linkunkeyct":   {},
    "nounkeyct":     {"type": "boolean"},
    "nolocallinkct": {"type": "boolean"},

    # DTMF & Functions
    "funcchar":       {"type": "dtmf_char"},      # exactly one character; ≠ endchar
    "endchar":        {"type": "dtmf_char"},      # exactly one character; ≠ funcchar
    "functions":      {"type": "identifier"},
    "link_functions": {"type": "identifier"},
    "phone_functions":{"type": "identifier"},
    "dtmfkey":        {"type": "boolean"},
    "dtmfkeys":       {"type": "identifier"},
    "propagate_dtmf": {"type": "boolean"},
    "startup_macro":  {},                         # DTMF command string — blank = no-op

    # Node Connections
    "extnodefile":    {},                         # file path(s) — blank OK
    "nodes":          {"type": "identifier"},
    "nodenames":      {},                         # directory path — blank uses default
    "connpgm":        {"type": "nonempty"},       # script path — must exist when enabled
    "discpgm":        {"type": "nonempty"},
    "lnkactenable":   {"type": "boolean"},
    "lnkacttime":     {"type": "number", "min": 0, "max": 90000},
    "lnkactmacro":    {},                         # DTMF macro — blank OK
    "lnkacttimerwarn":{},                         # sound filename — blank = no warning

    # Audio
    "linkmongain":    {"type": "number"},
    "erxgain":        {"type": "number"},
    "etxgain":        {"type": "number"},
    "rxnotch":        {"type": "notch"},          # freq,bandwidth — blank OK, format required if set
    "outstreamcmd":   {},                         # command string — blank OK

    # Tail & Scheduler
    "tailmessagelist":  {},                       # comma-sep sound files — blank OK
    "tailmessagetime":  {"type": "number", "min": 0, "max": 200000000},
    "tailsquashedtime": {"type": "number", "min": 0},
    "scheduler":        {"type": "identifier"},

    # Parrot / Echo
    "parrot":     {"type": "boolean"},
    "parrottime": {"type": "number", "min": 0},

    # EchoLink
    "eannmode":        {"type": "enum",    "options": ["0", "1", "2", "3"]},
    "echolinkdefault": {"type": "enum",    "options": ["0", "1", "2", "3"]},
    "echolinkdynamic": {"type": "boolean"},
    "elke":            {},                        # FFF timer units — blank = not an Elke node

    # Archiving & Stats
    "archivedir":      {"type": "nonempty"},      # directory path — must exist when enabled
    "archiveformat":   {"type": "enum",    "options": ["wav49", "wav", "gsm"]},
    "archivedatefmt":  {"type": "nonempty"},      # strftime format — can't be blank
    "archiveaudio":    {"type": "boolean"},
    "statpost_url":    {"type": "url"},           # http(s) URL — blank OK, format required if set
    "statpost_program":{},                        # external program path — blank uses default

    # Link Telemetry
    "guilinkdefault":  {"type": "enum",    "options": ["0", "1", "2", "3"]},
    "guilinkdynamic":  {"type": "boolean"},
    "phonelinkdefault":{"type": "enum",    "options": ["0", "1", "2", "3"]},
    "phonelinkdynamic":{"type": "boolean"},
    "tlbdefault":      {"type": "enum",    "options": ["0", "1", "2", "3"]},
    "tlbdynamic":      {"type": "boolean"},

    # Voting
    "votermode":   {"type": "enum",   "options": ["0", "1", "2"]},
    "votertype":   {"type": "enum",   "options": ["0", "1", "2"]},
    "votermargin": {"type": "number"},

    # Stanza Name Overrides
    "controlstates": {"type": "identifier"},
    "macro":         {"type": "identifier"},
    "telemetry":     {"type": "identifier"},
    "morse":         {"type": "identifier"},
    "tonemacro":     {"type": "identifier"},
    "events":        {"type": "identifier"},
    "wait-times":    {"type": "identifier"},
}


# Compiled regexes for validate_setting
_RE_IDENTIFIER = re.compile(r'^[A-Za-z0-9_\-]+$')
_RE_NOTCH      = re.compile(r'^\d+(?:\.\d+)?(?:,\d+(?:\.\d+)?)+$')   # one or more freq,bw pairs
_RE_URL        = re.compile(r'^https?://')
_RE_CW_CHARS   = re.compile(r'^[A-Za-z0-9 /.\-!?,_]+$')
_RE_SOUNDFILE  = re.compile(r'^[A-Za-z0-9_/.\-]+$')
_BOOL_VALUES   = frozenset(["0", "1", "yes", "no", "true", "false", "on", "off"])

# Types that cannot be empty when the setting is active (toggle ON)
_NONEMPTY_TYPES = frozenset(["nonempty", "id_sound", "dtmf_char", "identifier"])


def validate_setting(key, value):
    """Return None if value is acceptable for key, otherwise an error string."""
    schema = SETTINGS_SCHEMA.get(key)
    if schema is None:
        return None   # unknown key: free-form, always passes

    t = schema.get("type", "freeform")

    if value == "":
        if t in _NONEMPTY_TYPES:
            label = {
                "nonempty":   "a value",
                "id_sound":   "a sound file name or |iCALLSIGN CW value",
                "dtmf_char":  "a single character",
                "identifier": "a stanza or context name",
            }.get(t, "a value")
            return f"{key}: requires {label} when enabled — disable the toggle instead of leaving it blank"
        return None   # blank is fine for freeform/number/enum/boolean/notch/url

    # Non-empty value validation
    if t == "enum":
        opts = schema.get("options", [])
        if value not in opts:
            return f"{key}: {value!r} is not valid — must be one of: {', '.join(opts)}"

    elif t == "boolean":
        if value.lower() not in _BOOL_VALUES:
            return (f"{key}: {value!r} is not valid — must be one of: "
                    "0, 1, yes, no, true, false, on, off")

    elif t == "number":
        try:
            num = float(value)
        except ValueError:
            return f"{key}: {value!r} is not a number"
        if "min" in schema and num < schema["min"]:
            return f"{key}: {value!r} is below the minimum of {schema['min']}"
        if "max" in schema and num > schema["max"]:
            return f"{key}: {value!r} exceeds the maximum of {schema['max']}"

    elif t == "id_sound":
        if value.startswith("|i"):
            cw = value[2:]
            if not cw:
                return f"{key}: CW format |i<callsign> requires a callsign after |i (e.g. |iN8GMZ)"
            if not _RE_CW_CHARS.match(cw):
                return (f"{key}: CW callsign {cw!r} contains invalid characters "
                        "(allowed: A-Z 0-9 space / . - ! ? , _)")
        elif not _RE_SOUNDFILE.match(value):
            return (f"{key}: sound file name {value!r} contains invalid characters "
                    "(letters, digits, / . - _ only — no spaces or special chars)")

    elif t == "dtmf_char":
        if len(value) != 1:
            return f"{key}: must be exactly one character, got {len(value)} character(s)"

    elif t == "identifier":
        if not _RE_IDENTIFIER.match(value):
            return (f"{key}: {value!r} must use only letters, digits, "
                    "hyphens, and underscores (no spaces)")

    elif t == "notch":
        if not _RE_NOTCH.match(value):
            return (f"{key}: {value!r} must be one or more freq,bandwidth pairs "
                    "separated by commas (e.g. 1065,40)")

    elif t == "url":
        if not _RE_URL.match(value):
            return f"{key}: {value!r} must start with http:// or https://"

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


_ALLMONDB_EMPTY = {"callsign": "", "desc": "", "location": ""}


def fetch_allmondb_node(node: str) -> dict:
    global _allmondb_cache, _allmondb_loaded
    node = str(node)

    with _allmondb_lock:
        if node in _allmondb_cache:
            return _allmondb_cache[node]

    if _astdb_loaded and node in _astdb_cache:
        return _astdb_cache[node]

    # The allmondb is downloaded once (all ~41k nodes) and cached whole. If it
    # has already been loaded, a node that still isn't present simply isn't in
    # the database — do NOT re-download the entire thing. Cache a negative
    # result so repeated lookups stay O(1). Without this, the Status Board
    # (which calls lookup_node() for every connected node on every refresh)
    # triggered a full-database HTTP download per unknown node per refresh —
    # multi-second board latency and a flood of full downloads against
    # allmondb.allstarlink.org.
    if _allmondb_loaded:
        with _allmondb_lock:
            _allmondb_cache[node] = _ALLMONDB_EMPTY
        return _ALLMONDB_EMPTY

    try:
        url = ALLMONDB_URL
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
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
            # Negative-cache a miss so we don't reconsider this node next time.
            result = _allmondb_cache.get(node)
            if result is None:
                _allmondb_cache[node] = _ALLMONDB_EMPTY
                result = _ALLMONDB_EMPTY
            return result

    except Exception as e:
        log("WARN", f"[ALLMONDB] API fetch failed: {e} — falling back to local cache")
        return _astdb_cache.get(node, _ALLMONDB_EMPTY)


def lookup_node(node: str) -> dict:
    node = str(node)

    el_id = _echolink_station_id(node)
    if el_id is not None:
        # EchoLink pseudo-nodes aren't in allmondb/astdb at all — the only
        # source of truth is the directory poller's presence snapshot, and a
        # station currently offline just won't have a name to show.
        with _echolink_cache_lock:
            row = _echolink_by_node.get(el_id)
        if row:
            return {"callsign": row["call"], "desc": row["kind"], "location": row["location"]}
        return dict(_ALLMONDB_EMPTY)

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
#
# These are called from the Status Board endpoint on every kiosk refresh (and
# some from the 1s AMI poll loop), and several shell out to subprocesses that
# are individually cheap but add up on a low-end box when polled frequently:
#   get_disk_usage      -> df
#   get_asl_version     -> dpkg -l asl3          (parses the dpkg DB — heaviest)
#   get_asterisk_status -> systemctl is-active + asterisk -rx "core show version"
#   get_cpu_temp        -> vcgencmd, ONLY if the /sys thermal path is missing
# The underlying values barely change between refreshes, so each is wrapped in
# a short TTL memo. This decouples cost from how fast the kiosk polls, so the
# board can refresh quickly without spawning a burst of subprocesses each time.
# ---------------------------------------------------------------------------
def _ttl_cached(ttl):
    """Memoize a zero-arg function's result for `ttl` seconds (thread-safe)."""
    def deco(fn):
        state = {"v": None, "ts": 0.0}
        lock  = threading.Lock()
        @wraps(fn)
        def wrapper():
            now = time.time()
            with lock:
                if state["ts"] and (now - state["ts"]) < ttl:
                    return state["v"]
            v = fn()   # computed outside the lock so a slow subprocess never
                       # blocks other threads reading a still-fresh cached value
            with lock:
                state["v"]  = v
                state["ts"] = time.time()
            return v
        return wrapper
    return deco


@_ttl_cached(10)
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


@_ttl_cached(60)
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


RELEASE_POLL_INTERVAL  = 86400.0   # 24 hours between successful checks
RELEASE_RETRY_INTERVAL = 3600.0    # retry sooner after a failed check — one
                                   # transient network error at boot+30s
                                   # shouldn't mean a full day with no
                                   # update-availability info

_latest_release_cache = {"tag": "", "url": ""}
_latest_release_lock  = threading.Lock()


def _fetch_latest_release():
    """Hit GitHub for the latest published release tag. CalVer tags
    (vYYYY.MM.DD, optionally .N for a same-day second release) sort
    correctly as plain strings, so no semver parsing is needed — just
    compare against HENWEN_VERSION lexically elsewhere. (A same-day suffix
    above single digits, e.g. .10 vs .9, would sort incorrectly as a
    string — not a real concern at the release cadence this project
    actually has.)"""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0",
                                           "Accept": "application/vnd.github+json"})
        with urlreq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        return {"tag": data.get("tag_name", ""), "url": data.get("html_url", "")}
    except Exception as e:
        log("WARN", f"[UPDATE-POLL] release check failed: {e}")
        return None


def _release_poll_loop():
    log("INFO", "[UPDATE-POLL] Release poller started — first check in 30s")
    time.sleep(30)
    while True:
        result = _fetch_latest_release()
        if result:
            with _latest_release_lock:
                _latest_release_cache.update(result)
            log("INFO", f"[UPDATE-POLL] Latest published release: {result['tag'] or 'none'}")
            time.sleep(RELEASE_POLL_INTERVAL)
        else:
            time.sleep(RELEASE_RETRY_INTERVAL)


def start_release_poller():
    t = threading.Thread(target=_release_poll_loop, name="release-poller", daemon=True)
    t.start()
    log("INFO", "[UPDATE-POLL] Release poller thread launched")


def get_latest_release():
    """Cheap in-process cache read — the actual GitHub hit happens once a
    day in _release_poll_loop, not on every Manager login."""
    with _latest_release_lock:
        return dict(_latest_release_cache)


@_ttl_cached(3600)   # package version effectively never changes at runtime
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


@_ttl_cached(10)
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
def redirect_old_manager():
    return redirect(url_for("index"), 301)

@app.route("/henwen-manager")
def index():
    content   = read_conf_file(RPT_CONF_PATH)
    nodes     = get_node_numbers(content) if content else []
    templates = sorted(get_node_template_usage(content).keys()) if content else []
    macros    = sorted(get_macro_stanza_usage(content).keys()) if content else []
    schedules = sorted(get_schedule_stanza_usage(content).keys()) if content else []
    return render_template("henwen-manager.html",
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
        if session.get('role') not in ('superuser', 'owner'):
            return jsonify({"error": "Superuser access required for raw editor"}), 403
        log("INFO", f"[API] /api/save raw content ({len(raw)} bytes)")
        try:
            backup = write_conf_file(RPT_CONF_PATH, raw)
            return jsonify({"success": True, "backup": backup,
                            "message": f"Saved. Backup: {backup}"})
        except PermissionError as e:
            return jsonify({"error": str(e),
                            "hint": "Ensure rpt.conf and backups are owned by the service user (chown asterisk:asterisk /etc/asterisk/rpt.conf)"}), 403
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
    # Cross-field: funcchar and endchar must differ
    if not is_macro_stanza and not is_schedule_stanza and not errors:
        sec_vals = {k: v.get("value", "") for k, v in parse_stanza_settings(content, section).items()}
        for k, info in changes.items():
            if info.get("enabled", True):
                sec_vals[k] = info.get("value", "")
            else:
                sec_vals.pop(k, None)
        fc = sec_vals.get("funcchar", "")
        ec = sec_vals.get("endchar", "")
        if fc and ec and fc == ec:
            errors.append(f"funcchar and endchar must be different characters (both are {fc!r})")

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
                        "hint": "Ensure rpt.conf and backups are owned by the service user (chown asterisk:asterisk /etc/asterisk/rpt.conf)"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/save error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/restart", methods=["POST"])
def api_restart():
    log("INFO", "[API] /api/restart called")
    try:
        r = _systemctl("restart", "asterisk", timeout=30)
        log("INFO", f"[API] systemctl restart asterisk -> rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        if r.returncode == 0:
            return jsonify({"success": True,
                            "output": r.stdout or "Asterisk restarted successfully.",
                            "command": "systemctl restart asterisk"})
        stderr = r.stderr.strip()
        hint = ("Service account lacks sudo rights for this systemctl action — "
                "re-run install.sh to install the henwen-systemctl sudoers rule."
                if "password" in stderr.lower() or "authoriz" in stderr.lower()
                else "Check: systemctl status asterisk  and  journalctl -u asterisk -n 30")
        return jsonify({
            "error":     stderr or f"systemctl returned code {r.returncode}",
            "stdout":    r.stdout,
            "returncode": r.returncode,
            "hint":      hint,
        }), 500
    except PermissionError as e:
        return jsonify({"error": str(e),
                        "hint": "Service user needs sudo rights for systemctl restart"}), 403
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
                        "hint": "Ensure rpt.conf is writable by the service user (chown asterisk:asterisk)"}), 403
    except Exception as e:
        log("ERROR", f"[API] /api/backups/{name}/restore error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<name>", methods=["DELETE"])
def api_backup_delete(name):
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required to delete backups"}), 403
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


@app.route("/api/status/history")
def api_status_history():
    """
    Condensed recent connection history for the Status Board (kiosk).
    Scoped to this server's own hosted node(s) only, completed
    connections only (still-live ones belong in the Connected Nodes
    panel, not here), last 5. The full searchable/paginated history
    (any node, live or not, clear button) lives in the Manager's
    Conn. History tab via /api/connection-history.
    """
    content = read_conf_file(RPT_CONF_PATH)
    nodes   = get_node_numbers(content) if content else []
    if not nodes:
        return jsonify({"rows": []})

    db     = get_db()
    marks  = ",".join("?" * len(nodes))
    rows   = db.execute(
        f"SELECT peer_node, peer_callsign, peer_location, direction, "
        f"connected_at, disconnected_at, duration_seconds FROM connection_history "
        f"WHERE local_node IN ({marks}) AND disconnected_at IS NOT NULL "
        f"ORDER BY connected_at DESC LIMIT 5",
        [str(n) for n in nodes]
    ).fetchall()
    resp = jsonify({"rows": [dict(r) for r in rows]})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
            "on_dns_stuck": 1,
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
            on_node_connect, on_node_disconnect, watch_nodes, on_dns_stuck)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            1 if data.get("on_dns_stuck")      else 0,
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
        _send_alert("HenWen: Test Alert",
                    "This is a test notification from HenWen", "default")
        return jsonify({"ok": True, "message": "Test alert sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── User Management API ───────────────────────────────────────────────────────

@app.route("/api/users")
def api_users_list():
    rows = get_db().execute(
        "SELECT id, username, role, created_at, session_idle_timeout, restrict_disconnect "
        "FROM users ORDER BY role DESC, username"
    ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/users", methods=["POST"])
def api_users_create():
    data        = request.json or {}
    username    = str(data.get("username", "")).strip()
    password    = str(data.get("password", ""))
    role        = str(data.get("role", "user")).strip()
    # Only meaningful for role='user' — other roles already bypass the
    # disconnect gate entirely, so the flag is simply ignored for them.
    restrict_disconnect = bool(data.get("restrict_disconnect", False)) and role == "user"
    caller_role = session.get('role', '')
    if not re.match(r'^[A-Za-z0-9_.-]{2,32}$', username):
        return jsonify({"error": "Username must be 2-32 chars: letters, digits, _ . -"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if role not in ("owner", "superuser", "admin", "user"):
        return jsonify({"error": "Role must be owner, superuser, admin, or user"}), 400
    # No one may grant a role above their own — otherwise a Superuser could
    # promote themselves (or anyone) straight to Owner.
    if ROLE_RANK.get(role, 0) > ROLE_RANK.get(caller_role, -1):
        return jsonify({"error": "You cannot assign a role higher than your own"}), 403
    # Admins can only create user-level accounts
    if caller_role == 'admin' and role in ('owner', 'superuser', 'admin'):
        return jsonify({"error": "Admins can only create user accounts"}), 403
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, restrict_disconnect) VALUES (?,?,?,?)",
            (username, generate_password_hash(password), role, int(restrict_disconnect)))
        db.commit()
        _seed_default_favorites(cur.lastrowid)
        log("INFO", f"[USERS] Created user '{username}' role={role} "
                    f"restrict_disconnect={restrict_disconnect} by {caller_role}")
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
    # No one may manage an account ranked above their own, or assign a role
    # above their own — otherwise a Superuser could edit/promote an Owner,
    # or promote someone (including themselves) straight to Owner.
    if (ROLE_RANK.get(user["role"], 0) > ROLE_RANK.get(caller_role, -1) or
            ROLE_RANK.get(new_role, 0) > ROLE_RANK.get(caller_role, -1)):
        return jsonify({"error": "You cannot manage an account ranked above your own"}), 403
    # Admins cannot elevate accounts to owner/admin/superuser, or edit existing elevated accounts
    if caller_role == 'admin' and (user["role"] in ('owner', 'superuser', 'admin') or
                                    new_role in ('owner', 'superuser', 'admin')):
        return jsonify({"error": "Admins can only manage user accounts"}), 403
    # Prevent removing the last superuser
    if user["role"] == "superuser" and new_role != "superuser":
        su_count = db.execute("SELECT COUNT(*) FROM users WHERE role='superuser'").fetchone()[0]
        if su_count <= 1:
            return jsonify({"error": "Cannot change the last superuser account."}), 400
    updates = []
    params  = []
    if "role" in data:
        if new_role not in ("owner", "superuser", "admin", "user"):
            return jsonify({"error": "Role must be owner, superuser, admin, or user"}), 400
        updates.append("role=?"); params.append(new_role)
    if "password" in data:
        pw = data["password"]
        if len(pw) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        updates.append("password_hash=?"); params.append(generate_password_hash(pw))
    if "session_idle_timeout" in data:
        raw = data["session_idle_timeout"]
        if raw is None:
            # None/null → restore global default
            updates.append("session_idle_timeout=?"); params.append(None)
        else:
            try:
                secs = int(raw)
                if secs < 0:
                    return jsonify({"error": "Idle timeout must be 0 (never) or a positive number of seconds"}), 400
                # 0 → never expire; positive → custom timeout in seconds
                updates.append("session_idle_timeout=?"); params.append(secs)
            except (TypeError, ValueError):
                return jsonify({"error": "Idle timeout must be a number"}), 400
    if "restrict_disconnect" in data:
        # Only meaningful for role='user' — other roles bypass the disconnect
        # gate entirely regardless of this flag.
        restrict_disconnect = bool(data["restrict_disconnect"]) and new_role == "user"
        updates.append("restrict_disconnect=?"); params.append(int(restrict_disconnect))
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
    if ROLE_RANK.get(user["role"], 0) > ROLE_RANK.get(caller_role, -1):
        return jsonify({"error": "You cannot delete an account ranked above your own"}), 403
    if caller_role == 'admin' and user["role"] in ('owner', 'superuser', 'admin'):
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

# Seeded into every newly-created account's Favorites list, so the kiosk
# shows how the feature looks right away instead of an empty list. Purely a
# starting point — each user can remove any or all of these like any other
# favorite.
DEFAULT_FAVORITE_NODES = ['64549', '666380', '27664', '55553']


def _seed_default_favorites(user_id):
    db = get_db()
    for i, node in enumerate(DEFAULT_FAVORITE_NODES):
        info  = lookup_node(node)
        label = info.get("callsign") or info.get("desc") or f"Node {node}"
        db.execute("INSERT OR IGNORE INTO favorites (user_id, node, label, sort_order) VALUES (?,?,?,?)",
                   (user_id, node, label, i))
    db.commit()


@app.route("/api/favorites")
def api_favorites():
    user_id = session.get("user_id")
    if not user_id:
        resp = jsonify({"favorites": []})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    try:
        db   = get_db()
        rows = db.execute(
            "SELECT * FROM favorites WHERE user_id=? ORDER BY sort_order, id", (user_id,)).fetchall()
        favs = [dict(r) for r in rows]
        for fav in favs:
            fav.update(lookup_node(fav["node"]))
        resp = jsonify({"favorites": favs})
        resp.headers["Cache-Control"] = "no-store"
        return resp
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
        next_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM favorites WHERE user_id=?",
            (user_id,)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO favorites (user_id, node, label, sort_order) VALUES (?,?,?,?)",
                   (user_id, node, label, next_order))
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


@app.route("/api/favorites/reorder", methods=["POST"])
def api_fav_reorder():
    """
    Persist the custom drag-to-reorder sequence for the current user's
    Favorites. `order` is the full list of node numbers in the desired
    display order; sort_order is rewritten to match its index. Scoped to
    user_id in the UPDATE, so a node number that isn't actually one of this
    user's favorites is silently a no-op rather than an error.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    data  = request.json or {}
    order = data.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order must be a non-empty list of node numbers"}), 400
    try:
        db = get_db()
        for idx, node in enumerate(order):
            db.execute("UPDATE favorites SET sort_order=? WHERE user_id=? AND node=?",
                       (idx, user_id, str(node).strip()))
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Scheduled-net calendar reminders ──────────────────────────────────────────
# Shared, kiosk-editable list of recurring/one-time net reminders. Reading the
# list is public (it drives the weather-bar's "net today" display for every
# visitor, logged in or not); adding/editing/deleting requires a login, same
# permission level as Favorites, to keep an open kiosk touchscreen from being
# vandalized by an anonymous visitor.
_NET_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def _validate_net_fields(data, existing=None):
    """Validate incoming fields for create (existing=None) or update
    (existing=the current DB row, for a PATCH). Returns (fields_dict, error).

    For an update, only keys actually present in `data` are validated/written
    — except recurrence/weekday/net_date, which are validated as a group
    whenever the caller touches any one of them, falling back to the
    existing row's values for the ones they didn't send. That keeps a PATCH
    that only changes, say, weekday from leaving the row with a stale
    net_date from a previous 'once' schedule, or vice versa."""
    fields  = {}
    partial = existing is not None

    if "name" in data or not partial:
        name = str(data.get("name", "")).strip()
        if not name:
            return None, "Name is required"
        if len(name) > 100:
            return None, "Name must be 100 characters or fewer"
        fields["name"] = name

    if "node" in data or not partial:
        node = str(data.get("node", "")).strip()
        if len(node) > 50:
            return None, "Node must be 50 characters or fewer"
        fields["node"] = node

    if "notes" in data or not partial:
        notes = str(data.get("notes", "")).strip()
        if len(notes) > 500:
            return None, "Notes must be 500 characters or fewer"
        fields["notes"] = notes

    if "time" in data or not partial:
        net_time = str(data.get("time", "")).strip()
        if not _NET_TIME_RE.match(net_time):
            return None, "Time must be in 24-hour HH:MM format"
        fields["time"] = net_time

    if "end_time" in data or not partial:
        end_time = str(data.get("end_time", "") or "").strip()
        if end_time and not _NET_TIME_RE.match(end_time):
            return None, "End time must be in 24-hour HH:MM format"
        fields["end_time"] = end_time or None

    # Re-check start/end ordering whenever either side changed — editing just
    # "time" on a row that already has an end_time must not be allowed to
    # push the start past it (validating only inside the end_time block above
    # would miss that direction).
    if "time" in fields or "end_time" in fields:
        eff_start = fields.get("time", existing["time"] if partial else "")
        eff_end   = fields["end_time"] if "end_time" in fields else (existing["end_time"] if partial else None)
        if eff_start and eff_end and eff_end <= eff_start:
            return None, "End time must be after the start time"

    touches_recurrence_group = bool({"recurrence", "weekday", "net_date"} & set(data))
    if touches_recurrence_group or not partial:
        recurrence = str(data.get("recurrence", existing["recurrence"] if partial else "")).strip()
        if recurrence not in ("weekly", "once"):
            return None, "recurrence must be 'weekly' or 'once'"
        fields["recurrence"] = recurrence
        if recurrence == "weekly":
            weekday_raw = data.get("weekday", existing["weekday"] if partial else None)
            try:
                weekday = int(weekday_raw)
            except (TypeError, ValueError):
                weekday = -1
            if not (0 <= weekday <= 6):
                return None, "weekday must be 0 (Monday) through 6 (Sunday)"
            fields["weekday"]  = weekday
            fields["net_date"] = None
        else:
            net_date = str(data.get("net_date", existing["net_date"] if partial else "")).strip()
            try:
                datetime.strptime(net_date, "%Y-%m-%d")
            except ValueError:
                return None, "net_date must be a valid YYYY-MM-DD date"
            fields["net_date"] = net_date
            fields["weekday"]  = None

    return fields, None


@app.route("/api/nets")
def api_nets_list():
    db   = get_db()
    rows = db.execute("SELECT * FROM net_schedules ORDER BY name COLLATE NOCASE").fetchall()
    resp = jsonify([dict(r) for r in rows])
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/nets", methods=["POST"])
def api_nets_create():
    data = request.json or {}
    fields, err = _validate_net_fields(data)
    if err:
        return jsonify({"error": err}), 400
    fields["created_by"] = session.get("username", "")
    db = get_db()
    db.execute(
        "INSERT INTO net_schedules (name, node, recurrence, weekday, net_date, time, end_time, notes, created_by) "
        "VALUES (:name, :node, :recurrence, :weekday, :net_date, :time, :end_time, :notes, :created_by)",
        fields
    )
    db.commit()
    row = db.execute("SELECT * FROM net_schedules WHERE rowid=last_insert_rowid()").fetchone()
    log("INFO", f"[NETS] Created '{fields['name']}' by {fields['created_by'] or 'unknown'}")
    return jsonify(dict(row)), 201


@app.route("/api/nets/<int:net_id>", methods=["PATCH"])
def api_nets_update(net_id):
    db  = get_db()
    row = db.execute("SELECT * FROM net_schedules WHERE id=?", (net_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    fields, err = _validate_net_fields(data, existing=row)
    if err:
        return jsonify({"error": err}), 400
    if not fields:
        return jsonify(dict(row))
    set_clause = ", ".join(f"{k}=:{k}" for k in fields)
    fields["id"] = net_id
    db.execute(f"UPDATE net_schedules SET {set_clause} WHERE id=:id", fields)
    db.commit()
    row = db.execute("SELECT * FROM net_schedules WHERE id=?", (net_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/nets/<int:net_id>", methods=["DELETE"])
def api_nets_delete(net_id):
    db = get_db()
    db.execute("DELETE FROM net_schedules WHERE id=?", (net_id,))
    db.commit()
    return jsonify({"success": True})


@app.route("/api/echolink/search")
def api_echolink_search():
    """Search the cached EchoLink directory (see _echolink_poll_loop) by
    callsign substring or node number. Gated to logged-in users and up
    (see _USER_OR_ABOVE in check_auth) — this bridges RF under the club
    callsign same as any other connect action, so it isn't public."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": [], "updated": _echolink_cache_ts})
    q_lower = q.lower()
    with _echolink_cache_lock:
        snapshot, ts = list(_echolink_cache), _echolink_cache_ts
    matches = [r for r in snapshot if q_lower in r["call"].lower() or q in r["node"]][:50]
    resp = jsonify({"results": matches, "updated": ts})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/asl/search")
def api_asl_search():
    """Search the local astdb.txt node directory (see load_astdb) by node
    number, callsign, description, or location substring. Same login gate as
    the EchoLink search (_USER_OR_ABOVE in check_auth) — results feed the
    same connect flow. Unlike the EchoLink directory this lists every
    *registered* node, not just currently-online ones: astdb carries no
    online/offline signal, and per-result probes of stats.allstarlink.org
    are off the table (its API rate-limits bursts hard — see the pacing
    notes in _favstats_poll_loop), so the UI links each result to its
    stats page instead of claiming live status here."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": [], "total": 0})
    if not _astdb_loaded:
        load_astdb()
    q_lower = q.lower()
    matches = []
    for node, info in list(_astdb_cache.items()):
        if (q in node
                or q_lower in info["callsign"].lower()
                or q_lower in info["desc"].lower()
                or q_lower in info["location"].lower()):
            matches.append({"node": node,
                            "callsign": info["callsign"],
                            "desc":     info["desc"],
                            "location": info["location"]})

    # Exact node/callsign hits first, then prefix hits, then everything else;
    # numeric node order within each band (astdb node keys are all digits, so
    # len-then-string compares numerically).
    def _rank(m):
        exact  = m["node"] == q or m["callsign"].lower() == q_lower
        prefix = m["node"].startswith(q) or m["callsign"].lower().startswith(q_lower)
        return (0 if exact else 1 if prefix else 2, len(m["node"]), m["node"])
    matches.sort(key=_rank)

    resp = jsonify({"results": matches[:50], "total": len(matches)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── AllStarLink stats proxy ───────────────────────────────────────────────────

@app.route("/api/nodestats/<node>")
def api_node_stats(node):
    if not node.isdigit():
        return jsonify({"error": "Invalid node"}), 400
    try:
        url = ASL_STATS_URL.format(node)
        log("DEBUG", f"[API] Fetching stats for node {node} from {url}")
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
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
        node = str(node)
        if not node.isdigit():
            results[node] = {"error": "Invalid node"}
            continue
        try:
            url = ASL_STATS_URL.format(node)
            req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
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
    """Manual Disconnect Node / Disconnect All from the Dashboard's Connect /
    Disconnect panel. Uses ilink 11, not ilink 1 — see api_status_disconnect's
    docstring for why: ilink 1 silently no-ops on a permanent link."""
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
            r = ami.rpt_cmd(local_node, f"ilink 11 {remote_node}")
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
    """Manual Perm Connect from the Dashboard's Connect / Disconnect panel.
    Despite the name, `mode` isn't restricted to the two permanent ilink
    modes (12/13) — matching this endpoint's existing behavior — but only
    genuinely permanent modes get recorded in _kiosk_temp_conns/
    permanent_links, so the Status Board's Permanent badge reflects what
    app_rpt actually did, not what the button is called."""
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
        result = ami_send_command(_do)
        if result.get("success") and mode in ("12", "13"):
            monitor = (mode == "12")
            with _kiosk_temp_lock:
                _kiosk_temp_conns[(local_node, remote_node)] = {
                    'permanent':   True,
                    'monitor':     monitor,
                    'no_timeout':  False,
                    'last_active': time.time(),
                }
            db = get_db()
            db.execute(
                "INSERT OR REPLACE INTO permanent_links (local_node, peer_node, monitor) "
                "VALUES (?, ?, ?)",
                (local_node, remote_node, 1 if monitor else 0)
            )
            db.commit()
            _db_temp_conn_delete(local_node, remote_node)
        return jsonify(result)
    except Exception as e:
        log("ERROR", f"[API] /api/ami/perm_connect error: {e}")
        return jsonify({"error": str(e)}), 500


# ── System info API ───────────────────────────────────────────────────────────

@app.route("/api/sysinfo")
def api_sysinfo():
    if session.get("role") not in ("admin", "superuser", "owner"):
        return jsonify({"error": "Admin access required"}), 403
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
        "running_as":        pwd.getpwuid(os.getuid()).pw_name,
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

    db = get_db()
    connector_managed_pairs = {
        (r["local_node"], r["target_node"])
        for r in db.execute(
            "SELECT local_node, target_node FROM connectors WHERE enabled=1 AND state='connected'"
        ).fetchall()
    }
    locked_nodes = get_locked_nodes()
    active_announcements = db.execute(
        "SELECT COUNT(*) FROM announcements WHERE enabled=1"
    ).fetchone()[0]
    scheduled_connectors = db.execute(
        "SELECT COUNT(*) FROM connectors WHERE enabled=1 AND schedule_type != 'manual' AND connect_time IS NOT NULL"
    ).fetchone()[0]

    node_data = []
    for node in nodes:
        info     = lookup_node(node)
        cached   = get_cached_status(node)
        connected_details = []
        lcs = cached.get("link_connect_seconds", {})
        with _link_stats_lock:
            ls_snapshot = dict(_link_stats)
        node_str_local = str(node)
        idle_timeout   = int(get_setting('kiosk_idle_timeout_sec', '600') or 600)
        for cn in cached.get("connected", []):
            cn_info    = lookup_node(cn)
            cn_loc     = cn_info.get("location", "")
            cn_coords  = _geocode_nonblocking(cn_loc) if cn_loc else None
            ls = ls_snapshot.get(cn, {})
            # Idle-timeout info for kiosk display. 'permanent' (genuinely permanent
            # ilink 12/13, set only via the explicit Permanent checkbox at connect
            # time) and 'no_timeout' (idle-timeout exempted via the No Timeout
            # button, link mode untouched) are reported separately so the UI can
            # tell them apart instead of mislabeling a squashed link "Permanent".
            with _kiosk_temp_lock:
                tc = _kiosk_temp_conns.get((node_str_local, cn))
            if tc and tc.get('permanent'):
                idle_elapsed   = None
                idle_remaining = None
                is_permanent   = True
                no_timeout     = False
            elif tc and tc.get('no_timeout'):
                idle_elapsed   = None
                idle_remaining = None
                is_permanent   = False
                no_timeout     = True
            elif tc:
                idle_elapsed   = max(0, int(time.time() - tc['last_active']))
                idle_remaining = max(0, idle_timeout - idle_elapsed)
                is_permanent   = False
                no_timeout     = False
            else:
                idle_elapsed   = None
                idle_remaining = None
                is_permanent   = None  # not tracked (pre-existing connection)
                no_timeout     = None
            link_info = cached.get("links", {}).get(cn, {})
            connected_details.append({
                "node":           cn,
                "callsign":       cn_info.get("callsign", ""),
                "desc":           cn_info.get("desc", ""),
                "location":       cn_loc,
                "keyed":          link_info.get("keyed", False),
                "mode":           link_info.get("mode", ""),  # 'T' = transmit/transceive, 'R' = monitor/receive-only
                "connect_elapsed_sec": lcs.get(cn),
                "keyups":         ls.get("keyups", 0),
                "last_keyed":     ls.get("last_keyed"),
                "lat":            cn_coords["lat"] if cn_coords else None,
                "lon":            cn_coords["lon"] if cn_coords else None,
                "connect_state":  cached.get("link_connect_state", {}).get(cn, "ESTABLISHED"),
                "idle_elapsed":   idle_elapsed,
                "idle_remaining": idle_remaining,
                "permanent":      is_permanent,
                "no_timeout":     no_timeout,
                "connector_managed": (node_str_local, cn) in connector_managed_pairs,
            })
        location = info.get("location", "")
        coords   = _geocode_nonblocking(location) if location else None
        with _own_keyups_lock:
            own_keyups = _own_keyups.get(node, 0)
        node_data.append({
            "node":       node,
            "callsign":   info.get("callsign", ""),
            "desc":       info.get("desc", ""),
            "location":   location,
            "lat":        coords["lat"] if coords else None,
            "lon":        coords["lon"] if coords else None,
            "keyed":      cached.get("keyed", False),
            "own_keyups": own_keyups,
            "connected":  connected_details,
            "stale":      cached.get("stale", True),
            "locked":     node in locked_nodes,
            "locked_by":  locked_nodes.get(node, {}).get("locked_by"),
        })

    resp = jsonify({
        "nodes":            node_data,
        "asterisk_active":  ast_status["active"],
        "asterisk_version": ast_status["version"],
        "asl_version":      get_asl_version(),
        "uptime":           get_uptime(),
        "cpu_temp":         get_cpu_temp(),
        "disk":             get_disk_usage(),
        "ami_connected":    _ami_connected,
        "active_users":     get_active_user_count(),
        "connector_warning": _connector_upcoming_disconnect_all(),
        "active_announcements": active_announcements,
        "scheduled_connectors": scheduled_connectors,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/nodes/<node>/lockout", methods=["POST"])
def api_node_lockout(node):
    """
    Owner-only. Locking a node puts the whole system into a system-wide,
    read-only lockout (enforced once, centrally, in check_auth()) for every
    role but Owner — Superuser and Admin included. The lock state itself is
    tracked per node so the Owner can see/target which node they locked,
    even though the practical effect while any node is locked is global.
    """
    if session.get('role') != 'owner':
        return jsonify({"error": "Only the node owner can lock or unlock a node"}), 403
    content = read_conf_file(RPT_CONF_PATH)
    valid_nodes = get_node_numbers(content) if content else []
    if node not in valid_nodes:
        return jsonify({"error": f"Unknown node {node}"}), 400
    data   = request.json or {}
    locked = bool(data.get("locked", True))
    db = get_db()
    if locked:
        db.execute(
            "INSERT INTO node_lockouts (node, locked_by, locked_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(node) DO UPDATE SET locked_by=excluded.locked_by, locked_at=excluded.locked_at",
            (node, session.get('username', ''))
        )
    else:
        db.execute("DELETE FROM node_lockouts WHERE node=?", (node,))
    db.commit()
    log("INFO", f"[LOCKOUT] Node {node} {'locked' if locked else 'unlocked'} by {session.get('username', '')}")
    return jsonify({"ok": True, "node": node, "locked": locked})


@app.route("/api/status/active_users")
def api_status_active_users():
    """Superuser-only detail behind the footer's 'Logged in: N' count — who's
    actually logged in, their role, and how long each has been idle. Everyone
    else (including admins) only ever sees the plain count from api_status_board."""
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
    resp = jsonify({"users": get_active_sessions_detail()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    - global_nodes:   every node currently keyed network-wide, last 15 min
                      (polled every 2 min from stats.allstarlink.org/stats/keyed)
    """
    pin_min = int(get_setting('kiosk_map_pin_duration_min', '60') or 60)
    cutoff  = time.time() - pin_min * 60

    with _keyed_history_lock:
        keyed = [e for e in _keyed_history if e["ts"] >= cutoff]

    # Attach geocoordinates to recently-keyed entries. Non-blocking: cache hits
    # resolve instantly, misses queue for the background geocoder and fill in on
    # a later poll rather than stalling this request on Nominatim's rate limit.
    for entry in keyed:
        if entry["lat"] is None and entry["location"]:
            coords = _geocode_nonblocking(entry["location"])
            if coords:
                entry["lat"] = coords["lat"]
                entry["lon"] = coords["lon"]

    # Global nodes always use a fixed 15-minute window, independent of the
    # (locally configurable) pin duration used for this node's own keyed history.
    global_cutoff = time.time() - _GLOBAL_DISPLAY_WINDOW_SEC
    with _global_nodes_lock:
        global_nodes = [e for e in _global_nodes_cache if e["ts"] >= global_cutoff]
        global_ts    = _global_nodes_ts

    resp = jsonify({
        "recently_keyed":     keyed,
        "global_nodes":       global_nodes,
        "global_updated":     global_ts,
        "pin_duration_min":   pin_min,
        "global_window_min":  _GLOBAL_DISPLAY_WINDOW_SEC / 60,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    # Only admin/superuser may request a permanent connection
    caller_role = session.get('role', '')
    # Kiosk/user accounts are limited to one connection at a time — the
    # Status Board UI disables Connect/Monitor once any node is connected,
    # but that's client-side only, so enforce it here too (a user-role
    # session could otherwise hit this endpoint directly and stack links).
    if caller_role == 'user' and get_cached_status(local_node).get('connected'):
        return jsonify({"error": "Only one connection at a time is allowed for your account. Disconnect the current node first."}), 403
    permanent   = bool(data.get("permanent", False)) and caller_role in ('admin', 'superuser', 'owner')
    monitor     = bool(data.get("monitor", False))
    # ilink 3  = transient transceive (default — remote can drop it, no auto-reconnect)
    # ilink 13 = permanent transceive (auto-reconnects if far end drops it)
    # ilink 2  = transient monitor (listen-only)
    # ilink 12 = permanent monitor
    if permanent and monitor:
        ilink_mode = "12"
    elif permanent:
        ilink_mode = "13"
    elif monitor:
        ilink_mode = "2"
    else:
        ilink_mode = "3"
    try:
        # Via ami_send_command (not a raw _ami_ensure_connected) so a
        # connection that broke since the last poll gets invalidated and
        # retried on a fresh socket instead of failing the user's click.
        result = ami_send_command(
            lambda ami: ami.rpt_cmd(local_node, f"ilink {ilink_mode} {remote_node}"))
        now = time.time()
        with _kiosk_temp_lock:
            _kiosk_temp_conns[(local_node, remote_node)] = {
                'permanent':   permanent,
                'monitor':     monitor,
                'no_timeout':  False,
                'last_active': now,
            }
        if permanent:
            db = get_db()
            db.execute(
                "INSERT OR REPLACE INTO permanent_links (local_node, peer_node, monitor) "
                "VALUES (?, ?, ?)",
                (local_node, remote_node, 1 if monitor else 0)
            )
            db.commit()
            _db_temp_conn_delete(local_node, remote_node)
        else:
            _db_temp_conn_save(local_node, remote_node, monitor, False, now)
        log("INFO", f"[API] /api/status/connect {local_node} -> {remote_node} mode=ilink{ilink_mode}")
        return jsonify({"ok": True, "output": result})
    except Exception as e:
        log("ERROR", f"[API] /api/status/connect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status/disconnect", methods=["POST"])
def api_status_disconnect():
    """Disconnect a remote node from a local node — public endpoint for the Status Board.

    Uses ilink 11 ("Perm Link off"), not ilink 1 ("Link off"). Per app_rpt's
    function_ilink() (apps/app_rpt/rpt_functions.c): cases 1 and 11 share the
    same code path, but 1 has a guard — `if (myatoi(param) < 10 &&
    l->max_retries > MAX_RETRIES) return DC_COMPLETE;` — that silently no-ops
    on a permanent link (id < 10 is exactly the ilink-1 case) instead of
    disconnecting it. ilink 11 hits the same guard's false branch and always
    proceeds to the real disconnect, for both transient and permanent links,
    so it's used here unconditionally rather than only when we happen to know
    (via _kiosk_temp_conns) that a given link is permanent — a link made
    permanent outside this app (raw AMI/CLI, rpt.conf) isn't tracked there
    either, and would hit this same bug.
    """
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    # User/kiosk accounts may not tear down a link that Smart Connector is
    # actively managing on a schedule — only admin/superuser can. Checked
    # against live connector state (not client-supplied data) so a user-role
    # session can't just omit the info to bypass this.
    caller_role = session.get('role', '')
    if caller_role not in ('admin', 'superuser', 'owner'):
        db = get_db()
        # Restricted user accounts (User Management → "Restrict disconnect")
        # may listen/TX and make the first connection like any user account,
        # but can never tear one down — checked live against the DB, not
        # cached in the session, so a mid-session restriction change takes
        # effect on the very next attempt.
        urow = db.execute("SELECT restrict_disconnect FROM users WHERE id=?",
                          (session.get('user_id'),)).fetchone()
        if urow and urow["restrict_disconnect"]:
            return jsonify({"error": "Your account isn't permitted to disconnect nodes."}), 403
        managed = db.execute(
            "SELECT 1 FROM connectors WHERE enabled=1 AND state='connected' "
            "AND local_node=? AND target_node=?",
            (local_node, remote_node)
        ).fetchone()
        if managed:
            return jsonify({"error": "This connection is managed by Smart Connector. "
                                      "Only an admin or superuser can disconnect it."}), 403
    try:
        result = ami_send_command(
            lambda ami: ami.rpt_cmd(local_node, f"ilink 11 {remote_node}"))
        log("INFO", f"[API] /api/status/disconnect {local_node} -> {remote_node}")
        return jsonify({"ok": True, "output": result})
    except Exception as e:
        log("ERROR", f"[API] /api/status/disconnect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status/squash-idle", methods=["POST"])
def api_status_squash_idle():
    """Exempt an existing connection from the kiosk idle-timeout auto-disconnect.

    This must NOT change the connection's ilink mode. It previously re-issued
    ilink 12/13 (genuinely permanent — app_rpt persists it and auto-reconnects
    if the far end drops it), conflating "stop auto-disconnecting this" with
    "make this a permanent link" under the same 'permanent' flag. The kiosk
    should never promote a connection to permanent unless a user explicitly
    requests that (the separate Permanent checkbox, admin/superuser-gated, at
    connect time) — so this only sets a distinct 'no_timeout' flag that the
    idle-timeout poller skips, and issues no AMI command at all.
    """
    if session.get('role') not in ('admin', 'superuser', 'owner'):
        return jsonify({"error": "Admin access required"}), 403
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    key = (local_node, remote_node)
    with _kiosk_temp_lock:
        if key in _kiosk_temp_conns:
            _kiosk_temp_conns[key]['no_timeout'] = True
        else:
            _kiosk_temp_conns[key] = {'permanent': False, 'monitor': False,
                                      'no_timeout': True, 'last_active': time.time()}
        info = dict(_kiosk_temp_conns[key])
    if not info.get('permanent'):
        _db_temp_conn_save(local_node, remote_node, info.get('monitor', False),
                           True, info['last_active'])
    log("INFO", f"[API] /api/status/squash-idle {local_node} -> {remote_node}: "
                "idle timeout disabled (link mode unchanged)")
    return jsonify({"ok": True})


@app.route("/api/status/restore-idle", methods=["POST"])
def api_status_restore_idle():
    """Undo /api/status/squash-idle — re-enable the idle-timeout auto-disconnect
    for a connection that was exempted from it. Resets the idle clock to now,
    since time spent exempted was never counted as elapsed idle time."""
    if session.get('role') not in ('admin', 'superuser', 'owner'):
        return jsonify({"error": "Admin access required"}), 403
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    key = (local_node, remote_node)
    with _kiosk_temp_lock:
        if key not in _kiosk_temp_conns or _kiosk_temp_conns[key].get('permanent'):
            return jsonify({"error": "Not a tracked temporary connection"}), 404
        _kiosk_temp_conns[key]['no_timeout']  = False
        _kiosk_temp_conns[key]['last_active'] = time.time()
        info = dict(_kiosk_temp_conns[key])
    _db_temp_conn_save(local_node, remote_node, info.get('monitor', False),
                       False, info['last_active'])
    log("INFO", f"[API] /api/status/restore-idle {local_node} -> {remote_node}: idle timeout re-enabled")
    return jsonify({"ok": True})


@app.route("/api/status/reset-idle", methods=["POST"])
def api_status_reset_idle():
    """Reset the idle-timeout clock for a temporary connection back to zero, without
    making it permanent — the connection still auto-disconnects after the configured
    idle timeout, just measured from now instead of whenever it last went idle."""
    if session.get('role') not in ('admin', 'superuser', 'owner'):
        return jsonify({"error": "Admin access required"}), 403
    data        = request.json or {}
    local_node  = str(data.get("local_node",  "")).strip()
    remote_node = str(data.get("remote_node", "")).strip()
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local_node"}), 400
    if not re.match(r'^\d{4,7}$', remote_node):
        return jsonify({"error": "Invalid remote_node"}), 400
    key = (local_node, remote_node)
    with _kiosk_temp_lock:
        if key not in _kiosk_temp_conns or _kiosk_temp_conns[key].get('permanent'):
            return jsonify({"error": "Not a tracked temporary connection"}), 404
        _kiosk_temp_conns[key]['last_active'] = time.time()
        info = dict(_kiosk_temp_conns[key])
    _db_temp_conn_save(local_node, remote_node, info.get('monitor', False),
                       info.get('no_timeout', False), info['last_active'])
    log("INFO", f"[API] /api/status/reset-idle {local_node} -> {remote_node}: idle timer reset")
    return jsonify({"ok": True})


# ── Browser transmit (WebRTC → PJSIP → Rpt phone-control mode) ────────────────
# The kiosk's TX feature never routes audio through this process: the browser
# registers directly to Asterisk's PJSIP stack over WSS (Apache-proxied to the
# loopback-only builtin HTTP server) and calls the phone-portal extension
# 2<node> (PTT = DTMF *99, unkey = #). HenWen's only job is handing the SIP
# credentials to a sufficiently-privileged logged-in session. The Asterisk side
# is set up by tx-spike/apply.sh; if its secret file is missing the feature is
# simply not configured and the kiosk hides the TX button.
TX_SECRET_PATH = os.environ.get("TX_SECRET_PATH", "/etc/asterisk/henwen-tx.secret")
TX_SIP_USER    = os.environ.get("TX_SIP_USER", "henwen-tx")
TX_WS_PATH     = os.environ.get("TX_WS_PATH", "/asterisk-ws")


@app.route("/api/tx/config")
def api_tx_config():
    """SIP credentials for the browser transmitter. Any logged-in role — this
    keys an RF transmitter under the club callsign, but account creation
    itself is already gated to admin/superuser/owner, so any account that
    exists has already been vetted to transmit; login alone (enforced by
    _USER_OR_ABOVE in check_auth) is the real gate here. ?probe=1 answers
    availability only (no secret, no log line) for deciding button visibility."""
    if not session.get('logged_in'):
        return jsonify({"error": "Authentication required"}), 401
    try:
        with open(TX_SECRET_PATH) as f:
            secret = f.read().strip()
    except OSError:
        secret = ""
    if not secret:
        return jsonify({"enabled": False}), 404
    if request.args.get("probe"):
        return jsonify({"enabled": True})
    content = read_conf_file(RPT_CONF_PATH)
    nodes   = get_node_numbers(content) if content else []
    if not nodes:
        return jsonify({"error": "No local node configured"}), 500
    node = str(nodes[0])
    log("INFO", f"[TX] Browser-TX credentials issued to {session.get('username', '?')} "
                f"for node {node}")
    resp = jsonify({
        "enabled":  True,
        "username": TX_SIP_USER,
        "password": secret,
        "ws_path":  TX_WS_PATH,
        "dial":     "2" + node,
        "node":     node,
    })
    # Live SIP credential in the body — keep it out of shared-kiosk disk caches.
    resp.headers["Cache-Control"] = "no-store"
    return resp


TX_CHECK_PORTS_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tx-spike", "check-ports.sh"
)


@app.route("/api/tx/diagnostics")
def api_tx_diagnostics():
    """One-click check of everything the browser TX button needs, for the
    Manager's TX Diagnostics page. Admin/superuser only, same as
    /api/tx/config. Two layers:
      1. Fast in-process checks (secret file, PJSIP endpoint, local node,
         whether *this* request itself arrived over HTTPS) covering "was
         apply.sh/setup-https.sh ever run at all".
      2. tx-spike/check-ports.sh (read-only, changes nothing) covering the
         network/NAT layer neither of the setup scripts can verify for
         themselves — reused rather than reimplemented so the two never
         drift apart.
    """
    if session.get('role') not in ('admin', 'superuser', 'owner'):
        return jsonify({"error": "Admin access required"}), 403

    checks = []

    def add(label, ok, detail, warn=False):
        checks.append({
            "label": label,
            "status": "warn" if warn else ("pass" if ok else "fail"),
            "detail": detail,
        })

    try:
        with open(TX_SECRET_PATH) as f:
            secret_configured = bool(f.read().strip())
    except OSError:
        secret_configured = False
    add("Browser TX configured", secret_configured,
        "SIP secret present at " + TX_SECRET_PATH if secret_configured else
        "Not set up yet — run: sudo bash /opt/HenWen/tx-spike/apply.sh")

    content = read_conf_file(RPT_CONF_PATH)
    nodes   = get_node_numbers(content) if content else []
    add("Local node found in rpt.conf", bool(nodes),
        f"Node {nodes[0]}" if nodes else "No [NNNN] stanza found in rpt.conf")

    is_https = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    add("This page loaded over HTTPS", is_https,
        "Secure context confirmed" if is_https else
        "You're viewing the Manager over plain HTTP — the TX button needs "
        "https:// (or localhost) to get microphone access at all",
        warn=not is_https)

    try:
        out = ami_send_command(
            lambda ami: ami.command(f"pjsip show endpoint {TX_SIP_USER}"))
        found = any(l.strip().startswith("Endpoint:") and TX_SIP_USER in l for l in out)
        add("PJSIP endpoint registered in Asterisk", found,
            f"'{TX_SIP_USER}' endpoint present" if found else
            f"No '{TX_SIP_USER}' endpoint — run tx-spike/apply.sh (or re-run it if it was rolled back)")
    except Exception as e:
        add("PJSIP endpoint registered in Asterisk", False, f"Could not query AMI: {e}")

    script_output = ""
    if not os.path.isfile(TX_CHECK_PORTS_SCRIPT):
        add("Network/port checks (check-ports.sh)", False,
            "tx-spike/check-ports.sh not found on disk — reinstall or update HenWen", warn=True)
    else:
        try:
            proc = subprocess.run(["bash", TX_CHECK_PORTS_SCRIPT],
                                   capture_output=True, text=True, timeout=30)
            script_output = proc.stdout + proc.stderr
            for line in script_output.splitlines():
                m = re.match(r'^\s*(PASS|FAIL|WARN)\s+(.*)$', line)
                if m:
                    status, detail = m.group(1), m.group(2).strip()
                    add(detail, status == "PASS", detail, warn=(status == "WARN"))
        except subprocess.TimeoutExpired:
            add("Network/port checks (check-ports.sh)", False, "Timed out after 30s")
        except Exception as e:
            add("Network/port checks (check-ports.sh)", False, str(e))

    summary = {
        "pass": sum(1 for c in checks if c["status"] == "pass"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
    }
    log("INFO", f"[TX-DIAG] {session.get('username', '?')} ran TX diagnostics: "
                f"{summary['pass']} pass, {summary['fail']} fail, {summary['warn']} warn")
    return jsonify({"checks": checks, "summary": summary, "raw_output": script_output})


# ---------------------------------------------------------------------------
# Audio monitoring — one ffmpeg per node, broadcast to N simultaneous clients
#
# Architecture:
#   Asterisk MixMonitor → in-FIFO → audio_relay.py (own process) → paced-FIFO
#                                                                       ↓
#                                                              ffmpeg (reads
#                                                              paced-FIFO directly)
#                                                                       ↓
#                                                         _read_loop fans stdout
#                                                         to N client queues
#
# The frame-pacing relay (audio_relay.py) used to run as a Python thread
# inside this gunicorn worker, sharing a GIL with Flask request handlers,
# the AMI poller, and every other background thread. Any of those holding
# the GIL for even a few milliseconds during a 20 ms frame window delayed
# the next write — audibly, as a click or stutter. It now runs as its own
# OS process so the kernel schedules its real-time frame loop independently
# of everything else this worker is doing; ffmpeg reads the paced output
# directly from a second FIFO rather than via a stdin pipe fed from within
# this process.
#
# The relay still injects 20 ms silence frames whenever the node is quiet:
# without continuous output, a silent node produces no TCP data and any NAT
# device between the browser and server (home router, ISP, VPN) will drop
# the idle connection after 30–90 s.
#
# ffmpeg stderr is captured and emitted at WARN so codec errors, format
# mismatches, etc. appear in journalctl.
#
# A WebM byte stream is only decodable from its very start: the init segment
# (EBML header + Segment Info + Tracks) precedes the first Cluster, and a
# browser joining mid-stream without it fails its first append outright
# (Chrome: CHUNK_DEMUXER_ERROR_APPEND_FAILED "Found Cluster element before
# Info"). _read_loop therefore parses ffmpeg's output just enough to capture
# the init segment once and split the rest on Cluster boundaries: every
# client queue is seeded with the cached init segment on join, then receives
# whole ~200 ms Clusters (see _drain_webm).
# ---------------------------------------------------------------------------
import queue as _queue_mod

_WEBM_CLUSTER_ID = b'\x1f\x43\xb6\x75'   # EBML ID of a Matroska/WebM Cluster


def _webm_vint_len(first_byte):
    """Length in bytes (1–8) of an EBML variable-size integer whose first
    byte is `first_byte`, or 0 if invalid (no length-marker bit set)."""
    for i in range(8):
        if first_byte & (0x80 >> i):
            return i + 1
    return 0

_audio_lock   = threading.Lock()
_audio_active = {}   # node -> _AudioBroadcast

_AUDIO_RELAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_relay.py')


def _node_rxchannel(node):
    """The node's effective `rxchannel` setting from rpt.conf, resolving
    template inheritance (parse_stanza_settings already handles that). Used
    by _find_node_channel() as a config-driven fallback: a node configured
    with e.g. `rxchannel = Local/pseudo` (simplex-only, no attached
    radio/USRP/voter hardware) never puts its node number anywhere in the
    live channel name, Location, or Application columns the other match
    strategies scan for — the channel is always literally named after
    rxchannel's own value instead (e.g. `Local/pseudo@default-00000000;1`).
    Returns None if unset, unreadable, or commented out."""
    content = read_conf_file(RPT_CONF_PATH)
    if content is None:
        return None
    entry = parse_stanza_settings(content, str(node)).get("rxchannel")
    if not entry or entry.get("commented"):
        return None
    return entry.get("value", "").strip() or None


def _find_node_channel(node):
    """
    Return the Asterisk channel name for a local node by scanning
    'core show channels'. Matches a channel name containing '/<node>'
    (radio/USB-style local channels), a Location/Application column
    identifying the node (e.g. a trunked IAX2 channel running Rpt(<node>),
    where the node number never appears in the channel name itself), or —
    when neither of those find anything — a channel name literally starting
    with the node's configured rxchannel value (see _node_rxchannel()).
    Returns None if not found.
    """
    def _cmd(ami):
        return {'lines': ami.command('core show channels')}
    try:
        lines  = ami_send_command(_cmd).get('lines', [])
        rxchan = _node_rxchannel(node)
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            if f'/{node}' in parts[0]:
                return parts[0]
            if len(parts) > 1 and parts[1].startswith(f'{node}@'):
                return parts[0]
            if f'({node})' in line:
                return parts[0]
            if rxchan and parts[0].startswith(rxchan):
                return parts[0]
    except Exception as e:
        log('WARN', f'[AUDIO] channel search failed: {e}')
    return None


class _AudioBroadcast:
    """
    Owns one ffmpeg process (plus its dedicated audio_relay.py pacing process)
    and fans ffmpeg's WebM/Opus output to any number of simultaneous HTTP
    streaming clients, each backed by its own Queue.

    Lifecycle:
      - Created by the first listener for a node.
      - relay_proc (audio_relay.py) paces MixMonitor's raw PCM into a second
        FIFO that ffmpeg reads directly — see the module-level comment above
        for why this runs out-of-process rather than as a thread here.
      - _read_loop reads ffmpeg stdout and puts chunks into every client queue.
      - _stderr_loop drains ffmpeg stderr to the application log.
      - _relay_stderr_loop drains the relay process's stderr to the log.
      - When a client disconnects, remove_client() is called; if it was the
        last one, shutdown() tears everything down.
    """

    def __init__(self, node, channel, ffmpeg_proc, relay_proc):
        self.node        = node
        self.channel     = channel
        self.ffmpeg_proc = ffmpeg_proc
        self.relay_proc  = relay_proc
        self._lock       = threading.Lock()
        self._clients    = []       # list of Queue
        self._client_meta = {}      # id(q) -> {'remote':.., 'connected_at':.., 'drops':0}
        self._dead       = False
        self._init_segment = None   # WebM init segment (EBML header..Tracks),
                                    # captured by _drain_webm and replayed to
                                    # every client that joins after it went by

        self._started_at   = time.monotonic()
        self._chunks_out   = 0
        self._bytes_out    = 0
        self._total_drops  = 0

        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f'audio-reader-{node}')
        self._stderr = threading.Thread(target=self._stderr_loop, daemon=True,
                                        name=f'audio-stderr-{node}')
        self._relay_stderr = threading.Thread(target=self._relay_stderr_loop, daemon=True,
                                        name=f'audio-relay-stderr-{node}')
        self._stats  = threading.Thread(target=self._stats_loop, daemon=True,
                                        name=f'audio-stats-{node}')
        self._reader.start()
        self._stderr.start()
        self._relay_stderr.start()
        self._stats.start()

    # ── client management ────────────────────────────────────────────────────

    def add_client(self, remote_addr='?'):
        q = _queue_mod.Queue(maxsize=25)  # ~5 s of ~200 ms Clusters; drop oldest if client stalls
        with self._lock:
            if self._init_segment is not None:
                # Late joiner: the stream's init segment already went by.
                # Seed it first so the browser's demuxer sees a valid WebM
                # start instead of a bare mid-stream Cluster (which Chrome
                # rejects with CHUNK_DEMUXER_ERROR_APPEND_FAILED).
                q.put_nowait(self._init_segment)
            self._clients.append(q)
            self._client_meta[id(q)] = {
                'remote':       remote_addr,
                'connected_at': time.monotonic(),
                'drops':        0,
            }
            count = len(self._clients)
        log('INFO', f'[AUDIO] {remote_addr} connected to node {self.node} '
                    f'({count} total listener(s))')
        return q

    def remove_client(self, q, remote_addr='?'):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass
            meta = self._client_meta.pop(id(q), None)
            remaining = len(self._clients)
        duration = time.monotonic() - meta['connected_at'] if meta else 0.0
        drops    = meta['drops'] if meta else 0
        log('INFO', f'[AUDIO] {remote_addr} disconnected from node {self.node} '
                    f'after {duration:.1f}s ({drops} frame(s) dropped for this client), '
                    f'{remaining} listener(s) remaining')
        if remaining == 0:
            self.shutdown()

    def has_client(self, remote_addr):
        with self._lock:
            return any(meta['remote'] == remote_addr
                       for meta in self._client_meta.values())

    # ── internal ─────────────────────────────────────────────────────────────

    def _fanout(self, item):
        with self._lock:
            for q in self._clients:
                try:
                    q.put_nowait(item)
                except _queue_mod.Full:
                    try: q.get_nowait()   # drop oldest frame
                    except _queue_mod.Empty: pass
                    try: q.put_nowait(item)
                    except _queue_mod.Full: pass
                    meta = self._client_meta.get(id(q))
                    if meta is not None:
                        meta['drops'] += 1
                    self._total_drops += 1
                    if meta and meta['drops'] % 50 == 1:
                        # Log the 1st, 51st, 101st... drop per client so a
                        # slow/stalled listener is visible without spamming
                        # the log once per dropped 20ms frame.
                        log('DEBUG', f'[AUDIO] queue full for {meta["remote"]} on '
                                    f'node {self.node}, dropping oldest frame '
                                    f'(client is {meta["drops"]} frame(s) behind)')

    def _stats_loop(self):
        """
        Every 10s while the broadcast is alive, log a heartbeat: uptime,
        total bytes/chunks shipped, current listener count, and per-client
        queue depth (how far each client is from being caught up) — the
        thing you actually want visibility into over a long-running stream,
        since a slow client shows up here as a growing queue depth well
        before it starts audibly dropping frames.
        """
        while True:
            time.sleep(10.0)
            with self._lock:
                if self._dead:
                    return
                depths = [(self._client_meta.get(id(q), {}).get('remote', '?'), q.qsize())
                          for q in self._clients]
            uptime = time.monotonic() - self._started_at
            log('DEBUG', f'[AUDIO] heartbeat node={self.node} uptime={uptime:.0f}s '
                        f'listeners={len(depths)} chunks_out={self._chunks_out} '
                        f'bytes_out={self._bytes_out} total_drops={self._total_drops} '
                        f'queue_depths={depths}')

    def _stderr_loop(self):
        """Forward ffmpeg stderr to the application log at WARN level."""
        try:
            for raw_line in self.ffmpeg_proc.stderr:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                if line:
                    log('WARN', f'[AUDIO] ffmpeg [{self.node}]: {line}')
        except Exception:
            pass

    def _relay_stderr_loop(self):
        """
        Forward the audio_relay.py process's stderr to the app log.

        Lines prefixed 'STATS' are the relay's periodic frame/silence/drift
        heartbeat (only emitted when AUDIO_RELAY_DEBUG=1, i.e. when we're
        running at LOG_LEVEL=DEBUG) and are logged at DEBUG; anything else
        from that process is unexpected and logged at WARN.
        """
        try:
            for raw_line in self.relay_proc.stderr:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                if not line:
                    continue
                if line.startswith('STATS '):
                    log('DEBUG', f'[AUDIO-RELAY] [{self.node}]: {line[len("STATS "):]}')
                else:
                    log('WARN', f'[AUDIO] relay [{self.node}]: {line}')
        except Exception:
            pass

    def _drain_webm(self, buf):
        """
        Split accumulated ffmpeg output into WebM-structural units, mutating
        `buf` in place (an incomplete tail stays behind for the next read).

        The bytes before the first Cluster are the stream's init segment
        (EBML header + Segment Info + Tracks); it is captured once into
        self._init_segment and delivered here to clients already connected,
        while add_client() replays it to every later joiner — a WebM stream
        is not decodable from any other starting point.

        Returns a list of complete Clusters (~200 ms of audio each, per
        -cluster_time_limit) to fan out. Whole-Cluster granularity means a
        late joiner starts on a decodable boundary and a stalled client's
        drop-oldest queue loses whole Clusters instead of shearing the byte
        stream mid-element (the client SourceBuffer runs in 'sequence' mode,
        which splices over a missing Cluster seamlessly).
        """
        items = []
        while True:
            if self._init_segment is None:
                i = buf.find(_WEBM_CLUSTER_ID)
                if i == -1:
                    break                      # header still incomplete
                init = bytes(buf[:i])
                del buf[:i]
                with self._lock:
                    # Set and deliver under one lock hold so a concurrently
                    # added client gets the init segment exactly once
                    # (add_client seeds it only when it is already set).
                    self._init_segment = init
                    for q in self._clients:
                        try:
                            q.put_nowait(init)
                        except _queue_mod.Full:
                            pass
                continue
            if len(buf) < 5:
                break                          # need Cluster ID + size byte
            if buf[:4] != _WEBM_CLUSTER_ID:
                # Lost sync (shouldn't happen — this muxer emits known-size
                # Clusters): resync by scanning for the next Cluster ID.
                j = buf.find(_WEBM_CLUSTER_ID, 1)
                if j == -1:
                    break
                items.append(bytes(buf[:j]))
                del buf[:j]
                continue
            vlen = _webm_vint_len(buf[4])
            if vlen == 0 or len(buf) < 4 + vlen:
                if vlen == 0:
                    # Invalid size byte — treat as lost sync.
                    j = buf.find(_WEBM_CLUSTER_ID, 4)
                    if j != -1:
                        items.append(bytes(buf[:j]))
                        del buf[:j]
                        continue
                break
            size = buf[4] & (0xFF >> vlen)     # size value, marker bit cleared
            for b in buf[5:4 + vlen]:
                size = (size << 8) | b
            if size == (1 << (7 * vlen)) - 1:
                # Unknown-size Cluster: it ends only where the next one starts.
                j = buf.find(_WEBM_CLUSTER_ID, 4)
                if j == -1:
                    break
                items.append(bytes(buf[:j]))
                del buf[:j]
                continue
            total = 4 + vlen + size
            if len(buf) < total:
                break
            items.append(bytes(buf[:total]))
            del buf[:total]
        return items

    def _read_loop(self):
        first_chunk = True
        buf = bytearray()
        try:
            while True:
                chunk = self.ffmpeg_proc.stdout.read1(2048)
                if not chunk:
                    log('DEBUG', f'[AUDIO] ffmpeg stdout EOF for node {self.node} '
                                f'after {self._chunks_out} chunk(s), '
                                f'{self._bytes_out} byte(s)')
                    break
                if first_chunk:
                    ttfb = time.monotonic() - self._started_at
                    log('DEBUG', f'[AUDIO] first ffmpeg chunk for node {self.node} '
                                f'after {ttfb:.3f}s ({len(chunk)} bytes)')
                    first_chunk = False
                self._chunks_out += 1
                self._bytes_out  += len(chunk)
                buf += chunk
                for item in self._drain_webm(buf):
                    self._fanout(item)
        finally:
            if buf:
                self._fanout(bytes(buf))   # flush the partial tail at EOF
            self._fanout(None)   # unblock every waiting generate()
            rc = self.ffmpeg_proc.poll()
            uptime = time.monotonic() - self._started_at
            log('INFO', f'[AUDIO] ffmpeg for node {self.node} exited '
                        f'(returncode={rc}) after {uptime:.1f}s, '
                        f'{self._chunks_out} chunk(s), {self._bytes_out} byte(s) total')

    def shutdown(self):
        with self._lock:
            if self._dead:
                return
            self._dead = True
        uptime = time.monotonic() - self._started_at
        log('DEBUG', f'[AUDIO] shutting down broadcast for node {self.node}: '
                    f'uptime={uptime:.1f}s chunks_out={self._chunks_out} '
                    f'bytes_out={self._bytes_out} total_drops={self._total_drops}')

        with _audio_lock:
            if _audio_active.get(self.node) is self:
                del _audio_active[self.node]

        self._fanout(None)
        with self._lock:
            self._clients.clear()

        # Stop the relay process first: it holds the only writer fd on the
        # paced FIFO, so once it exits ffmpeg's read hits EOF and it flushes
        # and exits on its own. We still explicitly wait/terminate both below
        # rather than relying purely on that cascade.
        try:
            self.relay_proc.terminate()
            self.relay_proc.wait(timeout=2)
        except Exception:
            try:
                self.relay_proc.kill()
                self.relay_proc.wait(timeout=2)
            except Exception:
                pass

        try:
            self.ffmpeg_proc.wait(timeout=3)
        except Exception:
            try:
                self.ffmpeg_proc.terminate()
                self.ffmpeg_proc.wait(timeout=2)
            except Exception:
                pass

        # Paths were stashed as attrs by _start_broadcast() after construction
        for fifo_path in (getattr(self, '_fifo_in_path', None),
                          getattr(self, '_fifo_out_path', None)):
            if fifo_path:
                try:
                    os.unlink(fifo_path)
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
    Apply MixMonitor to the node's existing Asterisk channel, spawn the
    audio_relay.py pacing process (MixMonitor FIFO -> paced FIFO), then
    point ffmpeg directly at the paced FIFO -> WebM/Opus -> HTTP clients.
    Raises on error.
    """
    _t0 = time.monotonic()
    channel = _find_node_channel(node)
    if not channel:
        raise RuntimeError(
            f'No active Asterisk channel found for node {node}. '
            'Is the node running?'
        )
    log('INFO', f'[AUDIO] found channel {channel!r} for node {node} '
                f'({time.monotonic() - _t0:.3f}s)')

    fifo_in_path  = f'/tmp/henwen_audio_{node}.sln'
    fifo_out_path = f'/tmp/henwen_audio_{node}_paced.sln'
    for p in (fifo_in_path, fifo_out_path):
        if os.path.exists(p):
            os.unlink(p)
        os.mkfifo(p)
        os.chmod(p, 0o666)   # asterisk user must be able to write
    log('DEBUG', f'[AUDIO] FIFOs created: {fifo_in_path}, {fifo_out_path}')

    # audio_relay.py opens both FIFOs itself (O_RDWR, so it never blocks
    # waiting for MixMonitor/ffmpeg to open their ends first) and paces
    # MixMonitor's raw PCM into fifo_out_path as strict 20 ms frames,
    # injecting silence whenever the node is quiet. It runs as its own OS
    # process specifically so its real-time frame loop is scheduled by the
    # kernel rather than competing for this gunicorn worker's GIL — see the
    # module-level comment above.
    _relay_env = os.environ.copy()
    if LOG_LEVEL == 'DEBUG':
        # Only ask the relay for its per-5s frame/silence/drift heartbeat when
        # we're actually running at DEBUG ourselves — otherwise the lines
        # would just be dropped by _relay_stderr_loop below anyway.
        _relay_env['AUDIO_RELAY_DEBUG'] = '1'
    relay_proc = subprocess.Popen(
        [sys.executable, _AUDIO_RELAY_SCRIPT, fifo_in_path, fifo_out_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_relay_env,
    )
    log('DEBUG', f'[AUDIO] relay PID {relay_proc.pid} for node {node} '
                f'(heartbeat={"on" if _relay_env.get("AUDIO_RELAY_DEBUG") else "off"})')

    # ffmpeg reads the already-paced PCM directly from fifo_out_path (a normal
    # blocking open on the read side waits for audio_relay.py's writer to open
    # its end, which happens immediately above — no race).
    #
    # -application audio (NOT voip): the 'voip' mode enables Voice Activity
    # Detection (VAD) and Discontinuous Transmission (DTX).  Ham radio audio
    # has CTCSS tones, noise floor, and intermittent speech that VAD classifies
    # as "not voice", producing near-silent comfort-noise frames.  'audio' mode
    # disables all voice-specific processing and encodes the signal as-is.
    #
    # WebM container (not Ogg): browsers handle live WebM streams more reliably.
    # -cluster_time_limit 200 forces a new WebM cluster every 200 ms so the
    # browser receives ~600-byte chunks 5×/s instead of ~300-byte chunks 10×/s.
    # Fewer appendBuffer() calls per second reduces browser-side MSE overhead.
    # _drain_webm also relies on this: it makes the muxer emit complete,
    # known-size Clusters, which is what lets the fan-out align on Cluster
    # boundaries for late joiners.
    # -probesize 32 / -analyzeduration 0: skip format probing so encoding
    # starts immediately (avoids several-hundred-ms startup silence).
    #
    # -af AGC chain: hot nodes (loud mic/RX audio) were clipping on speech
    # peaks. dynaudnorm rides the gain to keep the signal near its target
    # peak (quiet stretches brought up, loud stretches brought down);
    # alimiter is a true-peak brick wall behind it in case a fast transient
    # gets past dynaudnorm's smoothing. level=false on alimiter is load-
    # bearing — alimiter's default level=true re-normalizes output loudness
    # to match input, which was measured to silently undo the limiting on a
    # fully-clipped test signal (peak stayed pinned at full scale). Verified
    # against a synthetic clipped-tone fixture pushed through this exact
    # chain incl. the Opus round-trip: 0 clipped samples, peak ~90% FS, and
    # the filter's gaussian-window smoothing added only ~5ms of onset delay
    # — negligible against the stream's ~0.75s live-edge target. If this
    # sounds like it's pumping/over-processing on your traffic, the first
    # things to relax are m (max gain) down or f (frame length) up.
    ffmpeg_cmd = [
        'ffmpeg', '-loglevel', 'warning',
        '-probesize', '32',
        '-analyzeduration', '0',
        '-fflags', '+nobuffer',
        '-f', 's16le', '-ar', '8000', '-ac', '1', '-channel_layout', 'mono',
        '-i', fifo_out_path,
        '-af', 'dynaudnorm=f=200:g=15:p=0.95:m=6,'
               'alimiter=limit=0.85:attack=5:release=50:level=false',
        '-ar', '48000',        # resample to Opus native rate before encoding
        '-c:a', 'libopus', '-b:a', '24k',
        '-vbr', 'off',         # CBR — keep a steady ~24 kbps byte flow even when the node
                               # is quiet, so the injected silence actually keeps the
                               # connection alive through NAT/proxies. With VBR, silence
                               # compresses to ~4 kbps and buffering proxies stall the
                               # idle stream, so a remote listener's player never starts.
        '-cutoff', '4000',     # narrowband — the 8 kHz source has no content above 4 kHz
                               # (Nyquist), so confine Opus to 0–4 kHz and spend every bit
                               # on the speech band instead of an empty 4–8 kHz range
        '-frame_duration', '20',
        '-application', 'audio',
        '-f', 'webm',
        '-cluster_time_limit', '200',
        'pipe:1',
    ]
    log('DEBUG', f'[AUDIO] launching ffmpeg: {" ".join(ffmpeg_cmd)}')
    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    log('DEBUG', f'[AUDIO] ffmpeg PID {ffmpeg_proc.pid} for node {node}')

    broadcast = _AudioBroadcast(node, channel, ffmpeg_proc, relay_proc)
    broadcast._fifo_in_path  = fifo_in_path    # stored for cleanup in shutdown()
    broadcast._fifo_out_path = fifo_out_path

    # Ensure app_mixmonitor.so is loaded (not in the default ASL3 module list)
    def _ensure_mm_module(ami):
        lines = ami.command('module show like app_mixmonitor')
        if not any('app_mixmonitor' in l for l in lines):
            if ami.module_load('app_mixmonitor.so'):
                log('INFO', '[AUDIO] Loaded app_mixmonitor.so on demand')
        return {'ok': True}
    try:
        ami_send_command(_ensure_mm_module)
    except Exception as e:
        log('WARN', f'[AUDIO] module load check failed: {e}')

    # Apply MixMonitor to the existing node channel
    def _start_mm(ami):
        ami._send_action({
            'Action':  'MixMonitor',
            'Channel': channel,
            'File':    fifo_in_path,
            'Options': '',
        })
        raw = ami._recv_until('\r\n\r\n', timeout=ami.timeout)
        pkt = ami._parse_packet(raw)
        if pkt.get('Response') == 'Error':
            raise RuntimeError(pkt.get('Message', 'MixMonitor failed'))
        log('DEBUG', f'[AUDIO] MixMonitor AMI response: {pkt}')
        return pkt

    try:
        ami_send_command(_start_mm)
    except Exception as e:
        broadcast.shutdown()
        raise RuntimeError(f'MixMonitor failed: {e}')

    log('INFO', f'[AUDIO] MixMonitor started on {channel} for node {node}, '
                f'broadcast setup took {time.monotonic() - _t0:.3f}s total')
    return broadcast


@app.route('/api/audio/stream/<node>')
@limiter.limit("30 per minute")
def api_audio_stream(node):
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({'error': 'invalid node'}), 400

    remote = request.remote_addr or '?'
    ua     = request.headers.get('User-Agent', '?')
    log('DEBUG', f'[AUDIO] stream request for node {node} from {remote} '
                f'(UA: {ua})')
    try:
        with _audio_lock:
            broadcast = _audio_active.get(node)
            if broadcast is None or broadcast._dead:
                log('INFO', f'[AUDIO] starting new broadcast for node {node} '
                            f'(first request from {remote})')
                broadcast = _start_broadcast(node)
                _audio_active[node] = broadcast
            else:
                log('DEBUG', f'[AUDIO] reusing existing broadcast for node {node} '
                            f'for {remote}')
            client_q = broadcast.add_client(remote)
    except Exception as e:
        log('ERROR', f'[AUDIO] stream setup failed for {node} ({remote}): {e}\n'
                    f'{traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500

    def generate():
        yielded = 0
        try:
            while True:
                chunk = client_q.get()
                if chunk is None:
                    log('DEBUG', f'[AUDIO] {remote} generator unblocked by shutdown '
                                f'sentinel for node {node} after {yielded} chunk(s)')
                    break
                yielded += 1
                yield chunk
        except GeneratorExit:
            log('DEBUG', f'[AUDIO] {remote} client generator closed (browser '
                        f'disconnected) for node {node} after {yielded} chunk(s)')
            raise
        finally:
            broadcast.remove_client(client_q, remote)

    return Response(
        stream_with_context(generate()),
        mimetype='audio/webm; codecs=opus',
        headers={
            'Cache-Control':     'no-cache, no-store',
            'X-Accel-Buffering': 'no',
            'Transfer-Encoding': 'chunked',
        },
    )


@app.route('/api/audio/stop', methods=['POST'])
@limiter.limit("30 per minute")
def api_audio_stop():
    node = str((request.json or {}).get('node', '')).strip()
    if not re.match(r'^\d{4,7}$', node):
        return jsonify({'error': 'invalid node'}), 400
    remote = request.remote_addr or '?'
    with _audio_lock:
        broadcast = _audio_active.get(node)
    if broadcast and not broadcast.has_client(remote):
        log('WARN', f'[AUDIO] stop for node {node} rejected: {remote} is not '
                    f'a current listener of this broadcast')
        return jsonify({'error': 'not a listener of this broadcast'}), 403
    log('DEBUG', f'[AUDIO] explicit stop requested for node {node} '
                f'(broadcast active={broadcast is not None})')
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
        loaded = any('app_mixmonitor' in l for l in lines)
        if not loaded:
            # Same on-demand load _start_broadcast() does right before
            # MixMonitor — app_mixmonitor.so isn't in ASL3's default module
            # list, so on a fresh Asterisk start (or after a module unload)
            # it's simply not loaded yet. Load it here too instead of just
            # reporting failure, otherwise this diagnostic permanently blocks
            # the Listen button until someone loads it by hand, even though
            # actually starting a broadcast would have loaded it anyway.
            if ami.module_load('app_mixmonitor.so'):
                log('INFO', '[AUDIO] Loaded app_mixmonitor.so on demand (check)')
                loaded = True
        return {'loaded': loaded}
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


@app.route('/api/audio/client-log', methods=['POST'])
@limiter.limit("60 per minute")
def api_audio_client_log():
    """Receives MSE playback glitch reports (lag jumps, stalls, rebuffers,
    errors) from the kiosk's audio player and logs them under [AUDIO-CLIENT]
    so they interleave with the [AUDIO]/[AUDIO-RELAY] server-side lines in
    journalctl — lets a listener-reported glitch be correlated to the exact
    moment in the pipeline without needing browser devtools open live."""
    body   = request.json or {}
    node   = str(body.get('node', ''))[:16]
    etype  = str(body.get('type', ''))[:40]
    detail = str(body.get('detail', ''))[:200]
    if not re.match(r'^\d{4,7}$', node) or not etype:
        return jsonify({'error': 'invalid payload'}), 400
    remote = request.remote_addr or '?'
    log('INFO', f'[AUDIO-CLIENT] {remote} node={node} {etype}: {detail}')
    return jsonify({'ok': True})


@app.route("/")
def status_board():
    return render_template("status.html", henwen_version=HENWEN_VERSION)


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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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


# Asterisk messages.log line prefix, e.g. "[2026-07-01 23:01:49] VERBOSE[123] ..."
_AST_LOG_LINE_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\]\s+([A-Z]+)')
# Map Asterisk log levels onto the small set the Diagnostics panel colours.
_AST_LEVEL_MAP = {"ERROR": "ERROR", "WARNING": "WARN", "NOTICE": "INFO",
                  "VERBOSE": "AMI", "DEBUG": "DEBUG", "DTMF": "AMI"}


@app.route("/api/diagnostics")
def api_diagnostics():
    """
    Merged diagnostics feed for the Dashboard's Diagnostics panel: HenWen's own
    recent log lines (from the in-memory ring) plus, for superusers, a tail of
    the Asterisk log — combined and sorted chronologically. Admins see HenWen
    lines only (Asterisk console access is superuser-only, matching
    /api/asterisk/log).
    """
    role = session.get("role", "")
    if role not in ("admin", "superuser", "owner"):
        return jsonify({"error": "Admin access required"}), 403
    try:
        limit = min(int(request.args.get("lines", 400)), 1500)
    except (ValueError, TypeError):
        limit = 400

    entries = []

    # ── HenWen's own log (in-memory ring) ────────────────────────────────────
    with _log_lock:
        ring = list(_LOG_RING)
    for e in ring:
        entries.append({
            "src":   "HENWEN",
            "t":     e["t"],
            "ts":    datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S"),
            "level": e["level"],
            "msg":   e["msg"],
        })

    # ── Asterisk log tail (superuser only) ───────────────────────────────────
    ast_available = False
    ast_reason    = ""
    if role == "superuser":
        if os.path.exists(ASTERISK_LOG_PATH):
            try:
                with open(ASTERISK_LOG_PATH, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    read_size = min(size, 256 * 1024)
                    f.seek(size - read_size)
                    raw = f.read(read_size).decode("utf-8", errors="replace")
                lines = raw.splitlines()
                if size > read_size and len(lines) > 1:
                    lines = lines[1:]   # drop the partial first line
                last_t = None
                for line in lines[-limit:]:
                    m = _AST_LOG_LINE_RE.match(line)
                    if m:
                        try:
                            last_t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                        except ValueError:
                            pass
                        level = _AST_LEVEL_MAP.get(m.group(2).upper(), "INFO")
                    else:
                        level = ""   # continuation / unparseable line
                    t = last_t if last_t is not None else 0.0
                    entries.append({
                        "src":   "ASTERISK",
                        "t":     t,
                        "ts":    datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "",
                        "level": level,
                        "msg":   line,
                    })
                ast_available = True
            except PermissionError:
                ast_reason = f"permission denied reading {ASTERISK_LOG_PATH}"
            except Exception as ex:
                ast_reason = str(ex)
        else:
            ast_reason = f"Asterisk log not found at {ASTERISK_LOG_PATH}"
    else:
        ast_reason = "Asterisk diagnostics require a superuser account"

    # Merge chronologically. Python's sort is stable, so lines sharing a second
    # keep their within-source order.
    entries.sort(key=lambda e: e["t"])
    entries = entries[-limit:]

    resp = jsonify({
        "entries":            entries,
        "henwen_log_level":   LOG_LEVEL,
        "asterisk_available": ast_available,
        "asterisk_reason":    ast_reason,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/asterisk/verbose", methods=["POST"])
def api_asterisk_verbose():
    """
    Set Asterisk console verbosity via AMI.
    Body: {"level": N}  where N is 0–9.
    Equivalent to running 'asterisk -rvvv' (level=3) from the command line.
    """
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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


# ── Update check ──────────────────────────────────────────────────────────

@app.route("/api/update-check")
def api_update_check():
    """?refresh=1 checks GitHub live instead of only reading the daily
    poller's cache — the Settings page's "Check for Updates" button passes
    it, since the whole point of a manual click is "ask right now", and the
    background cache can be up to 24h behind a just-published release.
    Manual clicks are rare and admin-gated, so there's no rate concern.
    On a failed live fetch, falls back to the cache and says so."""
    refreshed = None
    if request.args.get("refresh"):
        result = _fetch_latest_release()
        refreshed = result is not None
        if result:
            with _latest_release_lock:
                _latest_release_cache.update(result)
    latest = get_latest_release()
    latest_tag = latest["tag"]
    return jsonify({
        "current":          HENWEN_VERSION,
        "latest":           latest_tag,
        "url":              latest["url"] or f"https://github.com/{GITHUB_REPO}/releases/latest",
        "update_available": bool(latest_tag) and HENWEN_VERSION != "unknown"
                            and latest_tag > HENWEN_VERSION,
        # None = no live check requested; False = requested but GitHub was
        # unreachable (values shown are the last cached check)
        "refreshed":        refreshed,
    })


@app.route("/api/update/launch", methods=["POST"])
def api_update_launch():
    """Kick off update.sh as its own transient systemd unit (superuser only
    — it ends in a full service restart). Launched via `systemd-run` rather
    than a plain subprocess so the script survives outside HenWen.service's
    cgroup: `systemctl restart HenWen` at the end of the script would
    otherwise kill the very process running it."""
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403

    if not os.path.exists(UPDATE_SCRIPT_PATH):
        return jsonify({"error": f"Updater script not found: {UPDATE_SCRIPT_PATH}"}), 404

    cmd = [SUDO_PATH, "-n", SYSTEMD_RUN_PATH,
           "--unit=henwen-updater", "--collect", UPDATE_SCRIPT_PATH]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        log("ERROR", f"[API] /api/update/launch exception: {e}")
        return jsonify({"error": str(e)}), 500

    if r.returncode != 0:
        stderr = r.stderr.strip()
        hint = ("Service account lacks sudo rights for the updater — "
                "re-run install.sh to install the henwen-systemctl sudoers rule."
                if "password" in stderr.lower() or "authoriz" in stderr.lower()
                else "Check: journalctl -u henwen-updater -n 50")
        log("ERROR", f"[API] update launch failed: {stderr}")
        return jsonify({"error": stderr or f"systemd-run returned code {r.returncode}",
                        "hint": hint}), 500

    log("INFO", f"[API] Update launched by '{session.get('username')}' (unit: henwen-updater)")
    return jsonify({
        "success": True,
        "message": "Update started. If one is available, HenWen will restart "
                   "automatically in a moment — this page will disconnect, then "
                   "reload it. Follow progress with: journalctl -u henwen-updater -f",
    })


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


SECRET_KEY_CHARSET_RE = re.compile(r'^[A-Za-z0-9_-]{16,128}$')


def _atomic_replace(path: str, content: str, prefix: str = ".henwen_tmp_"):
    """Write content to path via write-to-temp-then-rename in the same
    directory, so a reader never observes a partial file and a crash
    mid-write never corrupts the original. Raises PermissionError if the
    directory isn't writable by the caller — shared by every SERVICE_FILE_PATH/
    MANAGER_CONF rewrite in this file (SECRET_KEY, ports)."""
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=prefix)
    with os.fdopen(fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, path)


def _write_secret_key_direct(new_key: str):
    """Rewrite SERVICE_FILE_PATH's Environment="SECRET_KEY=..." line in
    place. Raises PermissionError if the caller (normally the unprivileged
    `asterisk` service account) can't write the root-owned unit file — the
    route falls back to _write_secret_key_via_sudo_helper() when that
    happens."""
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

    _atomic_replace(SERVICE_FILE_PATH, content, prefix=".henwen_svc_")


def _write_secret_key_via_sudo_helper(new_key: str):
    """Fallback for when _write_secret_key_direct() hits PermissionError.
    rotate_secret_key.sh is the one piece of code install.sh's sudoers rule
    grants NOPASSWD root rights to — it does this exact edit and nothing
    else (no reload/restart; that stays here, via the pre-existing
    _systemctl() sudo rule). The key is piped over stdin, never argv, so it
    never appears in `ps` output. Raises RuntimeError with the script's
    stderr on failure (missing sudoers rule, rejected charset, etc.).

    sudo's env_reset strips SERVICE_FILE_PATH/SERVICE_NAME before the script
    ever sees them, so it always targets its own hardcoded default path —
    correct only when SERVICE_FILE_PATH here still equals that same default.
    Callers must not invoke this after a custom SERVICE_FILE_PATH override,
    or the script would silently "succeed" against the wrong file while the
    operator's actual unit file is never touched."""
    r = subprocess.run(
        [SUDO_PATH, "-n", ROTATE_SECRET_KEY_SCRIPT_PATH],
        input=new_key + "\n", capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"exit code {r.returncode}").strip())


def _write_service_ports_via_sudo_helper(new_flask_port, new_ami_port):
    """Ports-route counterpart to _write_secret_key_via_sudo_helper() —
    same rationale, same constraints (stdin only, default-path-only). Unlike
    the SECRET_KEY helper this one re-derives the substitutions itself
    rather than accepting precomputed file content: trusting content
    computed by the unprivileged caller would let it write anything
    (including a new ExecStart=/User=) into a root-owned unit file this same
    sudoers rule can then daemon-reload+restart into effect. Does not touch
    manager.conf — that file is asterisk:asterisk and the caller already
    writes it directly."""
    r = subprocess.run(
        [SUDO_PATH, "-n", UPDATE_SERVICE_PORTS_SCRIPT_PATH],
        input=f"{new_flask_port or ''}\n{new_ami_port or ''}\n",
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"exit code {r.returncode}").strip())


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

    if not new_key:
        new_key = secrets.token_hex(32)
        log("INFO", "[SETTINGS] Generated a new random SECRET_KEY")
    elif not SECRET_KEY_CHARSET_RE.match(new_key):
        return jsonify({"error": "Secret key must be 16-128 characters, "
                                  "letters/numbers/underscore/hyphen only"}), 400

    if not os.path.exists(SERVICE_FILE_PATH):
        return jsonify({"error": f"Service file not found: {SERVICE_FILE_PATH}",
                        "hint": "Set SERVICE_FILE_PATH if the unit file lives elsewhere."}), 404

    try:
        try:
            _write_secret_key_direct(new_key)
        except PermissionError as direct_err:
            if SERVICE_FILE_PATH != _DEFAULT_SERVICE_FILE_PATH:
                # rotate_secret_key.sh always targets the default path (sudo's
                # env_reset strips any override before the script sees it) —
                # falling back here against a customized SERVICE_FILE_PATH
                # would silently edit the wrong file. Skip straight to the
                # manual-instructions error below instead.
                raise
            try:
                _write_secret_key_via_sudo_helper(new_key)
            except Exception as sudo_err:
                raise PermissionError(
                    f"{direct_err}; sudo fallback also failed: {sudo_err}") from direct_err
        log("INFO", f"[SETTINGS] SECRET_KEY updated in {SERVICE_FILE_PATH}")

        dr = _systemctl("daemon-reload", timeout=15)
        if dr.returncode != 0:
            stderr = dr.stderr.strip()
            log("ERROR", f"[SETTINGS] daemon-reload failed after SECRET_KEY write: {stderr}")
            hint = ("Service account lacks sudo rights for systemctl — re-run "
                    "install.sh to install the henwen-systemctl sudoers rule."
                    if "password" in stderr.lower() or "authoriz" in stderr.lower()
                    else "Check: journalctl -u HenWen -n 30")
            return jsonify({
                "error": f"SECRET_KEY was written to {SERVICE_FILE_PATH}, but "
                        f"'systemctl daemon-reload' failed: {stderr or dr.returncode}. "
                        "The new key will not take effect until the service is "
                        "manually restarted.",
                "hint": hint,
            }), 500

        def _delayed_restart():
            time.sleep(1.0)
            log("INFO", f"[SETTINGS] Restarting {SERVICE_NAME} to apply new SECRET_KEY")
            r = _systemctl("restart", SERVICE_NAME, timeout=30)
            if r.returncode != 0:
                log("ERROR", f"[SETTINGS] Restart of {SERVICE_NAME} failed after "
                            f"SECRET_KEY rotation: {r.stderr.strip()}")

        threading.Thread(target=_delayed_restart, daemon=True).start()

        return jsonify({
            "success": True,
            "message": f"SECRET_KEY updated. Restarting {SERVICE_NAME} now — "
                       "the page will briefly disconnect, then reload it.",
        })
    except PermissionError as e:
        log("ERROR", f"[SETTINGS] Permission denied writing {SERVICE_FILE_PATH}: {e}")
        return jsonify({"error": str(e),
                        "hint": f"The service account ({os.environ.get('USER', 'asterisk')}) "
                                "cannot write the root-owned unit file directly, and the "
                                "sudo fallback (rotate_secret_key.sh) also failed — re-run "
                                "install.sh to install its sudoers rule, or edit SECRET_KEY in "
                                f"{SERVICE_FILE_PATH} as root, then "
                                "`systemctl daemon-reload && systemctl restart "
                                f"{SERVICE_NAME}`."}), 403
    except Exception as e:
        log("ERROR", f"[SETTINGS] secret_key update failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── App settings (ports) ──────────────────────────────────────────────────
#
# Two ports are safe to expose here because HenWen owns both ends of each:
# the Flask/gunicorn bind port (HenWen.service) and the AMI port (also
# HenWen.service, but must agree with manager.conf's own `port =`, which
# Asterisk itself binds). The HTTPS port deliberately isn't here — that's
# Apache's `ports.conf`/vhost/certbot territory, provisioned once by
# tx-spike/setup-https.sh's `--port` flag; rewriting that live from a web
# request is a different, much larger blast radius than these two.
PORT_MIN = 1024
PORT_MAX = 65535


@app.route("/api/settings/ports")
def api_get_ports():
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
    return jsonify({
        "flask_port":          PORT,
        "ami_port":            AMI_PORT,
        "service_file":        SERVICE_FILE_PATH,
        "service_file_exists": os.path.exists(SERVICE_FILE_PATH),
        "manager_conf":        MANAGER_CONF,
        "manager_conf_exists": os.path.exists(MANAGER_CONF),
    })


@app.route("/api/settings/ports", methods=["POST"])
def api_set_ports():
    """
    Change the Flask/gunicorn bind port and/or the AMI port, writing every
    place each one actually needs to agree:
      - Flask port: HenWen.service's `Environment=PORT=` line (read by the
        dev-mode `app.run()` path) AND the `--bind 0.0.0.0:<port>` gunicorn
        actually listens on in production — today those two can silently
        disagree, since only --bind matters under gunicorn and nothing
        kept them in sync. This route always writes both together.
      - AMI port: HenWen.service's `Environment=AMI_PORT=` line AND
        manager.conf's own `port =` for the stanza Asterisk itself binds —
        these must match or HenWen can't reach AMI at all after restart.
    Restarts HenWen (and reloads Asterisk's manager module first, for an
    AMI port change, so the new bindport is live before HenWen reconnects
    to it) so the new value(s) actually take effect. The response is sent
    before either happens, same reasoning as the SECRET_KEY route above:
    a Flask-port change means the browser's current URL stops working the
    moment gunicorn rebinds, so the caller needs the new port back to know
    where to navigate next.
    """
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403

    data            = request.json or {}
    raw_flask_port  = data.get("flask_port")
    raw_ami_port    = data.get("ami_port")

    if raw_flask_port is None and raw_ami_port is None:
        return jsonify({"error": "Provide flask_port and/or ami_port"}), 400

    def _valid_port(p):
        try:
            p = int(p)
        except (TypeError, ValueError):
            return None
        return p if PORT_MIN <= p <= PORT_MAX else None

    new_flask_port = None
    new_ami_port   = None
    if raw_flask_port is not None:
        new_flask_port = _valid_port(raw_flask_port)
        if new_flask_port is None:
            return jsonify({"error": f"flask_port must be a number between {PORT_MIN} and {PORT_MAX}"}), 400
    if raw_ami_port is not None:
        new_ami_port = _valid_port(raw_ami_port)
        if new_ami_port is None:
            return jsonify({"error": f"ami_port must be a number between {PORT_MIN} and {PORT_MAX}"}), 400
    if new_flask_port is not None and new_ami_port is not None and new_flask_port == new_ami_port:
        return jsonify({"error": "flask_port and ami_port can't be the same"}), 400
    for p, label in ((new_flask_port, "flask_port"), (new_ami_port, "ami_port")):
        if p == 8088:
            return jsonify({"error": f"{label} can't be 8088 — that's Asterisk's builtin HTTP/WS "
                                      "server, hardcoded into the Browser TX Apache proxy"}), 400

    if not os.path.exists(SERVICE_FILE_PATH):
        return jsonify({"error": f"Service file not found: {SERVICE_FILE_PATH}"}), 404
    if new_ami_port is not None and not os.path.exists(MANAGER_CONF):
        return jsonify({"error": f"manager.conf not found: {MANAGER_CONF}"}), 404

    try:
        with open(SERVICE_FILE_PATH) as f:
            content = f.read()

        if new_flask_port is not None:
            content, n1 = re.subn(r'^Environment="?PORT=\d+"?[ \t]*$',
                                   f'Environment=PORT={new_flask_port}', content,
                                   count=1, flags=re.MULTILINE)
            content, n2 = re.subn(r'(--bind\s+[^:\s]*:)\d+', rf'\g<1>{new_flask_port}',
                                   content, count=1)
            if not n1 or not n2:
                return jsonify({"error": "Could not find both the PORT environment line and the "
                                          "gunicorn --bind flag in the service file to update — "
                                          "it may not match the expected format"}), 500

        if new_ami_port is not None:
            content, n = re.subn(r'^Environment="?AMI_PORT=\d+"?[ \t]*$',
                                  f'Environment=AMI_PORT={new_ami_port}', content,
                                  count=1, flags=re.MULTILINE)
            if not n:
                return jsonify({"error": "Could not find the AMI_PORT environment line in the "
                                          "service file to update"}), 500

        try:
            _atomic_replace(SERVICE_FILE_PATH, content, prefix=".henwen_svc_")
        except PermissionError as direct_err:
            if SERVICE_FILE_PATH != _DEFAULT_SERVICE_FILE_PATH:
                # update_service_ports.sh always targets the default path
                # (sudo's env_reset strips any override first) — falling
                # back here against a customized SERVICE_FILE_PATH would
                # silently edit the wrong file.
                raise
            try:
                _write_service_ports_via_sudo_helper(new_flask_port, new_ami_port)
            except Exception as sudo_err:
                raise PermissionError(
                    f"{direct_err}; sudo fallback also failed: {sudo_err}") from direct_err
        log("INFO", f"[SETTINGS] Ports updated in {SERVICE_FILE_PATH} "
                     f"(flask={new_flask_port}, ami={new_ami_port})")

        if new_ami_port is not None:
            with open(MANAGER_CONF) as f:
                mgr_content = f.read()
            mgr_content, n = re.subn(r'^(\s*port\s*=\s*)\d+', rf'\g<1>{new_ami_port}',
                                      mgr_content, count=1, flags=re.MULTILINE)
            if not n:
                return jsonify({"error": f"No 'port = ' line found in {MANAGER_CONF} to update — "
                                          "set it there manually to match, then retry"}), 500
            _atomic_replace(MANAGER_CONF, mgr_content, prefix=".henwen_mgr_")
            log("INFO", f"[SETTINGS] AMI port updated in {MANAGER_CONF} -> {new_ami_port}")

        dr = _systemctl("daemon-reload", timeout=15)
        if dr.returncode != 0:
            stderr = dr.stderr.strip()
            log("ERROR", f"[SETTINGS] daemon-reload failed after port change: {stderr}")
            hint = ("Service account lacks sudo rights for systemctl — re-run "
                    "install.sh to install the henwen-systemctl sudoers rule."
                    if "password" in stderr.lower() or "authoriz" in stderr.lower()
                    else "Check: journalctl -u HenWen -n 30")
            return jsonify({
                "error": f"Port(s) written, but 'systemctl daemon-reload' failed: "
                        f"{stderr or dr.returncode}. Changes will not take effect until "
                        "the service is manually restarted.",
                "hint": hint,
            }), 500

        def _delayed_restart():
            time.sleep(1.0)
            if new_ami_port is not None:
                # Reload Asterisk's manager module first so it's already
                # listening on the new bindport by the time HenWen restarts
                # and tries to reconnect to it.
                try:
                    ami_send_command(lambda ami: ami.command("module reload manager"))
                except Exception as e:
                    log("WARN", f"[SETTINGS] 'module reload manager' failed (Asterisk may still be "
                                 f"on the old AMI port until its next restart): {e}")
            log("INFO", f"[SETTINGS] Restarting {SERVICE_NAME} to apply new port(s)")
            r = _systemctl("restart", SERVICE_NAME, timeout=30)
            if r.returncode != 0:
                log("ERROR", f"[SETTINGS] Restart of {SERVICE_NAME} failed after "
                            f"port change: {r.stderr.strip()}")

        threading.Thread(target=_delayed_restart, daemon=True).start()

        msg = "Restarting HenWen now to apply the change."
        if new_flask_port is not None:
            msg += (f" The web UI is moving to port {new_flask_port} — this page will "
                    f"disconnect and NOT reload itself; navigate to the new port manually.")
        else:
            msg += " This page will briefly disconnect, then reload it."

        return jsonify({
            "success":    True,
            "flask_port": new_flask_port,
            "ami_port":   new_ami_port,
            "message":    msg,
        })
    except PermissionError as e:
        log("ERROR", f"[SETTINGS] Permission denied writing port settings: {e}")
        return jsonify({"error": str(e),
                        "hint": f"The service account ({os.environ.get('USER', 'asterisk')}) "
                                "cannot write the root-owned unit file directly, and the sudo "
                                "fallback (update_service_ports.sh) also failed — re-run "
                                "install.sh to install its sudoers rule, or edit PORT/AMI_PORT in "
                                f"{SERVICE_FILE_PATH} (and manager.conf's port= to match) as "
                                "root, then `systemctl daemon-reload && systemctl restart "
                                f"{SERVICE_NAME}`."}), 403
    except Exception as e:
        log("ERROR", f"[SETTINGS] port update failed: {e}")
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
    if session.get("role") not in ("admin", "superuser", "owner"):
        return jsonify({"error": "Admin access required"}), 403
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
            "Run: sudo bash /opt/HenWen/ami-setup.sh"
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
            "Run: sudo bash /opt/HenWen/ami-setup.sh\n"
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
            "fix":        "Set AMI_USER and AMI_SECRET in /etc/systemd/system/HenWen.service"
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

# Curated Piper voice list — deliberately short and fixed, not an open picker.
# Every voice is the 'medium' quality tier: 'low' trades away intelligibility
# that matters for narrowband FM/repeater audio, and 'high' quality is
# unlikely to survive ULAW + FM compression while costing more synthesis
# time and disk. Only en_US-lessac-medium has been listen-tested through an
# actual repeater audio chain as of this writing — treat the others as
# reputationally good but unverified on-air until someone checks.
# Voice IDs must never be accepted from a client and used to build a
# download URL directly — see _ensure_voice_model_cached()'s allowlist check.
TTS_VOICES = {
    "en_US-lessac-medium": {
        "label": "Lessac (US, neutral)",
        "lang": "en", "region": "en_US", "name": "lessac", "quality": "medium",
    },
    "en_US-amy-medium": {
        "label": "Amy (US, female)",
        "lang": "en", "region": "en_US", "name": "amy", "quality": "medium",
    },
    "en_US-ryan-medium": {
        "label": "Ryan (US, male)",
        "lang": "en", "region": "en_US", "name": "ryan", "quality": "medium",
    },
    "en_GB-alan-medium": {
        "label": "Alan (UK, male)",
        "lang": "en", "region": "en_GB", "name": "alan", "quality": "medium",
    },
}
DEFAULT_TTS_VOICE = "en_US-lessac-medium"   # must match the DB column default above
TTS_TEXT_MAX_CHARS = 800
PIPER_VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Fixed allowlist for the NWS auto-announcement feature's event_types filter.
# Validated server-side on save so a typo'd event name can't silently
# produce a filter that never matches anything (unlike watch_nodes, where a
# typo'd node number just harmlessly never matches — a wrong entry here is a
# safety-relevant silent failure, worth the extra guard).
NWS_ALERT_EVENT_TYPES = [
    "Tornado Warning",
    "Tornado Watch",
    "Severe Thunderstorm Warning",
    "Severe Thunderstorm Watch",
    "Flash Flood Warning",
    "Flash Flood Watch",
    "Extreme Wind Warning",
]
NWS_ALERTS_BASE_URL = "https://api.weather.gov"
NWS_USER_AGENT = "HenWen/1.0 (ham radio node manager; https://github.com/GooseThings/HenWen)"
NWS_ALERT_SPOKEN_PREFIX = "The National Weather Service has issued a "


def _piper_voice_urls(voice_id: str):
    """(onnx_url, json_url) for a curated voice id. Caller must have already
    validated voice_id is a TTS_VOICES key — this never accepts arbitrary
    input, since it's used to build an outbound download URL."""
    v = TTS_VOICES[voice_id]
    base = f"{PIPER_VOICES_BASE_URL}/{v['lang']}/{v['region']}/{v['name']}/{v['quality']}/{v['region']}-{v['name']}-{v['quality']}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _voice_model_paths(voice_id: str):
    onnx = os.path.join(TTS_VOICES_DIR, f"{voice_id}.onnx")
    json_ = os.path.join(TTS_VOICES_DIR, f"{voice_id}.onnx.json")
    return onnx, json_


def _voice_is_cached(voice_id: str) -> bool:
    onnx, json_ = _voice_model_paths(voice_id)
    return os.path.exists(onnx) and os.path.exists(json_)


def _ensure_voice_model_cached(voice_id: str):
    """Download a curated voice's model files if not already present.
    Raises ValueError for an unrecognized voice id (never reachable from a
    client-supplied string without going through this check first — the
    caller must validate against TTS_VOICES before calling). Downloads to
    .part temp files and os.replace()s into place only on full success, so a
    network failure never leaves a truncated file that looks cached."""
    if voice_id not in TTS_VOICES:
        raise ValueError(f"Unknown voice id: {voice_id!r}")
    if _voice_is_cached(voice_id):
        return
    os.makedirs(TTS_VOICES_DIR, exist_ok=True)
    onnx_url, json_url = _piper_voice_urls(voice_id)
    onnx_path, json_path = _voice_model_paths(voice_id)
    for url, dest in ((onnx_url, onnx_path), (json_url, json_path)):
        tmp = dest + ".part"
        req = urlreq.Request(url, headers={"User-Agent": "HenWen/1.0"})
        with urlreq.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, dest)
    log("INFO", f"[TTS] Downloaded voice model '{voice_id}'")


def _synthesize_tts(text: str, voice_id: str, dest_wav: str) -> None:
    """Run Piper to synthesize text to a WAV file. Caller must have already
    ensured the voice is cached (_ensure_voice_model_cached) — this does not
    trigger a download itself, keeping synthesis latency predictable."""
    if voice_id not in TTS_VOICES:
        raise ValueError(f"Unknown voice id: {voice_id!r}")
    onnx_path, _ = _voice_model_paths(voice_id)
    if not os.path.exists(onnx_path):
        raise RuntimeError(f"Voice '{voice_id}' is not downloaded yet")
    os.makedirs(os.path.dirname(dest_wav), exist_ok=True)
    result = subprocess.run(
        [PIPER_BIN, "-m", onnx_path, "-f", dest_wav],
        input=text.encode("utf-8"),
        capture_output=True, timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(f"piper failed: {result.stderr.decode(errors='replace')[-500:]}")


def _tts_to_ulaw(text: str, voice_id: str, dest_ulaw: str) -> None:
    """Synthesize text to speech and convert it straight to the 8kHz mono
    ULAW format Asterisk needs, via the existing _convert_to_ulaw() — same
    output format as an uploaded announcement file, so the scheduler and
    playback path need no source-type-specific handling at all."""
    fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _synthesize_tts(text, voice_id, tmp_wav)
        _convert_to_ulaw(tmp_wav, dest_ulaw)
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass


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
    # Asterisk reads sounds as the asterisk user; ensure it can read the file
    # regardless of the process umask.
    os.chmod(dest, 0o644)


def _ann_sound_path(slug: str) -> str:
    return os.path.join(SOUNDS_DIR, f"{slug}.ulaw")


def _ensure_sounds_dir():
    """os.makedirs(SOUNDS_DIR) can fail (e.g. the asterisk user lacking
    permission to create it under a root-owned parent) — call sites need this
    caught and turned into a JSON error response rather than propagating as
    an unhandled exception, which Flask renders as an HTML error page that
    breaks every caller expecting JSON. Returns None on success, or a
    (response, status_code) tuple on failure."""
    try:
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        return None
    except OSError as e:
        log("ERROR", f"[ANNOUNCE] Could not create SOUNDS_DIR ({SOUNDS_DIR}): {e}")
        return jsonify({"error": f"Server cannot access its sounds directory: {e}"}), 500


def _run_due_announcements():
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    db = get_db()
    rows = db.execute("SELECT * FROM announcements WHERE enabled=1").fetchall()

    for row in rows:
        name = row["name"]
        try:
            ws_h, ws_m = map(int, row["window_start"].split(":"))
            we_h, we_m = map(int, row["window_end"].split(":"))
        except Exception:
            log("WARN", f"[ANNOUNCE] '{name}': bad window format "
                        f"(start={row['window_start']!r} end={row['window_end']!r}) — skipping")
            continue

        start_min = ws_h * 60 + ws_m
        end_min   = we_h * 60 + we_m
        # window_end < window_start (e.g. 07:30 → 07:28) means the window
        # spans midnight — end is on the following day, not earlier the same
        # day. start_min <= now_min <= end_min can never be true in that case
        # (start_min > end_min for every now_min), so the window would never
        # fire at all; OR the two half-open ranges instead.
        if start_min <= end_min:
            in_window = start_min <= now_min <= end_min
        else:
            in_window = now_min >= start_min or now_min <= end_min
        if not in_window:
            log("DEBUG", f"[ANNOUNCE] '{name}': outside window "
                         f"{row['window_start']}–{row['window_end']} (now={now.strftime('%H:%M')})")
            continue

        if row["last_played"]:
            try:
                last = datetime.strptime(row["last_played"], "%Y-%m-%d %H:%M:%S")
                elapsed_min = (now - last).total_seconds() / 60.0
                if elapsed_min < row["interval_min"]:
                    log("DEBUG", f"[ANNOUNCE] '{name}': interval not elapsed "
                                 f"({elapsed_min:.1f}/{row['interval_min']} min)")
                    continue
            except Exception:
                pass

        node = row["node"]
        settle_sec = row["idle_settle_sec"] if row["idle_settle_sec"] is not None else 30
        with _node_last_active_lock:
            last_active_ts = _node_last_active.get(node)
        if last_active_ts is not None:
            quiet_sec = now.timestamp() - last_active_ts
            if quiet_sec < settle_sec:
                # max_defer_sec (NWS-sourced rows only; NULL for every other
                # announcement, which preserves today's indefinite defer) lets
                # a severe weather alert force playback after being due for
                # too long, even over a still-busy channel, rather than
                # deferring indefinitely like a routine announcement would.
                forced = False
                max_defer = row["max_defer_sec"]
                if max_defer is not None:
                    try:
                        if row["last_played"]:
                            due_since = (datetime.strptime(row["last_played"], "%Y-%m-%d %H:%M:%S")
                                         + timedelta(minutes=row["interval_min"]))
                        else:
                            # created_at defaults to SQLite's datetime('now'),
                            # which is UTC — `now` here is local time. Compared
                            # raw, waited_sec started hours NEGATIVE in any US
                            # timezone, so the FIRST play of a new alert (the
                            # one that matters most) could never be forced past
                            # a busy channel. last_played above is written in
                            # local time, so that branch was always consistent.
                            due_since = (datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
                                         .replace(tzinfo=timezone.utc)
                                         .astimezone().replace(tzinfo=None))
                        waited_sec = (now - due_since).total_seconds()
                        forced = waited_sec >= max_defer
                    except Exception:
                        forced = False
                if not forced:
                    log("INFO", f"[ANNOUNCE] Node {node} not settled — deferring '{row['name']}' "
                                f"(quiet {quiet_sec:.0f}s / {settle_sec}s needed)")
                    continue
                log("WARN", f"[ANNOUNCE] Node {node} not settled but '{row['name']}' forced "
                            f"after exceeding max_defer_sec={max_defer}")

        # Re-check the row still exists and is enabled right before firing.
        # `rows` was snapshotted once at the top of this function; if an
        # admin deletes (or disables) this announcement after that snapshot
        # but before its turn in this loop — a real, observed race, since a
        # busy cycle can spend real time on AMI I/O for earlier rows — the
        # stale in-memory copy would otherwise still get played even though
        # it no longer exists.
        if not db.execute("SELECT 1 FROM announcements WHERE id=? AND enabled=1", (row["id"],)).fetchone():
            log("INFO", f"[ANNOUNCE] '{name}' was deleted/disabled since this cycle started — skipping")
            continue

        sound_arg = f"{SOUNDS_SUBDIR}/{row['slug']}"
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
# TTS voice management API routes
# ---------------------------------------------------------------------------

@app.route("/api/tts/voices")
def api_tts_voices_list():
    out = []
    for vid, v in TTS_VOICES.items():
        out.append({
            "id":     vid,
            "label":  v["label"],
            "cached": _voice_is_cached(vid),
        })
    return jsonify(out)


@app.route("/api/tts/voices/<voice_id>/download", methods=["POST"])
def api_tts_voice_download(voice_id):
    """Explicit, separate action from announcement create/edit — a first-time
    model download is network-dependent and can itself take a while, and
    stacking it inside the same request as text synthesis + ffmpeg conversion
    risks the combined latency against gunicorn's worker timeout."""
    if voice_id not in TTS_VOICES:
        return jsonify({"error": f"Unknown voice id: {voice_id}"}), 400
    try:
        _ensure_voice_model_cached(voice_id)
        return jsonify({"ok": True, "id": voice_id, "cached": True})
    except Exception as e:
        log("ERROR", f"[TTS] Voice download failed for '{voice_id}': {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Announcement API routes
# ---------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


@app.route("/api/announcements")
def api_ann_list():
    # NWS-sourced rows are auto-managed and shown on their own Weather Alerts
    # page (GET /api/nws-alerts/status) — excluded here so they don't also
    # show up mixed in with manually-created announcements. Per-row actions
    # (PATCH/DELETE/toggle/play) still work on them unchanged; only this
    # list view filters them out.
    db = get_db()
    rows = db.execute(
        "SELECT * FROM announcements WHERE source_type != 'nws_alert' ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


def _validate_ann_common(data):
    """Shared scheduling-field validation for both the upload (multipart
    form) and TTS (JSON) announcement-creation paths. `data` must support
    .get(key, default) — works for both request.form and a plain dict from
    request.json. Returns (validated_dict, None) on success, or
    (None, (response, status_code)) on the first validation failure."""
    name = str(data.get("name", "")).strip()
    node = str(data.get("node", "")).strip()
    if not name:
        return None, (jsonify({"error": "Name is required"}), 400)
    if not re.match(r'^\d{4,7}$', node):
        return None, (jsonify({"error": "Invalid node number"}), 400)

    try:
        interval_min = int(data.get("interval_min", 60))
        if interval_min < 1:
            raise ValueError
    except (ValueError, TypeError):
        return None, (jsonify({"error": "interval_min must be a positive integer"}), 400)

    try:
        idle_settle_sec = int(data.get("idle_settle_sec", 30))
        if idle_settle_sec < 0:
            raise ValueError
    except (ValueError, TypeError):
        return None, (jsonify({"error": "idle_settle_sec must be a non-negative integer"}), 400)

    window_start = str(data.get("window_start", "07:30")).strip()
    window_end   = str(data.get("window_end",   "19:30")).strip()
    play_cmd     = str(data.get("play_cmd",     "localplay")).strip()
    if play_cmd not in ("localplay", "playback"):
        play_cmd = "localplay"
    if not re.match(r'^\d{2}:\d{2}$', window_start) or not re.match(r'^\d{2}:\d{2}$', window_end):
        return None, (jsonify({"error": "window_start and window_end must be HH:MM"}), 400)

    return {
        "name": name, "node": node, "interval_min": interval_min,
        "idle_settle_sec": idle_settle_sec, "window_start": window_start,
        "window_end": window_end, "play_cmd": play_cmd,
    }, None


@app.route("/api/announcements", methods=["POST"])
def api_ann_create():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    fields, err = _validate_ann_common(request.form)
    if err:
        return err
    name, node = fields["name"], fields["node"]
    interval_min, idle_settle_sec = fields["interval_min"], fields["idle_settle_sec"]
    window_start, window_end, play_cmd = fields["window_start"], fields["window_end"], fields["play_cmd"]

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    err = _ensure_sounds_dir()
    if err:
        return err

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
           (name, slug, node, enabled, interval_min, window_start, window_end, play_cmd,
            idle_settle_sec)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (name, slug, node, interval_min, window_start, window_end, play_cmd, idle_settle_sec),
    )
    db.commit()
    row = db.execute("SELECT * FROM announcements WHERE slug=?", (slug,)).fetchone()
    log("INFO", f"[ANNOUNCE] Created announcement '{name}' (slug={slug}, node={node})")
    return jsonify(dict(row)), 201


@app.route("/api/announcements/tts", methods=["POST"])
def api_ann_create_tts():
    """Create a TTS-sourced announcement. Separate route from the upload
    path (JSON body, not multipart) rather than branching api_ann_create —
    that route's first line is a request.files presence check, and this
    keeps the file-upload control flow completely untouched.

    The chosen voice must already be downloaded (see /api/tts/voices and
    POST /api/tts/voices/<id>/download) — synthesis does not trigger a
    download itself, so create-time latency stays predictable and bounded
    well under gunicorn's worker timeout.
    """
    data = request.json or {}
    fields, err = _validate_ann_common(data)
    if err:
        return err
    name, node = fields["name"], fields["node"]
    interval_min, idle_settle_sec = fields["interval_min"], fields["idle_settle_sec"]
    window_start, window_end, play_cmd = fields["window_start"], fields["window_end"], fields["play_cmd"]

    text  = str(data.get("text", "")).strip()
    voice = str(data.get("voice", DEFAULT_TTS_VOICE)).strip()

    if not text:
        return jsonify({"error": "Message text is required"}), 400
    if len(text) > TTS_TEXT_MAX_CHARS:
        return jsonify({"error": f"Message text must be {TTS_TEXT_MAX_CHARS} characters or fewer"}), 400
    if voice not in TTS_VOICES:
        return jsonify({"error": f"Unknown voice: {voice}"}), 400
    if not _voice_is_cached(voice):
        return jsonify({"error": f"Voice '{voice}' is not downloaded yet — "
                                 "download it first from the voice picker."}), 400

    err = _ensure_sounds_dir()
    if err:
        return err

    base_slug = _ann_slug(name)
    slug      = _ann_unique_slug(base_slug)
    dest      = _ann_sound_path(slug)

    try:
        _tts_to_ulaw(text, voice, dest)
    except Exception as e:
        log("ERROR", f"[ANNOUNCE] TTS synthesis failed for '{name}': {e}")
        return jsonify({"error": f"Speech synthesis failed: {e}"}), 500

    try:
        shutil.chown(dest, user="asterisk", group="asterisk")
        os.chmod(dest, 0o640)
    except Exception:
        pass

    db = get_db()
    db.execute(
        """INSERT INTO announcements
           (name, slug, node, enabled, interval_min, window_start, window_end, play_cmd,
            idle_settle_sec, source_type, tts_text, tts_voice)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 'tts', ?, ?)""",
        (name, slug, node, interval_min, window_start, window_end, play_cmd, idle_settle_sec,
         text, voice),
    )
    db.commit()
    row = db.execute("SELECT * FROM announcements WHERE slug=?", (slug,)).fetchone()
    log("INFO", f"[ANNOUNCE] Created TTS announcement '{name}' (slug={slug}, node={node}, voice={voice})")
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
    idle_settle_sec = int(data.get("idle_settle_sec", row["idle_settle_sec"]))

    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400
    if interval_min < 1:
        return jsonify({"error": "interval_min must be >= 1"}), 400
    if idle_settle_sec < 0:
        return jsonify({"error": "idle_settle_sec must be >= 0"}), 400
    if not re.match(r'^\d{2}:\d{2}$', window_start) or not re.match(r'^\d{2}:\d{2}$', window_end):
        return jsonify({"error": "window times must be HH:MM"}), 400
    if play_cmd not in ("localplay", "playback"):
        play_cmd = "localplay"

    tts_text  = row["tts_text"]
    tts_voice = row["tts_voice"]
    if row["source_type"] in ("tts", "nws_alert"):
        new_text  = str(data.get("text",  row["tts_text"]  or "")).strip()
        new_voice = str(data.get("voice", row["tts_voice"])).strip()
        if not new_text:
            return jsonify({"error": "Message text is required"}), 400
        if len(new_text) > TTS_TEXT_MAX_CHARS:
            return jsonify({"error": f"Message text must be {TTS_TEXT_MAX_CHARS} characters or fewer"}), 400
        if new_voice not in TTS_VOICES:
            return jsonify({"error": f"Unknown voice: {new_voice}"}), 400
        if new_text != (row["tts_text"] or "") or new_voice != row["tts_voice"]:
            if not _voice_is_cached(new_voice):
                return jsonify({"error": f"Voice '{new_voice}' is not downloaded yet — "
                                         "download it first from the voice picker."}), 400
            # The slug — and therefore the sound file path — never changes on
            # edit, so this is old-content/new-content at the SAME path, not
            # old-file/new-file. Synthesize to a temp file and atomically
            # os.replace() over the live path: the scheduler or a manual Test
            # Play firing mid-edit gets either the fully-old or fully-new
            # file, never a missing one, and a failed re-synthesis leaves the
            # previous working audio untouched.
            dest = _ann_sound_path(row["slug"])
            fd, tmp_ulaw = tempfile.mkstemp(suffix=".ulaw", dir=SOUNDS_DIR)
            os.close(fd)
            try:
                _tts_to_ulaw(new_text, new_voice, tmp_ulaw)
                try:
                    shutil.chown(tmp_ulaw, user="asterisk", group="asterisk")
                    os.chmod(tmp_ulaw, 0o640)
                except Exception:
                    pass
                os.replace(tmp_ulaw, dest)
            except Exception as e:
                try:
                    os.unlink(tmp_ulaw)
                except OSError:
                    pass
                log("ERROR", f"[ANNOUNCE] TTS re-synthesis failed for '{row['name']}': {e}")
                return jsonify({"error": f"Speech synthesis failed: {e}"}), 500
            tts_text, tts_voice = new_text, new_voice

    db.execute(
        """UPDATE announcements
           SET name=?, node=?, interval_min=?, window_start=?, window_end=?, play_cmd=?,
               idle_settle_sec=?, tts_text=?, tts_voice=?
           WHERE id=?""",
        (name, node, interval_min, window_start, window_end, play_cmd, idle_settle_sec,
         tts_text, tts_voice, ann_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not updated:
        # Narrow race: for an NWS-sourced row, the poller could have decided
        # (in a concurrent cycle) that this alert is no longer active and
        # deleted it while this edit was in flight — most likely during a
        # slow TTS re-synthesis above. Report clearly rather than crashing.
        return jsonify({"error": "This announcement was deleted while being edited"}), 404
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

    sound_arg = f"{SOUNDS_SUBDIR}/{row['slug']}"
    cmd       = f"rpt {row['play_cmd']} {row['node']} {sound_arg}"
    log("INFO", f"[ANNOUNCE] Test play '{row['name']}': {cmd!r}")

    try:
        def _play(ami, _cmd=cmd):
            out = ami.command(_cmd)
            return {"output": out}
        result = ami_send_command(_play)
        # Deliberately do NOT update last_played here — test plays are manual
        # one-offs and should not reset the scheduler's interval timer.
        return jsonify({"ok": True, "output": result.get("output", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tts/preview", methods=["POST"])
def api_tts_preview():
    """Synthesize and immediately play text WITHOUT saving it as an
    announcement — lets an admin hear a message before scheduling it. Always
    uses a fixed scratch slug (henwen/_tts_preview), overwritten each call,
    so previews never accumulate files needing cleanup."""
    data  = request.json or {}
    node  = str(data.get("node", "")).strip()
    text  = str(data.get("text", "")).strip()
    voice = str(data.get("voice", DEFAULT_TTS_VOICE)).strip()

    if not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "Invalid node number"}), 400
    if not text:
        return jsonify({"error": "Message text is required"}), 400
    if len(text) > TTS_TEXT_MAX_CHARS:
        return jsonify({"error": f"Message text must be {TTS_TEXT_MAX_CHARS} characters or fewer"}), 400
    if voice not in TTS_VOICES:
        return jsonify({"error": f"Unknown voice: {voice}"}), 400
    if not _voice_is_cached(voice):
        return jsonify({"error": f"Voice '{voice}' is not downloaded yet — "
                                 "download it first from the voice picker."}), 400

    err = _ensure_sounds_dir()
    if err:
        return err
    slug = "_tts_preview"
    dest = _ann_sound_path(slug)
    try:
        _tts_to_ulaw(text, voice, dest)
    except Exception as e:
        log("ERROR", f"[TTS] Preview synthesis failed: {e}")
        return jsonify({"error": f"Speech synthesis failed: {e}"}), 500
    try:
        shutil.chown(dest, user="asterisk", group="asterisk")
        os.chmod(dest, 0o640)
    except Exception:
        pass

    cmd = f"rpt localplay {node} {SOUNDS_SUBDIR}/{slug}"
    log("INFO", f"[TTS] Preview play on {node}: {cmd!r}")
    try:
        def _play(ami, _cmd=cmd):
            return {"output": ami.command(_cmd)}
        result = ami_send_command(_play)
        return jsonify({"ok": True, "output": result.get("output", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# NWS severe weather auto-announcements — poll api.weather.gov, auto-create
# TTS announcements for active Tornado/Severe Weather alerts, let the
# existing announcement scheduler handle all actual timed playback.
# ---------------------------------------------------------------------------

def _get_nws_alert_config():
    """Return nws_alert_config row as dict, or None if not configured."""
    try:
        db  = get_db()
        row = db.execute("SELECT * FROM nws_alert_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception as e:
        log("ERROR", f"[NWS] _get_nws_alert_config error: {e}")
        return None


def _nws_zone_from_latlon(lat: float, lon: float):
    """Resolve a lat/lon to an NWS county/forecast-zone UGC code + a
    human-readable area name, via a one-time api.weather.gov/points call.
    Returns {"zone": "COZ085", "area_name": "Colorado Springs, CO"} or None."""
    try:
        url = f"{NWS_ALERTS_BASE_URL}/points/{lat},{lon}"
        req = urlreq.Request(url, headers={"User-Agent": NWS_USER_AGENT})
        with urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        props = data.get("properties", {})
        # Prefer the county UGC — most Warning-tier severe weather products
        # (Tornado Warning, Severe Thunderstorm Warning) are issued against
        # county zones, not public forecast zones.
        county_url = props.get("county", "") or ""
        zone = county_url.rstrip("/").split("/")[-1] if county_url else ""
        if not zone:
            forecast_url = props.get("forecastZone", "") or ""
            zone = forecast_url.rstrip("/").split("/")[-1] if forecast_url else ""
        if not zone:
            return None
        rel = (props.get("relativeLocation") or {}).get("properties", {})
        area_name = ", ".join(x for x in (rel.get("city"), rel.get("state")) if x)
        return {"zone": zone, "area_name": area_name}
    except Exception as e:
        log("WARN", f"[NWS] _nws_zone_from_latlon({lat},{lon}): {e}")
        return None


def _nws_parse_vtec_key(alert_props: dict) -> str:
    """Parse the stable {office}.{phenomena}.{significance}.{ETN} identity
    out of a VTEC string, e.g. '/O.CON.KMKX.TO.W.0075.000000T0000Z-260703T1815Z/'
    -> 'KMKX.TO.W.0075'. The VTEC string also embeds a timestamp range,
    which is deliberately excluded from the key — it can change on a
    time-extension update, unlike the office/phenomena/significance/ETN
    identity, which persists for the warning's entire lifecycle across
    NEW/CON/EXT/UPG/CAN updates. The CAP `id` field is NOT stable across
    updates (verified against live NWS data) — using it as a dedup key
    would delete-and-recreate the row on every routine update, resetting
    last_played and breaking the interval-based replay cadence. Falls back
    to the raw CAP id for alert types that don't carry a VTEC parameter
    (some lower-tier statements) — those specific types re-announce on
    every update instead of holding a stable interval, an accepted
    degradation rather than a crash.
    """
    vtec_list = (alert_props.get("parameters") or {}).get("VTEC") or []
    if vtec_list:
        parts = vtec_list[0].strip("/").split(".")
        if len(parts) >= 6:
            _cls, _action, office, phenomena, significance, etn = parts[:6]
            return f"{office}.{phenomena}.{significance}.{etn}"
    return alert_props.get("id", "")


def _nws_fetch_active_alerts(zone: str, event_types=None):
    """Fetch currently-active NWS alerts for a UGC zone/county code, already
    filtered to real (status=='Actual', non-Cancel) alerts, optionally
    further filtered to a set of event names. Raises on network/HTTP
    failure — the poller must NOT prune any tracked alert just because this
    call failed (fail-safe, not fail-open, for a safety feature)."""
    url = f"{NWS_ALERTS_BASE_URL}/alerts/active?zone={urlparse.quote(zone)}"
    req = urlreq.Request(url, headers={"User-Agent": NWS_USER_AGENT,
                                        "Accept": "application/geo+json"})
    with urlreq.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        if p.get("status") != "Actual":
            continue
        if p.get("messageType") == "Cancel":
            continue
        if event_types is not None and p.get("event") not in event_types:
            continue
        out.append({
            "id":       p.get("id", ""),
            "vtec_key": _nws_parse_vtec_key(p),
            "event":    p.get("event", ""),
            "headline": p.get("headline", ""),
            "areaDesc": p.get("areaDesc", ""),
            "expires":  p.get("expires", ""),
            "severity": p.get("severity", ""),
        })
    return out


_STATE_ABBREV_RE = re.compile(r',\s*[A-Z]{2}\b')


def _strip_state_names(area_desc: str) -> str:
    """NWS areaDesc is 'County, ST' pairs joined by semicolons (e.g.
    'Kenosha, WI; Racine, WI'). The county list alone identifies the area
    precisely enough for on-air use — strip the trailing state code from
    each entry rather than repeating (or mispronouncing) it after every
    single county."""
    return _STATE_ABBREV_RE.sub('', area_desc)


def _join_with_and(items) -> str:
    """Natural spoken list: 'A', 'A and B', or 'A, B, and C' — rather than a
    flat semicolon/comma run, which reads like a list of unrelated fragments
    instead of one place description."""
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _format_clock_time(dt: datetime) -> str:
    """'10:00 PM' reads/sounds better as '10 PM' — on-the-hour minutes add
    nothing and Piper reads out the redundant ':00'. Anything else keeps
    its minutes, e.g. '1:15 PM'."""
    return dt.strftime("%-I %p") if dt.minute == 0 else dt.strftime("%-I:%M %p")


def _nws_alert_spoken_text(alert: dict) -> str:
    """Build the spoken announcement string — templated, not the verbatim
    NWS headline, which includes wordy issue-time phrasing meant for text
    display, not repeated speech."""
    expires_str = ""
    if alert.get("expires"):
        try:
            expires_str = _format_clock_time(datetime.fromisoformat(alert["expires"]))
        except Exception:
            expires_str = ""
    raw_area = alert.get("areaDesc", "") or ""
    event    = alert.get("event", "") or "Severe Weather Alert"
    counties = _join_with_and(_strip_state_names(raw_area).split(";"))
    area_phrase = f"the following counties: {counties}" if counties else "your area"
    if expires_str:
        return f"{NWS_ALERT_SPOKEN_PREFIX}{event}. In effect for {area_phrase}, until {expires_str}."
    return f"{NWS_ALERT_SPOKEN_PREFIX}{event}. In effect for {area_phrase}."


def _nws_sync_alert(alert: dict, cfg: dict):
    """Create or update the announcements row tracking one active NWS
    alert. Shared by both the poller and the test-alert route so there's
    exactly one code path for 'turn an alert dict into an announcement
    row' — the poller and manual test can't drift out of sync."""
    db     = get_db()
    spoken = _nws_alert_spoken_text(alert)
    row    = db.execute(
        "SELECT * FROM announcements WHERE source_type='nws_alert' AND external_id=?",
        (alert["vtec_key"],)
    ).fetchone()

    if row is None:
        name      = f"NWS: {alert['event']} ({alert['vtec_key']})"
        base_slug = _ann_slug(name)
        slug      = _ann_unique_slug(base_slug)
        dest      = _ann_sound_path(slug)
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        _ensure_voice_model_cached(cfg["voice"])
        _tts_to_ulaw(spoken, cfg["voice"], dest)
        try:
            shutil.chown(dest, user="asterisk", group="asterisk")
            os.chmod(dest, 0o640)
        except Exception:
            pass
        db.execute(
            """INSERT INTO announcements
               (name, slug, node, enabled, interval_min, window_start, window_end,
                play_cmd, idle_settle_sec, source_type, tts_text, tts_voice,
                external_id, nws_expires, max_defer_sec)
               VALUES (?, ?, ?, 1, ?, '00:00', '23:59', ?, ?, 'nws_alert', ?, ?, ?, ?, ?)""",
            (name, slug, cfg["node"], cfg["interval_min"], cfg["play_cmd"],
             cfg["idle_settle_sec"], spoken, cfg["voice"], alert["vtec_key"],
             alert.get("expires", ""), cfg["max_defer_sec"]),
        )
        db.commit()
        log("INFO", f"[NWS] New alert tracked: {alert['event']} ({alert['vtec_key']})")
    elif row["nws_expires"] != alert.get("expires", ""):
        # Only re-synthesize when NWS's own data changed (an Update extended
        # or altered the expiry) — deliberately NOT triggered by tts_text
        # merely differing from a freshly-templated string, since an admin
        # can now manually edit an NWS alert's text/voice. Diffing on
        # nws_expires means a manual edit persists across poll cycles until
        # NWS itself provides a genuine update, at which point regenerating
        # fresh content is the right call (an edit about an old expiry time
        # would otherwise go stale silently). Re-synthesize atomically in
        # place, exactly like a manual TTS edit — the slug/path never
        # changes, so this is old-content/new-content at the same path,
        # never a missing-file gap for a scheduler tick to hit.
        dest = _ann_sound_path(row["slug"])
        fd, tmp_ulaw = tempfile.mkstemp(suffix=".ulaw", dir=SOUNDS_DIR)
        os.close(fd)
        try:
            _ensure_voice_model_cached(cfg["voice"])
            _tts_to_ulaw(spoken, cfg["voice"], tmp_ulaw)
            try:
                shutil.chown(tmp_ulaw, user="asterisk", group="asterisk")
                os.chmod(tmp_ulaw, 0o640)
            except Exception:
                pass
            os.replace(tmp_ulaw, dest)
        except Exception as e:
            try:
                os.unlink(tmp_ulaw)
            except OSError:
                pass
            log("ERROR", f"[NWS] Re-synthesis failed for {alert['vtec_key']}: {e}")
            return
        db.execute(
            "UPDATE announcements SET tts_text=?, nws_expires=? WHERE id=?",
            (spoken, alert.get("expires", ""), row["id"]),
        )
        db.commit()
        log("INFO", f"[NWS] Updated tracked alert: {alert['event']} ({alert['vtec_key']})")


NWS_ALERT_CONFIG_DEFAULTS = {
    "enabled": 0, "zone": "", "zone_area_name": "", "zone_auto_detected": 0,
    "node": "", "play_cmd": "localplay", "voice": DEFAULT_TTS_VOICE,
    "interval_min": 15, "idle_settle_sec": 10, "max_defer_sec": 120,
    "event_types": "Tornado Warning,Severe Thunderstorm Warning",
    "last_poll_at": None, "last_poll_error": None,
}


@app.route("/api/nws-alerts/config")
def api_nws_alerts_get_config():
    cfg = _get_nws_alert_config()
    return jsonify(dict(cfg) if cfg else NWS_ALERT_CONFIG_DEFAULTS)


@app.route("/api/nws-alerts/config", methods=["POST"])
def api_nws_alerts_save_config():
    data = request.json or {}

    event_types = [e.strip() for e in str(data.get("event_types", "")).split(",") if e.strip()]
    bad = [e for e in event_types if e not in NWS_ALERT_EVENT_TYPES]
    if bad:
        return jsonify({"error": f"Unknown event type(s): {', '.join(bad)}"}), 400

    voice = str(data.get("voice", DEFAULT_TTS_VOICE))
    if voice not in TTS_VOICES:
        return jsonify({"error": f"Unknown voice: {voice}"}), 400

    node = str(data.get("node", "")).strip()
    if data.get("enabled") and not re.match(r'^\d{4,7}$', node):
        return jsonify({"error": "A valid node number is required to enable NWS alerts"}), 400

    play_cmd = str(data.get("play_cmd", "localplay")).strip()
    if play_cmd not in ("localplay", "playback"):
        play_cmd = "localplay"

    # Download the voice model now, with the admin watching, rather than
    # lazily when the first real alert fires — deferring it would mean the
    # first real tornado warning is also the first moment a multi-minute
    # download gets triggered.
    if data.get("enabled"):
        try:
            _ensure_voice_model_cached(voice)
        except Exception as e:
            return jsonify({"error": f"Could not download voice '{voice}': {e}"}), 500

    db = get_db()
    db.execute(
        """INSERT OR REPLACE INTO nws_alert_config
           (id, enabled, zone, zone_area_name, zone_auto_detected, node, play_cmd, voice,
            interval_min, idle_settle_sec, max_defer_sec, event_types, last_poll_at, last_poll_error)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   (SELECT last_poll_at FROM nws_alert_config WHERE id=1),
                   (SELECT last_poll_error FROM nws_alert_config WHERE id=1))""",
        (
            1 if data.get("enabled") else 0,
            str(data.get("zone", "")).strip(),
            str(data.get("zone_area_name", "")).strip(),
            1 if data.get("zone_auto_detected") else 0,
            node,
            play_cmd,
            voice,
            int(data.get("interval_min", 15)),
            int(data.get("idle_settle_sec", 10)),
            int(data.get("max_defer_sec", 120)),
            ",".join(event_types),
        )
    )
    db.commit()
    log("INFO", "[NWS] Alert config saved")
    return jsonify({"ok": True})


@app.route("/api/nws-alerts/detect-zone", methods=["POST"])
def api_nws_alerts_detect_zone():
    """Suggest an NWS zone from the node's configured location — does not
    save anything, the admin confirms/overrides before saving config."""
    content  = read_conf_file(RPT_CONF_PATH)
    nodes    = get_node_numbers(content) if content else []
    location = lookup_node(nodes[0]).get("location", "") if nodes else ""
    if not location:
        return jsonify({"error": "No node location found in rpt.conf to geocode"}), 400
    coords = _geocode(location)
    if not coords:
        return jsonify({"error": f"Could not geocode location '{location}'"}), 400
    result = _nws_zone_from_latlon(coords["lat"], coords["lon"])
    if not result:
        return jsonify({"error": "Could not resolve an NWS zone for that location"}), 400
    return jsonify({"zone": result["zone"], "area_name": result["area_name"], "location": location})


@app.route("/api/nws-alerts/status")
def api_nws_alerts_status():
    cfg = _get_nws_alert_config()
    db  = get_db()
    tracked = db.execute(
        """SELECT id, name, node, tts_text, tts_voice, play_cmd, interval_min, last_played,
                  external_id, nws_expires, enabled
           FROM announcements WHERE source_type='nws_alert' ORDER BY nws_expires"""
    ).fetchall()
    alerts = []
    for r in tracked:
        d = dict(r)
        d["expires_display"] = ""
        if d["nws_expires"]:
            try:
                exp = datetime.fromisoformat(d["nws_expires"])
                d["expires_display"] = exp.strftime("%a ") + _format_clock_time(exp)
            except Exception:
                d["expires_display"] = d["nws_expires"]
        alerts.append(d)
    return jsonify({
        "last_poll_at":    cfg.get("last_poll_at") if cfg else None,
        "last_poll_error": cfg.get("last_poll_error") if cfg else None,
        "active_alerts":   alerts,
    })


@app.route("/api/nws-alerts/test", methods=["POST"])
def api_nws_alerts_test():
    cfg = _get_nws_alert_config()
    if not cfg or not cfg["node"]:
        return jsonify({"error": "Configure and save a node before testing"}), 400
    test_alert = {
        "vtec_key": "TEST.WX.W.0000",
        "event":    "Test Severe Weather Alert",
        "headline": "This is a test of the severe weather alert system.",
        "areaDesc": cfg.get("zone_area_name") or "your area",
        "expires":  (datetime.now().astimezone()).isoformat(),
        "severity": "Test",
    }
    try:
        _nws_sync_alert(test_alert, cfg)
        return jsonify({"ok": True, "message": "Test alert created — it will play on the next scheduler tick"})
    except Exception as e:
        log("ERROR", f"[NWS] Test alert failed: {e}")
        return jsonify({"error": str(e)}), 500


_NWS_POLL_INTERVAL   = 120.0   # base interval between fetches, single zone — well within NWS's rate expectations
_NWS_MAX_SLEEP       = 1200.0  # cap backoff at 20 minutes between cycles
_NWS_EXPIRE_GRACE_SEC = 30 * 60  # local safety ceiling: prune a tracked alert this long past its own expiry
                                 # even if NWS can't be reached to confirm — see _nws_alert_poll_loop().
_nws_backoff_level = 0


def _nws_prune_expired_locally(db, now):
    """Delete any tracked NWS alert whose OWN reported expiry is more than
    _NWS_EXPIRE_GRACE_SEC in the past. Runs every cycle regardless of fetch
    success — this is a local, network-independent ceiling so a stale
    warning can't replay forever during a sustained NWS outage. The grace
    period (rather than deleting right at `expires`) avoids racing a
    legitimate time-extension Update that hasn't been seen yet."""
    now_aware = now.astimezone() if now.tzinfo is None else now
    rows = db.execute(
        "SELECT id, slug, name, nws_expires FROM announcements "
        "WHERE source_type='nws_alert' AND nws_expires IS NOT NULL AND nws_expires != ''"
    ).fetchall()
    for row in rows:
        try:
            exp = datetime.fromisoformat(row["nws_expires"])
            if exp.tzinfo is None:
                exp = exp.astimezone()
            age_sec = (now_aware - exp).total_seconds()
        except Exception:
            continue
        if age_sec > _NWS_EXPIRE_GRACE_SEC:
            sound = _ann_sound_path(row["slug"])
            try:
                if os.path.exists(sound):
                    os.unlink(sound)
            except Exception as e:
                log("WARN", f"[NWS] Could not delete sound file {sound}: {e}")
            db.execute("DELETE FROM announcements WHERE id=?", (row["id"],))
            db.commit()
            log("INFO", f"[NWS] Pruned locally-expired alert '{row['name']}' "
                        f"({age_sec/60:.0f} min past expiry)")


def _nws_alert_poll_loop():
    global _nws_backoff_level
    log("INFO", f"[NWS-POLL] Background poller started (interval={_NWS_POLL_INTERVAL}s)")
    while True:
        success = False
        try:
            cfg = _get_nws_alert_config()
            db  = get_db()
            now = datetime.now()

            # Regardless of fetch success — local safety ceiling.
            _nws_prune_expired_locally(db, now)

            if not cfg or not cfg["enabled"] or not cfg["zone"]:
                success = True  # not an error, just nothing to do — don't back off
            else:
                event_types = [e.strip() for e in cfg["event_types"].split(",") if e.strip()]
                alerts = _nws_fetch_active_alerts(cfg["zone"], event_types=event_types or None)
                success = True

                current_keys = {a["vtec_key"] for a in alerts}
                tracked = db.execute(
                    "SELECT id, slug, name, external_id FROM announcements WHERE source_type='nws_alert'"
                ).fetchall()
                for row in tracked:
                    if row["external_id"] not in current_keys:
                        sound = _ann_sound_path(row["slug"])
                        try:
                            if os.path.exists(sound):
                                os.unlink(sound)
                        except Exception as e:
                            log("WARN", f"[NWS] Could not delete sound file {sound}: {e}")
                        db.execute("DELETE FROM announcements WHERE id=?", (row["id"],))
                        db.commit()
                        log("INFO", f"[NWS] No longer active, pruned: '{row['name']}'")

                for alert in alerts:
                    try:
                        _nws_sync_alert(alert, cfg)
                    except Exception as e:
                        log("ERROR", f"[NWS] _nws_sync_alert failed for {alert.get('vtec_key')}: {e}")

            db.execute(
                "UPDATE nws_alert_config SET last_poll_at=?, last_poll_error=? WHERE id=1",
                (now.strftime("%Y-%m-%d %H:%M:%S"), None)
            )
            db.commit()
        except Exception as e:
            log("WARN", f"[NWS-POLL] Cycle error: {e}")
            try:
                db = get_db()
                db.execute(
                    "UPDATE nws_alert_config SET last_poll_error=? WHERE id=1",
                    (str(e),)
                )
                db.commit()
            except Exception:
                pass

        _nws_backoff_level = 0 if success else min(_nws_backoff_level + 1, 6)
        sleep_time = min(_NWS_POLL_INTERVAL * (2 ** _nws_backoff_level), _NWS_MAX_SLEEP)
        if _nws_backoff_level:
            log("WARN", f"[NWS-POLL] backing off — next poll in {sleep_time:.0f}s (level {_nws_backoff_level})")
        time.sleep(sleep_time)


def start_nws_alert_poller():
    t = threading.Thread(target=_nws_alert_poll_loop, name="nws-alert-poller", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Smart Connector — scheduled node connect/disconnect with idle monitoring
# ---------------------------------------------------------------------------

def _node_active(node: str) -> bool:
    """Return True if the node is keyed (RX or any linked TX) per AMI cache."""
    cached = _ami_cache.get(node, {})
    if cached.get("keyed", False):
        return True
    return any(l.get("keyed", False) for l in cached.get("links", {}).values())


def _connector_do_connect(local: str, target: str, disconnect_first: bool = False,
                          skip_permanent: bool = False):
    """Connect target to local. If disconnect_first, drop existing links on
    local before connecting: either all of them (ilink 6, same pattern as the
    manual "Connect / Disconnect" panel's disconnect_first option in
    /api/ami/connect), or — if skip_permanent — only the ones not marked
    permanent, disconnected individually with ilink 11 (not ilink 1 — see
    api_status_disconnect's docstring; ilink 1 silently no-ops on a link
    app_rpt considers permanent) so permanent links are left alone.

    "Permanent" here means this app has the link recorded as such in
    _kiosk_temp_conns (set only when a user explicitly checks Permanent at
    connect time). app_rpt doesn't expose a queryable "is this link
    permanent" flag via AMI, so a link established outside the app — raw
    AMI/CLI, or a startup connection baked into rpt.conf — isn't tracked and
    will be treated as non-permanent and disconnected like any other. Using
    ilink 11 (rather than 1) for that disconnect matters precisely because
    such a link might actually be permanent in app_rpt despite being
    untracked here — ilink 1 would silently fail to drop it.
    """
    def _cmd(ami, ln=local, tn=target, disc=disconnect_first, skip=skip_permanent):
        if disc:
            if skip:
                cached    = _ami_cache.get(ln) or {}
                connected = list(cached.get("connected", []))
                with _kiosk_temp_lock:
                    perm_peers = {pn for (l, pn), info in _kiosk_temp_conns.items()
                                 if l == ln and info.get('permanent')}
                to_drop = [pn for pn in connected if pn not in perm_peers]
                log("INFO", f"[CONNECTOR] Disconnecting {len(to_drop)} non-permanent "
                            f"node(s) on {ln} before scheduled connect to {tn} "
                            f"(preserving {len(connected) - len(to_drop)} permanent)")
                for peer in to_drop:
                    ami.rpt_cmd(ln, f"ilink 11 {peer}")
            else:
                log("INFO", f"[CONNECTOR] Disconnecting all nodes on {ln} before scheduled connect to {tn}")
                ami.rpt_cmd(ln, "ilink 6")
            time.sleep(0.3)
        return ami.rpt_cmd(ln, f"ilink 3 {tn}")
    return ami_send_command(_cmd)


def _connector_do_disconnect(local: str, target: str):
    """Used for both the idle-timeout auto-disconnect and the manual
    Disconnect button. Uses ilink 11, not ilink 1 — see
    api_status_disconnect's docstring for why: ilink 1 silently no-ops on a
    permanent link. A connector-managed link is normally transient (created
    via ilink 3), but if the same node pair was independently made permanent
    elsewhere, ilink 1 would fail here too."""
    def _cmd(ami, ln=local, tn=target):
        return ami.rpt_cmd(ln, f"ilink 11 {tn}")
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


def _connector_should_fire(row, now):
    """Return True if this connector's schedule says it should trigger right now."""
    if not row["connect_time"]:
        return False
    if now.strftime("%H:%M") != row["connect_time"]:
        return False
    stype = row["schedule_type"] or "daily"
    sdays = row["schedule_days"] or ""

    if stype == "manual":
        return False
    if stype == "daily":
        return True
    if stype == "weekly":
        allowed = []
        for d in sdays.split(","):
            d = d.strip()
            if d.isdigit():
                allowed.append(int(d))
        return now.weekday() in allowed
    if stype == "biweekly":
        parts = sdays.split(":")
        try:
            wday   = int(parts[0])
            parity = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return False
        return now.weekday() == wday and (now.isocalendar()[1] % 2) == parity
    if stype == "monthly":
        try:
            return now.day == int(sdays)
        except ValueError:
            return False
    if stype == "bimonthly":
        parts = sdays.split(":")
        try:
            day         = int(parts[0])
            start_month = int(parts[1]) if len(parts) > 1 else 1
        except (ValueError, IndexError):
            return False
        return now.day == day and ((now.month - start_month) % 2 == 0)
    if stype == "quarterly":
        parts = sdays.split(":")
        try:
            day         = int(parts[0])
            start_month = int(parts[1]) if len(parts) > 1 else 1
        except (ValueError, IndexError):
            return False
        return now.day == day and ((now.month - start_month) % 3 == 0)
    if stype == "yearly":
        try:
            mm, dd = sdays.split("-")
            return now.month == int(mm) and now.day == int(dd)
        except (ValueError, AttributeError):
            return False
    if stype == "onetime":
        try:
            target_date = datetime.strptime(sdays, "%Y-%m-%d").date()
            return now.date() == target_date
        except (ValueError, AttributeError):
            return False
    return False


def _connector_upcoming_disconnect_all():
    """Warn the Status Board when a disconnect-all-first connector is about to
    fire: either its schedule lands within the next 60s, or it's already past
    its scheduled time and sitting in 'waiting' for the local node to go idle
    (up to 2 minutes — see _run_connectors). Only connectors with
    disconnect_all_first matter here, since a plain scheduled connect doesn't
    disturb anyone already linked."""
    try:
        db   = get_db()
        rows = db.execute(
            "SELECT * FROM connectors WHERE enabled=1 AND disconnect_all_first=1"
        ).fetchall()
    except Exception:
        return None
    now = datetime.now()
    for row in rows:
        what = "non-permanent nodes" if row["disconnect_skip_permanent"] else "all nodes"
        if row["state"] == "waiting":
            return f"Scheduled connection to {row['target_node']} will disconnect {what} shortly"
        if _connector_should_fire(row, now + timedelta(seconds=60)):
            return (f"Scheduled connection to {row['target_node']} in the next minute "
                    f"will disconnect {what}")
    return None


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
        if state == "idle" and _connector_should_fire(row, now):
            db.execute(
                "UPDATE connectors SET state='waiting', state_msg='Waiting for node to be idle', "
                "state_updated=? WHERE id=?", (now_str, cid)
            )
            # One-time connectors auto-disable after triggering
            if (row["schedule_type"] or "daily") == "onetime":
                db.execute("UPDATE connectors SET enabled=0 WHERE id=?", (cid,))
                log("INFO", f"[CONNECTOR] '{row['name']}' one-time trigger — auto-disabled")
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
                    result = _connector_do_connect(local, target,
                                                   disconnect_first=bool(row["disconnect_all_first"]),
                                                   skip_permanent=bool(row["disconnect_skip_permanent"]))
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
                        # ilink 11 on an already-gone node returns no-error output
                        # so treat both success and "node not connected" as done
                        raw = result.get("raw", "")
                        hard_fail = any(w in raw for w in ["permission denied", "not permitted", "unknown command"])
                        if hard_fail:
                            raise RuntimeError(f"ilink 11 rejected: {raw[:120]}")
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


_VALID_SCHEDULE_TYPES = {'manual', 'daily', 'weekly', 'biweekly', 'monthly',
                         'bimonthly', 'quarterly', 'yearly', 'onetime'}

@app.route("/api/connectors", methods=["POST"])
def api_conn_create():
    data = request.json or {}
    name          = str(data.get("name",          "")).strip()
    local_node    = str(data.get("local_node",    "")).strip()
    target_node   = str(data.get("target_node",   "")).strip()
    connect_time  = data.get("connect_time") or None
    schedule_type = str(data.get("schedule_type", "daily")).strip()
    schedule_days = str(data.get("schedule_days", "")).strip()
    idle_limit_sec = int(data.get("idle_limit_sec", 180))
    settle_sec     = int(data.get("settle_sec",     300))
    disconnect_all_first = 1 if data.get("disconnect_all_first") else 0
    disconnect_skip_permanent = 1 if data.get("disconnect_skip_permanent") else 0

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local node number"}), 400
    if not re.match(r'^\d{4,7}$', target_node):
        return jsonify({"error": "Invalid target node number"}), 400
    if connect_time and not re.match(r'^\d{2}:\d{2}$', connect_time):
        return jsonify({"error": "connect_time must be HH:MM"}), 400
    if schedule_type not in _VALID_SCHEDULE_TYPES:
        return jsonify({"error": f"Invalid schedule_type"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO connectors (name, local_node, target_node, connect_time, "
        "schedule_type, schedule_days, idle_limit_sec, settle_sec, disconnect_all_first, "
        "disconnect_skip_permanent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, local_node, target_node, connect_time,
         schedule_type, schedule_days, idle_limit_sec, settle_sec, disconnect_all_first,
         disconnect_skip_permanent)
    )
    db.commit()
    row = db.execute("SELECT * FROM connectors WHERE rowid=last_insert_rowid()").fetchone()
    log("INFO", f"[CONNECTOR] Created '{name}' ({local_node} → {target_node}) sched={schedule_type}")
    return jsonify(dict(row)), 201


@app.route("/api/connectors/<int:cid>", methods=["PATCH"])
def api_conn_update(cid):
    db  = get_db()
    row = db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    name          = str(data.get("name",          row["name"])).strip()
    local_node    = str(data.get("local_node",    row["local_node"])).strip()
    target_node   = str(data.get("target_node",   row["target_node"])).strip()
    # Unlike every other field here, this must fall back to the existing value
    # when omitted — data.get("connect_time") or None would silently clear the
    # schedule on any partial PATCH that doesn't resend it. `or None` still
    # normalizes an explicitly-submitted empty string to NULL.
    connect_time  = data.get("connect_time", row["connect_time"]) or None
    schedule_type = str(data.get("schedule_type", row["schedule_type"] or "daily")).strip()
    schedule_days = str(data.get("schedule_days", row["schedule_days"] or "")).strip()
    idle_limit_sec = int(data.get("idle_limit_sec", row["idle_limit_sec"]))
    settle_sec     = int(data.get("settle_sec",     row["settle_sec"]))
    disconnect_all_first = 1 if data.get("disconnect_all_first",
                                         bool(row["disconnect_all_first"])) else 0
    disconnect_skip_permanent = 1 if data.get("disconnect_skip_permanent",
                                              bool(row["disconnect_skip_permanent"])) else 0

    if not re.match(r'^\d{4,7}$', local_node):
        return jsonify({"error": "Invalid local node number"}), 400
    if not re.match(r'^\d{4,7}$', target_node):
        return jsonify({"error": "Invalid target node number"}), 400
    if connect_time and not re.match(r'^\d{2}:\d{2}$', connect_time):
        return jsonify({"error": "connect_time must be HH:MM"}), 400
    if schedule_type not in _VALID_SCHEDULE_TYPES:
        return jsonify({"error": f"Invalid schedule_type"}), 400

    db.execute(
        "UPDATE connectors SET name=?, local_node=?, target_node=?, connect_time=?, "
        "schedule_type=?, schedule_days=?, idle_limit_sec=?, settle_sec=?, "
        "disconnect_all_first=?, disconnect_skip_permanent=? WHERE id=?",
        (name, local_node, target_node, connect_time,
         schedule_type, schedule_days, idle_limit_sec, settle_sec,
         disconnect_all_first, disconnect_skip_permanent, cid)
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
        # exercises the exact same code path as the real disconnect command
        # (ilink 11, not 1 — see api_status_disconnect's docstring).
        try:
            lines = ami.command(f"rpt cmd {local_node} ilink 11 0")
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if not re.match(r'^[a-zA-Z0-9/_\-]{1,256}$', sound_path):
        return jsonify({"error": "sound_path contains invalid characters"}), 400

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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if not re.match(r'^[a-zA-Z0-9/_\-]{1,256}$', sound_path):
        return jsonify({"error": "sound_path contains invalid characters"}), 400

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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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
    if session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": "Superuser access required"}), 403
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

    err = _ensure_sounds_dir()
    if err:
        return err
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
    return jsonify({"path": f"{SOUNDS_SUBDIR}/{slug}", "slug": slug})


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
        lines = ami.command(f"rpt fun {node} {digits}")
        raw   = "\n".join(lines)
        return {"ok": True, "raw": raw}
    try:
        result = ami_send_command(_send)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Function numbers Admin (not just Superuser) may invoke via /api/dtmf/cop
# and /api/dtmf/status — verified individually against app_rpt's actual
# source (apps/app_rpt/rpt_functions.c) after cop 1 turned out to be
# `killall -9 asterisk`, not a status query. Everything NOT on these lists
# defaults to superuser-only — an allowlist, rather than trying to keep
# enumerating every dangerous number by hand (that's exactly how cop 1 got
# mislabeled "System Status" in the first place). Two functions that ARE in
# this UI's quick-action buttons are deliberately excluded here even though
# they're not destructive in the cop-1 sense:
#   cop 8  (Timeout Timer Disable) — removes the transmit time-limit safety/
#           compliance mechanism; leaving it off risks an indefinite keyup.
#   cop 12 (Link Disable) — disables ilink node-wide, breaking Connect/
#           Monitor/Smart Connector for the whole node until cop 11 restores it.
_DTMF_COP_ADMIN_SAFE    = {2, 3, 7, 9, 10, 11, 13, 21, 22, 24}
_DTMF_STATUS_ADMIN_SAFE = {1, 11}


@app.route("/api/dtmf/cop", methods=["POST"])
def api_dtmf_cop():
    """Raw app_rpt COP (control operator) passthrough — rpt cmd <node> cop <N>.

    cop 1 is `system("killall -9 asterisk")` in app_rpt itself (see
    apps/app_rpt/rpt_functions.c, function_cop() case 1) — an ungraceful hard
    kill of the entire Asterisk process, NOT a status query. A mislabeled
    quick-action button once sent this by accident and took the node down.
    It now requires an explicit confirm flag; use the Dashboard's own
    Restart Asterisk button (a clean systemctl restart) for an intentional
    restart instead of cop 1.
    """
    data     = request.get_json(force=True)
    node     = str(data.get("node", "")).strip()
    function = str(data.get("function", "")).strip()
    confirm  = bool(data.get("confirm", False))
    if not node.isdigit():
        return jsonify({"error": "node must be numeric"}), 400
    if not function.isdigit():
        return jsonify({"error": "function must be a positive integer"}), 400
    if int(function) not in _DTMF_COP_ADMIN_SAFE and session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": f"cop {function} requires a superuser account"}), 403
    if function == "1" and not confirm:
        return jsonify({"error": "cop 1 kills the entire Asterisk process (killall -9 asterisk) "
                                 "— it is not a status query. Pass confirm:true if this is really "
                                 "what you want; otherwise use Restart Asterisk on the Dashboard."}), 400
    def _send(ami):
        lines = ami.command(f"rpt cmd {node} cop {function}")
        raw   = "\n".join(lines)
        return {"ok": True, "raw": raw}
    try:
        result = ami_send_command(_send)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dtmf/status", methods=["POST"])
def api_dtmf_status():
    """Raw app_rpt STATUS passthrough — rpt cmd <node> status <N>. Separate
    command family from cop; this is where "Force ID" actually lives (status
    1 = System ID, status 11 = System ID local-only) — it is NOT a cop
    function, despite what an earlier version of this UI implied."""
    data     = request.get_json(force=True)
    node     = str(data.get("node", "")).strip()
    function = str(data.get("function", "")).strip()
    if not node.isdigit():
        return jsonify({"error": "node must be numeric"}), 400
    if not function.isdigit():
        return jsonify({"error": "function must be a positive integer"}), 400
    if int(function) not in _DTMF_STATUS_ADMIN_SAFE and session.get('role') not in ('superuser', 'owner'):
        return jsonify({"error": f"status {function} requires a superuser account"}), 403
    def _send(ami):
        lines = ami.command(f"rpt cmd {node} status {function}")
        raw   = "\n".join(lines)
        return {"ok": True, "raw": raw}
    try:
        result = ami_send_command(_send)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Startup — load node DB and start background AMI poller
# These run regardless of whether we're under gunicorn or direct python —
# except under the test suite (HENWEN_SKIP_STARTUP=1, set by tests/conftest.py)
# where importing this module must not spin up AMI/network polling threads
# or touch real /etc/asterisk paths.
# ---------------------------------------------------------------------------
if not os.environ.get("HENWEN_SKIP_STARTUP"):
    load_astdb()
    _db_conn_startup_cleanup()
    _rehydrate_permanent_links()
    _rehydrate_kiosk_temp_conns()
    start_poller()
    start_favstats_poller()
    start_global_activity_poller()
    start_echolink_directory_poller()
    start_geocode_worker()
    start_announcer()
    start_connector_scheduler()
    start_id_monitor()
    start_nws_alert_poller()
    start_release_poller()

if __name__ == "__main__":
    log("INFO", "Starting in direct-run mode (not via gunicorn)")
    app.run(host=HOST, port=PORT, debug=False)
