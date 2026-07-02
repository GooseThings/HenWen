# HenWen — Open Issues

_Compiled 2026-07-01 by an automated code + config review (Claude). Each item is a to-do; check it off when fixed. Severities: **High** / **Med** / **Low**. Line numbers are from the state of the repo at review time and may drift as the code changes — search by symbol if a line no longer matches._

Scope reviewed: `app.py` (all ~6,100 lines), `templates/status.html`, `templates/henwen-manager.html`, `templates/login.html`, `audio_relay.py`, `install.sh`, `ami-setup.sh`, `ASL3-EZ.service`, `.gitignore`. Live service (`ASL3-EZ`) confirmed running as `User=asterisk`, bound to `0.0.0.0:5000`.

---

## Security

- [ ] **[S1] Default SECRET_KEY ships in the repo and is never randomized at install (High).**
  `ASL3-EZ.service` hardcodes `Environment=SECRET_KEY=henwen-change-me-in-production`, and `install.sh` copies that file verbatim (`install.sh:79`) without generating a random key. `SECRET_KEY` signs Flask session cookies, so a known key lets anyone forge a valid authenticated session cookie (including a superuser session) without credentials — full auth bypass. The default is public in the GitHub repo. `app.py:72,87-88` even track this as a known-default to warn about, but nothing forces a change.
  **Fix:** Generate a random `SECRET_KEY` during `install.sh` (e.g. `python3 -c "import secrets;print(secrets.token_hex(32))"`) and write it into the unit file before first start; refuse to start (or force setup) while the key is a known default.

- [ ] **[S2] Secret-key rotation from the UI cannot work under the shipped service config (High, blocks remediation of S1).**
  `api_set_secret_key` (`app.py:4617`) writes `/etc/systemd/system/ASL3-EZ.service`, runs `systemctl daemon-reload`, and restarts the unit. The service runs as `asterisk` (`ASL3-EZ.service` `User=asterisk`, confirmed live), which cannot write a root-owned unit file nor run `systemctl` for another/again its own unit (no sudoers/polkit rule exists — `sudo -l -U asterisk` = not allowed). So the one in-product remediation for S1 fails with PermissionError. Same root cause affects **[S2b] `api_restart` → `systemctl restart asterisk` (`app.py:2890`)** and **the service-restart path** — the Dashboard "Restart Asterisk" button and secret-key rotation are non-functional as shipped.
  **Fix:** Either install a tightly-scoped polkit/sudoers rule for the specific `systemctl` actions during `install.sh`, or document that these actions require the service to run as root, and make the UI surface the PermissionError clearly instead of a generic 500/403.

- [ ] **[S3] Manager UI exposed on all interfaces over cleartext HTTP with insecure cookies (High in aggregate).**
  `HOST=0.0.0.0` (`ASL3-EZ.service`), `SECURE_COOKIES` defaults to `false` (`app.py:82`), and `install.sh` opens the firewall port (`install.sh:82-88`). The full manager — node control, raw `rpt.conf` editor, user management, Asterisk CLI — is reachable on the LAN/WAN with no TLS and session cookies sent in the clear. Combined with S1 this is trivially remotely exploitable.
  **Fix:** Document/recommend binding to `127.0.0.1` behind a TLS reverse proxy, set `SECURE_COOKIES=true` when TLS is terminated, and consider not auto-opening the firewall port.

- [ ] **[S4] Live audio of any node is streamable without authentication (Med).**
  `api_audio_stream` is in the `_PUBLIC` set (`app.py:375`) and `/api/audio/stream/<node>` (`app.py:4353`) starts a MixMonitor + `ffmpeg` + `audio_relay.py` pipeline on demand. Any unauthenticated client on the network can (a) listen to live repeater audio and (b) spawn server-side encoder/relay processes. Node must have an active channel, so it's bounded to real local nodes, but it's still unauthenticated resource spend and passive eavesdropping. `api_audio_stop` (`app.py:4408`) is also public, so any anonymous client can tear down another listener's broadcast.
  **Fix:** Decide whether audio should be public (kiosk) or gated; if public, add per-IP rate limiting/connection caps and don't let anonymous callers stop an active broadcast.

- [ ] **[S5] `/api/nodestats/batch` does not validate node values before URL interpolation (Low, SSRF-ish, admin-only).**
  `api_nodestats_batch` (`app.py:3474`) formats each `node` from the request body straight into `ASL_STATS_URL.format(node)` with no `isdigit()` check (unlike the single-node `api_node_stats` at `app.py:3460`). Host is hardcoded so this is path-injection against `stats.allstarlink.org` only, and the endpoint requires admin. Still, validate each node as `\d{4,7}`.

- [ ] **[S6] `/logout` is a GET that mutates session state (Low, CSRF).**
  `logout` (`app.py:534`) clears the session on GET, so a cross-site `<img src=".../logout">` can force-log-out a user. Minor. Consider POST + CSRF, or accept as low-risk.

- [ ] **[S7] Placeholder `AMI_SECRET=yourpassword` shipped in unit file (Low).**
  `ASL3-EZ.service`. Detected as a placeholder by `parse_manager_conf` (`app.py:615`) and reconfigured by `ami-setup.sh`, so low impact, but it is a committed default credential string. Ensure ami-setup always overwrites it.

- [ ] **[S8] Superuser Asterisk CLI blocklist is prefix-based and shallow (Low, superuser-only).**
  `api_asterisk_command` (`app.py:4564`) blocks a fixed list of `core stop/restart...` prefixes. Superuser is already fully trusted (raw editor, service control), so this is defense-in-depth only, but the blocklist is easily sidestepped (leading whitespace is stripped, but e.g. module unload / other disruptive CLI verbs aren't covered). Treat as "superuser is root-equivalent" and document that, rather than relying on the blocklist.

---

## Correctness / Reliability

- [ ] **[C1] `_favstats_poll_loop` can crash its thread permanently on a DB error (Med).**
  In `app.py:1249-1296`, `nodes` is assigned inside the `try` (`nodes = [...]` at 1257) but referenced *outside* it at `if any_429 or (nodes and not any_success)` (1288). If `get_db()`/`db.execute()` raises before `nodes` is bound, the `except` logs it, then line 1288 raises `NameError` (unbound `nodes`) which is *not* caught, so the `while True` loop exits and the favorites-status poller dies for the life of the process (favorites keyed/connected counts silently stop updating).
  **Fix:** Initialize `nodes = []` before the `try`.

- [ ] **[C2] Restart/reload features silently ineffective as shipped (Med).** See **S2** — same root cause (non-root service user, no polkit). The Dashboard restart button and `/api/reload` invoke `systemctl`/`asterisk -rx` directly and will fail or no-op without privilege. Track jointly with S2.

- [ ] **[C3] `get_db()` recreates schema and runs migration checks on every request (Low, perf).**
  `get_db()` (`app.py:173-335`) issues all the `CREATE TABLE IF NOT EXISTS`, `PRAGMA table_info`, and migration `ALTER`/`UPDATE` statements on *every* call, and it's called per-request and inside tight poller loops. Works, but it's avoidable overhead and repeated write transactions. Consider a one-time init guarded by a flag, with `get_db()` just opening a connection.

- [ ] **[C4] Per-worker in-process state will break if `--workers` is ever raised (Low, latent).**
  `_active_sessions`, `_ami_cache`, `_kiosk_temp_conns`, `_link_stats`, audio broadcasts, etc. all live in process memory and assume `--workers 1` (documented in `CLAUDE.md`). This is fine today but is a foot-gun; note it next to any future scaling work. No action needed now beyond awareness.

- [ ] **[C5] `_ami_cache` is read from request threads without the pool lock (Low).**
  Routes and pollers read `_ami_cache`/`_ami_cache_ts` (e.g. `get_cached_status` `app.py:1190`, `_run_due_announcements` `app.py:5010`, `_node_active` `app.py:5234`) while `_poll_loop` mutates them. CPython dict ops make this crash-safe in practice, but a status read can observe a torn/half-updated view. Low priority; if tightening, snapshot under a lock.

---

## UI / UX

- [ ] **[U1] XSS surface is well-mitigated — keep it that way (informational).**
  Both SPAs consistently route attacker-influenceable data (node callsigns/locations scraped from `stats.allstarlink.org` and `allmondb`, connection-history peer fields, Asterisk log lines) through `esc()` before `innerHTML` (`status.html:512`; `henwen-manager.html:1909`). Spot-checked node card, connected list, activity feed, Leaflet popups (`status.html:792,804,839`), backup diff colorizer (`henwen-manager.html:2866`), Asterisk log viewer (`3595`), and connector/announcement/ID cards — all escape. **To-do:** add a lint/review checklist item so new `innerHTML` sites keep using `esc()`; the manager has ~130 `innerHTML` sites so regressions are easy.

- [ ] **[U2] Verify Restart/secret-key error surfacing in the UI (Med, tied to S2/C2).**
  Because those actions fail with PermissionError under the shipped `User=asterisk`, confirm the manager shows an actionable message (not a bare 500 or a success toast followed by nothing happening). Needs live testing in a browser as admin/superuser.

- [ ] **[U3] Live browser pass still needed (Med).**
  This review was static (code vs. routes). A real click-through of the kiosk and every manager tab — on desktop and a phone-width viewport — has not been done. Check: dead buttons, mobile layout of the map/tables, the audio player start/stop, and that every `fetch` sends the CSRF token after a re-login (`api_login` returns a fresh token at `app.py:564-565` — verify the JS actually swaps it in without a reload).

---

## Audio: clicks / pops (under investigation)

- [ ] **[A1] Investigate audible clicks/pops in the live audio stream.**
  Reported by the user. Pipeline: Asterisk MixMonitor → in-FIFO → `audio_relay.py` (20 ms frame pacing + silence injection) → paced-FIFO → `ffmpeg` (libopus) → browser MSE. Findings will be appended below as they are confirmed. (See the follow-up section this session for detailed notes.)

---

## Housekeeping

- [ ] **[H1] Stray 0-byte `asl3ez.db` committed to / sitting in the repo root (Low).**
  `/opt/ASL3-EZ/asl3ez.db` (0 bytes) exists untracked in the working tree. The real DB is `/etc/asterisk/asl3ez.db`. This root-level file is created if the app is ever run with a relative `DB_PATH`. Add `asl3ez.db` (or `*.db`) to `.gitignore` and delete the stray file so it can't be committed by accident.

- [ ] **[H2] `.claude/` directory untracked in repo root (Low).**
  Decide whether to commit shared agent config or add `.claude/` to `.gitignore`.

- [ ] **[H3] `.gitignore` ignores `*.bak` (Low, intentional?).**
  Fine given backups live in `/etc/asterisk/rpt_backups`, just confirm no intended `.bak` fixtures are being missed.
