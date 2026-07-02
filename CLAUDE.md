# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HenWen is a browser-based AllStarLink 3 node manager and kiosk display. It runs as a systemd service (`ASL3-EZ`) on the same Debian machine as Asterisk, installed to `/opt/ASL3-EZ`.

- **Author callsign:** N8GMZ
- **Club callsign:** WE8CHZ (do not use for author attribution)

## Running and Deploying

There is no build step. The app runs directly via gunicorn.

**Deploy changes to the live service:**
```bash
sudo cp -r app.py audio_relay.py templates static /opt/ASL3-EZ/
sudo systemctl restart ASL3-EZ
```

**View live logs:**
```bash
journalctl -u ASL3-EZ -f
```

**Run directly (dev/debug, not via gunicorn):**
```bash
cd /opt/ASL3-EZ
./venv/bin/python3 app.py
```

**Install/reinstall:**
```bash
sudo bash install.sh   # copies files to /opt/ASL3-EZ, installs venv, enables service
```

There are no tests and no linter configuration.

## Architecture

### Single-file backend

`app.py` (~5800 lines) contains everything: Flask routes, AMI client, rpt.conf parser, SQLite schema, and all background threads. There is no module split.

### Background threads (started at module load, bottom of app.py)

Six daemon threads launch when gunicorn imports the module:
- `start_poller()` — AMI poll loop (1s interval): refreshes node keyed/connected state from Asterisk into an in-process cache
- `start_favstats_poller()` — polls AllStarLink stats API (30s interval) for favorite node status
- `start_global_activity_poller()` — fetches global ASL activity feed for the kiosk map
- `start_announcer()` — fires scheduled audio announcements via AMI `rpt localplay`
- `start_connector_scheduler()` — manages Smart Connector link/unlink on schedule
- `start_id_monitor()` — monitors node activity to trigger FCC ID audio playback

Because gunicorn runs `--workers 1 --threads 8`, all threads share a single process and in-process cache. Do not increase worker count without rethinking the AMI connection pool.

### AMI connection

`AMIClient` (class in app.py ~line 664) is a raw TCP socket client to Asterisk Manager Interface on port 5038. It is a persistent connection managed by `_poll_loop`. Routes read from the AMI cache (`get_cached_status()`) rather than issuing live AMI commands — this makes most status reads sub-millisecond. Commands that must be sent live (connect/disconnect, restart, etc.) use `ami_send_command()`.

### Database

SQLite at `/etc/asterisk/asl3ez.db`. Schema is defined inline in `get_db()` (called per request). Migrations happen via `ALTER TABLE` checks at startup — no migration framework. Tables: `users`, `favorites`, `settings`, `announcements`, `connectors`, `id_configs`, and a connection history log table.

### rpt.conf parsing

Custom parser (not `configparser`) — `_collect_stanzas()`, `parse_stanza_settings()`, `update_setting_in_content()`. It preserves comments and formatting on save and handles ASL3's multi-stanza structure (node stanzas, templates, macros, schedules).

### Audio streaming

Asterisk `MixMonitor` writes raw PCM to a FIFO (`/tmp/asl3ez_audio_<node>.sln`). `audio_relay.py` — a standalone process spawned by `_start_broadcast()` (~line 3902 in `app.py`), not a thread inside gunicorn — paces that PCM into strict 20ms frames (injecting silence when the node is quiet) and writes them to a second FIFO (`..._paced.sln`). ffmpeg reads that second FIFO directly and encodes WebM/Opus to its stdout. `_AudioBroadcast._read_loop` fans ffmpeg's stdout out to each client's `Queue`, and `/api/audio/stream/<node>` streams from that queue to the browser.

The pacing loop runs in its own OS process specifically to avoid GIL contention: gunicorn's request handlers, the AMI poller, and other background threads sharing this worker's GIL can stall a real-time 20ms deadline long enough to be audible as a click or stutter. Running the frame loop in a separate process lets the kernel schedule it independently. The MSE live-edge controller in `status.html` keeps the browser at ~0.5s behind live edge using `playbackRate` adjustment, with a startup watchdog and stall-recovery rebuffer logic.

### Templates

- `templates/status.html` — kiosk/status board (`/` and `/status` routes); self-contained SPA with embedded JS (~1800 lines). Contains the live audio player, network map, weather bar, and global activity feed. Accessible without login.
- `templates/henwen-manager.html` — all manager pages (settings, connectors, user management, announcements, node ID, etc.) loaded as a SPA shell via `/henwen-manager`.
- `templates/login.html` — login and first-run account creation.

### Configuration

All config comes from environment variables set in `/etc/systemd/system/ASL3-EZ.service`. The service file is the single source of truth for AMI credentials, `SECRET_KEY`, paths, and tuning parameters. After editing the service file: `sudo systemctl daemon-reload && sudo systemctl restart ASL3-EZ`.

Key env vars: `AMI_USER`, `AMI_SECRET`, `SECRET_KEY`, `DB_PATH`, `SOUNDS_DIR`, `LOG_LEVEL` (`INFO`/`DEBUG`), `RPT_CONF_PATH`.

### Auth and security

Flask-WTF CSRF on all mutating routes. Flask-Limiter on login. Three roles: Superuser (full access + raw rpt.conf editor), Admin (full access minus raw editor), User/Kiosk (connect/disconnect only). Role is stored in the SQLite `users` table and checked by `check_auth()` decorator.

Sessions are plain signed cookies — there is no server-side session store. To show a live "logged in users" count on the Status Board footer, `check_auth()` stamps each session with a random `sid` and touches an in-process dict (`_active_sessions`, guarded by `_active_sessions_lock`) on every authenticated request; `get_active_user_count()` prunes entries idle more than `ACTIVE_SESSION_WINDOW` (90s) and is called from `/api/status/board`. This state is per-worker-process — fine today since gunicorn runs `--workers 1`, but would need to move to the DB or a shared store if worker count is ever increased.

### External dependencies

- `https://stats.allstarlink.org/api/stats/{node}` — node keyed/connected counts
- `https://stats.allstarlink.org/stats/keyed` — scraped (regex, no HTML parser dependency) for the global activity feed on the kiosk map; every node currently keyed network-wide, polled every 2 min
- `https://allmondb.allstarlink.org/allmondb.php` — node callsign/location database
- `astdb.txt` — local copy of ASL node DB written by `asl3-update-nodelist` package

### Service identity

The systemd unit is named `ASL3-EZ` (not `HenWen`) — a legacy name from when the project was called ASL3-EZ. The install path `/opt/ASL3-EZ` and service unit name are unchanged on existing installs. Do not rename them.
