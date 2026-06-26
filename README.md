# ASL3-EZ - AllStarLink 3 Node Manager

A browser-based web interface for managing your AllStarLink 3 nodes:
- Edit `rpt.conf` with field-by-field or raw text editing
- Field-by-field editing validates values against the official ASL3 docs (dropdowns for fixed-choice settings, numeric inputs with min/max for timers) so you can't save an invalid value
- Connect, disconnect, and monitor nodes via AMI (Asterisk Manager Interface)
- Automatic backups on every save
- Dashboard with system status and verbose debug logging
- Node lookup from local astdb and AllStarLink stats API
- Restart Asterisk, or just reload `rpt.conf` live, from the Dashboard
- Settings page to rotate the app's Flask `SECRET_KEY` without hand-editing the service file

---

## Recent Changes

- **Added: Asterisk Console page.** View the live Asterisk log (`/var/log/asterisk/messages.log`) directly from the web UI — equivalent to tailing the log file or running `asterisk -rvvv`. Includes a verbosity level selector (0–5, where 3 = `-vvv`) that applies immediately via AMI (`core set verbose N`) without a restart, a line count selector (50–1,000 lines), a text filter, and a 5-second auto-refresh toggle. Log lines are displayed newest-first and color-coded by level (ERROR / WARNING / NOTICE / DEBUG / VERBOSE). The Clear button records the time it was clicked — auto-refresh only shows lines logged after that point; a Recall button restores the full log. A CLI input lets you run any Asterisk CLI command via AMI directly from the browser (Enter or Run button; ↑/↓ for history); output appears inline. A collapsible Command Reference covers Core, RPT/ASL3, Logger, Modules, Manager/AMI, and SIP/PJSIP commands — clicking any entry populates the input. A new `LOG_LEVEL` environment variable (default `INFO`) controls ASL3-EZ's own log verbosity in journald — set `LOG_LEVEL=DEBUG` in the service file to enable full trace output.
- **Added: Node ID module (ASL3-ID integration).** FCC-compliant repeater identification running as a background monitor. Watches keyed state via AMI and plays a configurable sound file at three points: on initial key-up (optional), every N seconds during continuous activity (default 10 min), and M seconds after the node goes idle (default 2 min). Upload an mp3/wav/ogg/flac/m4a file directly from the UI — it is converted to 8 kHz mono ULAW automatically and the Asterisk sound path is filled in for you. Multiple monitors can run simultaneously on different nodes. State badge shows live status (Idle / Active with countdown to next interval ID / Final ID pending with countdown) and auto-refreshes while a monitor is active.
- **Fixed: Smart Connector not disconnecting after the net ends.** Three bugs caused connectors to get stuck in "connected" forever: (1) no link verification — the scheduler never checked whether the target was actually still in `rpt lstats`, so when the remote node dropped the connection our state stayed connected indefinitely; (2) a NULL `last_activity` fallback that silently produced `idle_sec=0` every tick so the idle threshold was never crossed; (3) hard AMI errors from the iLink command were swallowed and the connector was incorrectly marked idle. All three are fixed.
- **Added: Smart Connector — scheduled node connect/disconnect with idle monitoring.** Automatically links to a remote node at a configured time each day, waits for the local node to be idle before connecting, observes a settle period after connect, then monitors activity and disconnects after a configurable idle timeout. Manual connect/disconnect buttons available at any time. State badge (Idle / Waiting / Connected / Error) auto-refreshes while active. Sync State button cross-checks DB state against live `rpt lstats` and corrects drift.
- **Added: Smart Connector pre-flight diagnostics.** Runs 5 real AMI smoke tests — AMI command permission, node variable read (RPT_RXKEYED/RPT_TXKEYED parseable), link stats readability, iLink command path (verifies `rpt cmd ilink` is accepted by Asterisk), and target node link state. Each failed test includes an exact fix instruction. Not a dry run — these exercise the same code paths the connector uses.
- **Added: Announcements module.** Upload mp3, wav, ogg, flac, or m4a files and schedule them to play automatically on a node. Files are converted to 8 kHz mono ULAW via ffmpeg on upload and stored in `/var/lib/asterisk/sounds/asl3ez/`. Each announcement has a configurable time window, repeat interval, and local-only vs all-links playback mode. Enable/disable per announcement, test-play button fires immediately outside the schedule.
- **Added: About card to the Settings page.** Shows author info, project links (GitHub, WE8CHZ.org, GPL-2.0 license), and AllStarLink resource links (portal, ASL3 docs, rpt.conf reference, community forum).
- **Added: 12 color themes**, selectable from the Settings page. Dark (default), Midnight (OLED black), Light, Terminal (green phosphor), Amber (amber phosphor), Nord, Mario (NES Super Mario Bros palette), Dracula (purple dark), Monokai (Sublime Text classic), Cyberpunk (neon magenta on void black), Solarized (warm light), and Cotton Candy (soft pastel light). Switching is instant with no page reload; the choice persists in `localStorage` and applies to the login page as well.
- **Added: login/authentication.** The first time you open the app you are prompted to create a username and password. All pages and API endpoints require a valid session after that. Passwords are stored as salted hashes (werkzeug pbkdf2:sha256) in the local SQLite database — never in plain text. The Settings page has a Change Password form. The Flask `SECRET_KEY` (which signs session cookies) can still be rotated from Settings; rotating it invalidates all active sessions.
- **Compacted UI and improved mobile support.** Tighter spacing and a smaller base font make better use of screen space on all devices. On mobile, the sidebar collapses to a sticky top bar with a hamburger button — the nav is hidden by default and auto-closes after selecting a page, so the content is immediately visible. Toggle rows (on settings pages) wrap on narrow screens so the key name is always readable. Buttons get a larger minimum touch target on mobile.
- **Added: quick-reference guides on macro and schedule pages.** Each macro stanza page now has a collapsible "Quick Guide" card covering allowed DTMF characters, common app_rpt commands, and example entries. Each schedule stanza page has a matching guide with the cron field reference, range/value rules, and examples. Both guides start collapsed so they stay out of the way once you know the format.
- **Fixed: commented-out stanza headers not acting as section boundaries.** A disabled header like `;[daq-cham-1]` was not being recognized as a stanza boundary, so example content under it (all comments, but valid `key = value` syntax) was attributed to the preceding real stanza. Confirmed live: `rpt.conf`'s disabled `[daq-cham-1]` / `[meter-faces]` / `[alarms]` example blocks were bleeding phantom keys into the `[macro]` stanza.
- **Fixed: numeric-keyed settings (macro/schedule slots) not parsing.** The key regex required a letter or underscore as the first character, so entries like `1 = *327096` in a `[macro]` stanza were silently skipped.
- **Fixed: rpt.conf changes via the web UI not taking effect.** The "Reload rpt.conf" action was calling `asterisk -rx "rpt reload"`, which is not a real app_rpt CLI command — Asterisk's `-rx` exits 0 even for unknown commands, so the app reported success while silently doing nothing. Changes only ever took effect after a full Asterisk restart. Now uses the real command, `rpt restart`, and the endpoint checks the actual output instead of trusting the exit code.
- **Fixed: a parser bug that invented fake settings.** Stock `rpt.conf` documents `node_lookup_method`'s valid values as indented comment lines (e.g. `;both = dns lookup first...`). The parser was reading these as real (disabled) settings named `both`, `dns`, and `file`. The General Settings page no longer shows these phantom entries.
- **Fixed: toggling a setting on/off could blank its value.** The enable/disable switch and the value field were saved independently, so flipping just the switch (without touching the text field) could save an empty value, and vice versa. Both are now captured together.
- **Added: server- and client-side validation for rpt.conf settings**, sourced from the official [AllStarLink rpt.conf documentation](https://allstarlink.github.io/config/rpt_conf/). Fixed-choice settings (`duplex`, `archiveformat`, `telemdefault`, etc.) now render as dropdowns instead of free text, and timer/numeric settings get range-checked number inputs. The same rules are enforced again on the backend (`/api/save`), so a request that bypasses the UI can't write an invalid value either. Several setting descriptions were also corrected against the docs (e.g. `duplex` mode meanings were wrong; an unverified "illegal in US" claim on `beaconing` was removed).
- **Added: a Settings page** to rotate the Flask `SECRET_KEY` from the UI — generates a strong random key (or accepts a custom one, 16+ characters), writes it into the systemd unit file, and restarts the service to apply it. The Dashboard warns if you're still on the default key.
- **Fixed: AMI command injection.** `/api/ami/connect`, `/api/ami/disconnect`, and `/api/ami/perm_connect` build raw AMI protocol commands from request fields. `local_node`/`remote_node` are now required to be numeric and `mode` is restricted to the documented `ilink` function numbers (1, 2, 3, 6, 12, 13) on every endpoint — previously some fields were unvalidated, which could let a request smuggle extra AMI actions (including system-level ones, since the AMI user typically has `command`/`system` permissions).
- **Fixed: a DOM-XSS gap** in the frontend's `esc()` helper — it escaped `&`, `<`, `>`, and `"` but not `'`, which mattered for any value rendered inside a single-quoted `onclick` attribute. Now escapes all five.

---

## Requirements

- AllStarLink 3 on Debian 12 (Bookworm) or 13 (Trixie)
- Python 3.8 or later
- Root access (required to write `/etc/asterisk/rpt.conf` and restart Asterisk)

---

## Quick Install

```bash
git clone https://github.com/GooseThings/ASL3-EZ.git
cd ASL3-EZ
sudo bash install.sh
```

Then open: `http://YOUR_NODE_IP:5000`

---

## Manual AMI Setup (Required for Node Control)
!!! Do not do this if automatic setup worked !!!
Check in AMI Diagnostics in the Dashboard

The Node Control and status features require AMI credentials.

### Step 1 — Configure manager.conf

Edit `/etc/asterisk/manager.conf`:

```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1

[admin]
secret = your_secret_here
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
permit = 127.0.0.1/255.255.255.0
```

Reload Asterisk after editing:
```bash
sudo asterisk -rx "module reload manager"
```

### Step 2 — Set credentials in service file

```bash
sudo nano /etc/systemd/system/ASL3-EZ.service
```

Set these two lines to match your manager.conf:
```
Environment="AMI_USER=admin"
Environment="AMI_SECRET=your_secret_here"
```

Then apply:
```bash
sudo systemctl daemon-reload
sudo systemctl restart ASL3-EZ
```

### Step 3 — Verify

Go to **AMI Diagnostics** in the web UI and click **Run Test**. You should see a green success message.

---

## Changing the Flask Secret Key

ASL3-EZ ships with a generic default `SECRET_KEY`. It signs the session cookies that keep you logged in, so using a weak or well-known key would let an attacker forge a valid session. Rotating it invalidates all active sessions (everyone gets logged out).

Go to **Settings** in the web UI and click **Generate & Apply New Key** (or enter your own, 16+ characters). This writes the new key into the systemd unit file and restarts ASL3-EZ to apply it — the page will briefly disconnect, then reload it. The Dashboard shows a warning banner if you're still on the default key.

---

## Troubleshooting

**Service won't start:**
```bash
journalctl -u ASL3-EZ -n 50
systemctl status ASL3-EZ
```

**Permission denied saving rpt.conf:**
- The service must run as root. Verify `User=root` is in the service file.
- Check: `ls -la /etc/asterisk/rpt.conf`

**Saved rpt.conf changes don't seem to take effect:**
- Use **Reload rpt.conf** on the Dashboard (runs `rpt restart`, which re-reads rpt.conf without a full Asterisk restart) after saving.
- If that still doesn't help, fall back to a full **Restart Asterisk**.

**AMI login failed:**
- Check `AMI_USER` and `AMI_SECRET` in the service file match exactly what is in manager.conf.
- Verify `enabled = yes` in `[general]` of manager.conf.
- Verify the user stanza has `write` including `command`.
- Check Asterisk is running: `systemctl status asterisk`

**Asterisk restart fails from the UI:**
- Service must run as root.
- Verify Asterisk is managed by systemd: `systemctl status asterisk`

**Node Control not connecting:**
- Confirm AMI test passes first (AMI Diagnostics page).
- Verify your node number appears in the local node dropdown.
- Check the rpt.conf has a valid `[NODENUMBER]` stanza.

---

## Environment Variables

All settings can be overridden in the service file:

| Variable           | Default                                       | Description                                   |
|--------------------|------------------------------------------------|------------------------------------------------|
| `AMI_USER`         | (none)                                          | AMI username — MUST be set                    |
| `AMI_SECRET`        | (none)                                          | AMI password — MUST be set                    |
| `AMI_HOST`          | `127.0.0.1`                                    | Asterisk host                                  |
| `AMI_PORT`          | `5038`                                          | AMI TCP port                                   |
| `RPT_CONF_PATH`     | `/etc/asterisk/rpt.conf`                       | Path to rpt.conf                               |
| `MANAGER_CONF`      | `/etc/asterisk/manager.conf`                   | Path to manager.conf                           |
| `BACKUP_DIR`        | `/etc/asterisk/rpt_backups`                    | Backup directory                               |
| `PORT`              | `5000`                                          | Web server port                                |
| `HOST`              | `0.0.0.0`                                      | Bind address                                   |
| `SECRET_KEY`        | `asl3-ez-change-me`                            | Flask session key — change via the Settings page |
| `SERVICE_NAME`      | `ASL3-EZ`                                      | systemd unit name, used when applying a new SECRET_KEY |
| `SERVICE_FILE_PATH` | `/etc/systemd/system/<SERVICE_NAME>.service`   | Path to the systemd unit file the Settings page edits |
| `LOG_LEVEL`         | `INFO`                                          | ASL3-EZ log verbosity: `INFO` (default) or `DEBUG` (full trace in journald) |
| `ASTERISK_LOG_PATH` | `/var/log/asterisk/messages.log`               | Asterisk log file shown in the Asterisk Console page |

---

## File Structure

```
ASL3-EZ/
├── app.py                  # Flask backend
├── templates/
│   ├── index.html          # Main single-page web UI
│   └── login.html          # Login / first-run setup page
├── requirements.txt        # Python deps (flask, gunicorn)
├── ASL3-EZ.service         # systemd unit file
├── install.sh              # Installer
├── uninstall.sh            # Uninstaller
└── README.md
```

---

## License

GPL-2.0 — use freely, at your own risk. Not affiliated with AllStarLink, Inc.

*73 de N8GMZ*
