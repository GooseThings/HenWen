# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HenWen is a browser-based AllStarLink 3 node manager and kiosk display. It runs as a systemd service (`HenWen`) on the same Debian machine as Asterisk, installed to `/opt/HenWen`.

- **Author callsign:** N8GMZ
- **Club callsign:** WE8CHZ (do not use for author attribution)

## Running and Deploying

There is no build step. The app runs directly via gunicorn.

**Deploy changes to the live service:**
```bash
sudo cp -r app.py audio_relay.py templates static /opt/HenWen/
sudo systemctl restart HenWen
```

**View live logs:**
```bash
journalctl -u HenWen -f
```

**Run directly (dev/debug, not via gunicorn):**
```bash
cd /opt/HenWen
./venv/bin/python3 app.py
```

**Install/reinstall:**
```bash
sudo bash install.sh   # copies files to /opt/HenWen, installs venv, enables service
```

There is no linter configuration. Unit tests exist under `tests/` — see **Testing** below.

## Testing

`tests/` holds pytest-based unit/regression tests for the pure logic in `app.py` (rpt.conf parsing/validation, NWS alert helpers, connector scheduling, active-session tracking) plus a handful of Flask-test-client route smoke tests (login/setup flow, session, role gating). There's no coverage of the AMI client, audio pipeline, or background poll loops themselves — those need a live Asterisk/AMI to exercise meaningfully.

**Setup (one-time):**
```bash
python3 -m venv venv   # or reuse /opt/HenWen/venv if this checkout *is* the live install
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

**Run:**
```bash
./venv/bin/pytest
```

`tests/conftest.py` sets `HENWEN_SKIP_STARTUP=1` (and points `DB_PATH`/`RPT_CONF_PATH`/`SOUNDS_DIR`/etc. at a temp dir) *before* importing `app.py` — that env var is the only test-specific seam in `app.py` itself: it skips the unconditional module-load startup block (AMI poller, favstats/NWS/release pollers, `load_astdb()`, ...) described below, since none of that should run — or touch real `/etc/asterisk` paths — just because the module got imported by pytest. It's a no-op in every other context (gunicorn, `python3 app.py`), so production startup is unchanged. Each test that touches the DB gets its own fresh sqlite file via the `fresh_db`/`client` fixtures — see conftest.py for how (`DB_PATH` and the `_db_ready` schema-init flag both get monkeypatched per-test, since `get_db()` only runs its schema/migration statements once per process by design).

## Architecture

### Single-file backend

`app.py` (~7800 lines) contains everything: Flask routes, AMI client, rpt.conf parser, SQLite schema, and all background threads. There is no module split.

### Background threads (started at module load, bottom of app.py)

Daemon threads launch when gunicorn imports the module:
- `start_poller()` — AMI poll loop (1s interval): refreshes node keyed/connected state from Asterisk into an in-process cache
- `start_favstats_poller()` — polls AllStarLink stats API (30s interval) for favorite node status
- `start_global_activity_poller()` — fetches global ASL activity feed for the kiosk map
- `start_announcer()` — fires scheduled audio announcements via AMI `rpt localplay`/`playback` (30s interval, `_run_due_announcements()`); covers upload, TTS, and NWS-sourced announcements identically
- `start_connector_scheduler()` — manages Smart Connector link/unlink on schedule
- `start_id_monitor()` — monitors node activity to trigger FCC ID audio playback
- `start_nws_alert_poller()` — polls api.weather.gov for active severe weather alerts (~120s interval, exponential backoff) and manages the lifecycle of auto-created NWS announcement rows; never triggers playback itself, that's still `start_announcer()`'s job
- `start_release_poller()` — checks GitHub for the latest published HenWen release once a day into an in-process cache (`get_latest_release()`); `/api/update-check` reads that cache instead of hitting GitHub live, and the Manager dashboard surfaces it as a dismissible bar to superusers only (checked once per login/page-load, via `checkForUpdate()` in `henwen-manager.html`)
- `start_aprs_poller()` — optional; maintains one persistent APRS-IS connection (via the `aprslib` pip package) filtered to `APRS_MAX_RADIUS_MI` around the node's own geocoded location, caching station positions in-process (`_aprs_cache`); no-ops with a log line until an admin saves a login callsign in Manager > Kiosk Settings, or if `aprslib` isn't installed. See "APRS-IS map layer" below.

Because gunicorn runs `--workers 1 --threads 8`, all threads share a single process and in-process cache. Do not increase worker count without rethinking the AMI connection pool.

### Self-update

The update bar's "Launch Updater" button (`/api/update/launch`, superuser only) runs `update.sh` — but not as a direct child process. It's launched via `sudo systemd-run --unit=henwen-updater --collect update.sh` specifically so the script runs as its own transient systemd unit, outside HenWen.service's cgroup: the script ends by running `systemctl restart HenWen`, which would kill a plain subprocess child of the very process it's restarting. `update.sh` fetches `main` over plain HTTPS from the public repo (not the SSH `origin` remote — root may have no SSH key/agent under systemd-run), hard-resets to it, reinstalls `requirements.txt`, byte-compiles `app.py` as a sanity check before touching the running service (rolling back and aborting the restart if that fails), then restarts. This only works when `/opt/HenWen` is itself a git checkout of the repo — true of this specific deployment, but not of installs that used `install.sh`'s plain `cp -r` (which doesn't set up git at all). The required sudoers line is installed by `install.sh` alongside the existing restart/reload rules.

### AMI connection

`AMIClient` (class in app.py ~line 915) is a raw TCP socket client to Asterisk Manager Interface on port 5038. It is a persistent connection managed by `_poll_loop`. Routes read from the AMI cache (`get_cached_status()`) rather than issuing live AMI commands — this makes most status reads sub-millisecond. Commands that must be sent live (connect/disconnect, restart, etc.) use `ami_send_command()`.

### Database

SQLite at `/etc/asterisk/henwen.db`. Schema is defined inline in `get_db()` (called per request). Migrations happen via `ALTER TABLE` checks at startup — no migration framework. Tables: `users`, `favorites`, `settings`, `announcements`, `connectors`, `id_configs`, `permanent_links`, `alert_config`, `nws_alert_config`, and a connection history log table.

`announcements.source_type` distinguishes `'upload'` (user-uploaded audio file), `'tts'` (typed text, synthesized once at save time), and `'nws_alert'` (auto-created/retired by the NWS poller) — the scheduler (`_run_due_announcements()`) treats all three identically except for two NWS-only nullable columns: `max_defer_sec` (forces playback past a busy channel after being due too long; NULL preserves indefinite defer for every other row) and `nws_expires`/`external_id` (NWS lifecycle bookkeeping, unused by upload/tts rows).

### rpt.conf parsing

Custom parser (not `configparser`) — `_collect_stanzas()`, `parse_stanza_settings()`, `update_setting_in_content()`. It preserves comments and formatting on save and handles ASL3's multi-stanza structure (node stanzas, templates, macros, schedules).

### Audio streaming

Asterisk `MixMonitor` writes raw PCM to a FIFO (`/tmp/henwen_audio_<node>.sln`). `audio_relay.py` — a standalone process spawned by `_start_broadcast()` (~line 4710 in `app.py`), not a thread inside gunicorn — paces that PCM into strict 20ms frames (injecting silence when the node is quiet) and writes them to a second FIFO (`..._paced.sln`). ffmpeg reads that second FIFO directly and encodes WebM/Opus to its stdout. `_AudioBroadcast._read_loop` fans ffmpeg's stdout out to each client's `Queue` — not as raw bytes but parsed into WebM units (`_drain_webm`): the init segment is cached and replayed to every late-joining listener (a WebM stream is only decodable from its start; without this, a second listener's demuxer fails immediately) and everything after it is fanned out as whole ~200ms Clusters. `/api/audio/stream/<node>` streams from that queue to the browser.

The pacing loop runs in its own OS process specifically to avoid GIL contention: gunicorn's request handlers, the AMI poller, and other background threads sharing this worker's GIL can stall a real-time 20ms deadline long enough to be audible as a click or stutter. Running the frame loop in a separate process lets the kernel schedule it independently. The MSE live-edge controller in `status.html` keeps the browser at ~0.5s behind live edge using `playbackRate` adjustment, with a startup watchdog and stall-recovery rebuffer logic.

### Text-to-speech (TTS) Announcements

Piper (`piper-tts` pip package, shelled out to via `PIPER_BIN` — never `import piper`, so a missing dependency fails cleanly at TTS-use time rather than crashing the whole app at startup) synthesizes typed text to a WAV, which then goes through the same `_convert_to_ulaw()` ffmpeg pipeline uploaded files use — TTS and upload rows produce byte-identical output formats and share every downstream code path. Voice models (`TTS_VOICES`, a fixed curated dict, never an open picker) live in `TTS_VOICES_DIR` (default `/var/lib/asterisk/henwen_tts_voices` — **not** under `/opt/HenWen`, which is `root:root` and unwritable by the `asterisk` user the service runs as), downloaded on demand from Hugging Face's `rhasspy/piper-voices` repo and cached thereafter. Editing a TTS announcement's text re-synthesizes to a temp file and `os.replace()`s it atomically over the existing slug path — the slug/filename never changes on edit, so this is old-content/new-content at the same path, never a window where the scheduler could hit a missing file.

### NWS severe weather alerts

`_nws_alert_poll_loop()` polls `api.weather.gov/alerts/active?zone=<UGC>` for a single configured county/zone (`nws_alert_config`, a singleton table like `alert_config`) and manages the lifecycle of `announcements` rows with `source_type='nws_alert'` — it never plays anything itself, `_run_due_announcements()` does that on its own schedule exactly as it would for any other announcement. Two independent prune paths: on a successful fetch, anything no longer in the active set gets deleted; regardless of fetch success, anything whose own `expires` is more than 30 minutes past gets deleted too (a local, network-independent ceiling so a stale alert can't replay forever during a sustained NWS outage — the fail-safe stance is "don't assume an alert ended just because NWS is unreachable," but that can't be unbounded).

Dedup/lifecycle tracking (`announcements.external_id`) uses a **parsed VTEC identity** (`{office}.{phenomena}.{significance}.{ETN}`, e.g. `KMKX.TO.W.0075`), not NWS's CAP `id` field — verified against live data that the CAP `id` changes on every `messageType: Update`, so using it directly would delete-and-recreate the row (resetting `last_played`) on every routine update to a tracked storm instead of holding the intended replay interval. The VTEC string itself also embeds a timestamp range that must be excluded from the key, since a time-extension update changes it.

Spoken text is templated (`_nws_alert_spoken_text()`), not NWS's raw headline — see the function for the exact phrasing rules (state names dropped, natural "A, B, and C" county joining, on-the-hour times drop `:00`).

### APRS-IS map layer

Optional "APRS" toggle on the kiosk's Network Map, next to the existing Radar checkbox. `start_aprs_poller()` opens one persistent connection to APRS-IS (`aprslib.IS`, receive-only login with passcode `-1` — this app only ever consumes the feed, never transmits) filtered server-side to `APRS_MAX_RADIUS_MI` (200mi) around the node's own geocoded rpt.conf location, and caches every position packet it sees in `_aprs_cache` (`{callsign: {lat, lon, symbol, comment, ts}}`, keyed by callsign so the latest report replaces the last). `GET /api/aprs/stations?radius=<1-200>` (public, same as the rest of the board's map data) filters that one shared cache by haversine distance per request — so however many kiosk viewers pick however many different radii, it still costs exactly one upstream APRS-IS connection, same "one shared in-process cache, many cheap reads" shape as the AMI/favstats/global-activity pollers. Entries older than `APRS_STALE_SEC` (2h) are pruned locally regardless of connection state, mirroring the NWS poller's local staleness ceiling.

The login callsign (`aprs_is_callsign`) is a Manager > Kiosk Settings field (`/api/kiosk/settings`, same route as idle timeout/clock format/timezone/map pin duration), not an env var — every operator using this feature must be a licensed amateur identifying with their own callsign, so each install's admin fills it in rather than the code defaulting to anything. `_aprs_poll_loop()` re-reads that setting at the top of every reconnect cycle rather than once at startup, so a saved change takes effect on the connection's next natural reconnect (or immediately after a HenWen restart) without needing one — `immortal=False` on the `aprslib` consumer call is what makes drops come back around to that check instead of aprslib reconnecting silently forever with the old identity. The value is validated server-side against `APRS_CALLSIGN_RE` (letters/digits, optional `-SSID`) before being saved or used, since it gets embedded directly in the raw APRS-IS login line. The feature no-ops (log line, not a crash) if no callsign is saved yet or the `aprslib` pip package isn't installed — same "fails cleanly at use-time" posture as Piper TTS.

The frontend's radius slider (1-200mi, default 25) and on/off state are plain `localStorage` prefs, same pattern as the Radar/tile-style toggles; a thin circle outline on the map traces the selected radius around the resolved center. Markers render as a small purple circle (`makeAprsIcon()`), deliberately not the teardrop node-pin or gold hosted-node star, fading with age the same way the global-activity pins do (`aprsPinStyle()`, mirroring `globalPinStyle()`), and live in their own `_aprsMarkers` array on a separate 5-minute poll timer (`APRS_POLL_MS`) so they're untouched by `updateMap()`'s much more frequent node/activity marker rebuild.

### Templates

- `templates/status.html` — kiosk/status board (`/` and `/status` routes); self-contained SPA with embedded JS (~1800 lines). Contains the live audio player, network map, weather bar, and global activity feed. Accessible without login.
- `templates/henwen-manager.html` — all manager pages (settings, connectors, user management, announcements, node ID, etc.) loaded as a SPA shell via `/henwen-manager`.
- `templates/login.html` — login and first-run account creation.

### Configuration

All config comes from environment variables set in `/etc/systemd/system/HenWen.service`. The service file is the single source of truth for AMI credentials, `SECRET_KEY`, paths, and tuning parameters. After editing the service file: `sudo systemctl daemon-reload && sudo systemctl restart HenWen`.

Key env vars: `AMI_USER`, `AMI_SECRET`, `SECRET_KEY`, `DB_PATH`, `SOUNDS_DIR`, `LOG_LEVEL` (`INFO`/`DEBUG`), `RPT_CONF_PATH`, `TTS_VOICES_DIR`, `PIPER_BIN`, `APRS_IS_PASSCODE` (default `-1`, receive-only), `APRS_IS_HOST`/`APRS_IS_PORT` (default `rotate.aprs2.net:14580`). The APRS-IS login callsign itself is a Kiosk Setting, not an env var — see "APRS-IS map layer".

### Auth and security

Flask-WTF CSRF on all mutating routes. Flask-Limiter on login. Three roles: Superuser (full access + raw rpt.conf editor), Admin (full access minus raw editor), User/Kiosk (connect/disconnect only). Role is stored in the SQLite `users` table and checked by `check_auth()` decorator.

Sessions are plain signed cookies — there is no server-side session store. To show a live "logged in users" count on the Status Board footer, `check_auth()` stamps each session with a random `sid` and touches an in-process dict (`_active_sessions`, guarded by `_active_sessions_lock`) on every authenticated request; `get_active_user_count()` prunes entries idle more than `ACTIVE_SESSION_WINDOW` (90s) and is called from `/api/status/board`. This state is per-worker-process — fine today since gunicorn runs `--workers 1`, but would need to move to the DB or a shared store if worker count is ever increased.

### External dependencies

- `https://stats.allstarlink.org/api/stats/{node}` — node keyed/connected counts
- `https://stats.allstarlink.org/stats/keyed` — scraped (regex, no HTML parser dependency) for the global activity feed on the kiosk map; every node currently keyed network-wide, polled every 2 min
- `https://allmondb.allstarlink.org/allmondb.php` — node callsign/location database
- `astdb.txt` — local copy of ASL node DB written by `asl3-update-nodelist` package
- `https://api.weather.gov` — NWS active alerts (`/alerts/active`) and zone lookup (`/points/{lat},{lon}`), no API key, requires a descriptive `User-Agent`
- `https://huggingface.co/rhasspy/piper-voices` — Piper TTS voice model downloads (`.onnx`/`.onnx.json`), on demand
- `https://nominatim.openstreetmap.org` — geocodes a node's free-text location string to lat/lon, feeding both the kiosk map and the NWS zone lookup; rate-limited to 1 req/1.1s in-process
- `https://mesonet.agron.iastate.edu/cache/tile.py/...nexrad-n0q-900913/{z}/{x}/{y}.png` — Iowa Environmental Mesonet's public NEXRAD composite-reflectivity tile cache, no API key; fetched client-side directly by the browser as an optional Leaflet overlay on the kiosk map (toggled via the map's "Radar" checkbox), not proxied through the backend
- `rotate.aprs2.net:14580` (APRS-IS) — nearby APRS station positions for the kiosk map's optional "APRS" layer, via the `aprslib` pip package; receive-only, requires a callsign saved in Manager > Kiosk Settings (feature is off otherwise) — see "APRS-IS map layer" above
- `https://db.satnogs.org/api/tle/` — ISS TLE (two-line element set) for the kiosk map's optional "ISS" layer, no API key; polled server-side every ~6h — see "ISS tracking (map layer)" below. Celestrak (the more commonly cited TLE source) was tried first but is unreachable from this server at the TCP level; SatNOGS DB was the working alternative.
- `https://cdn.jsdelivr.net/npm/satellite.js@5/dist/satellite.min.js` — SGP4 propagator library, loaded client-side for the ISS layer's position/pass-prediction math (see below); not a backend dependency

### ISS tracking (map layer)

Optional "ISS" toggle on the kiosk's Network Map, next to Radar/APRS, with a pass-count selector (1-6 upcoming passes). `start_iss_poller()` fetches the ISS's TLE from SatNOGS DB every `ISS_TLE_POLL_SEC` (6h) into `_iss_tle_cache`, exposed via public `GET /api/iss/tle`. That's the entire server-side role — unlike APRS/global-activity there's no per-viewer filtering of shared state to do, so all propagation happens client-side in `status.html` via `satellite.js` (SGP4): current position (updated every 5s), and upcoming visible-pass prediction (elevation ≥ `ISS_MIN_ELEV_DEG`, 10°) against the observer coordinates already on hand from the board refresh (`_hostedNodes[0]`, the same geocoded node location APRS/NWS use). Passes are found by a coarse forward time-scan for elevation-threshold crossings (`ISS_PASS_COARSE_STEP_SEC`), refined by bisection, then resampled at `ISS_PASS_FINE_STEP_SEC` to draw each pass's ground-track polyline (split at the antimeridian since each point's longitude is independently normalized to ±180°). Re-derived every `ISS_PASS_RECOMPUTE_MS` (5 min) to drop passes that already happened and roll the next one into view.

### Service identity

The systemd unit is `HenWen`, installed at `/opt/HenWen`. This was renamed from the project's original `ASL3-EZ` name (2026-07-03) — there was no install base yet at the time, so the rename covered the systemd unit, install path, sudoers rule, database path, sounds directory, TTS voice cache directory, and the `henwen/<slug>` AMI command prefix in one pass rather than leaving internal `asl3ez`-branded paths in place. If this project ever gets a real install base, do not repeat a path/unit-name rename casually — it requires a real migration script, not just an in-place edit, since existing installs would need `systemctl stop`, a data move, a new unit file, and an updated sudoers rule to not break.
