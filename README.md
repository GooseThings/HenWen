# HenWen — AllStarLink 3 Node Manager & Kiosk

A browser-based web interface for managing your AllStarLink 3 node. Runs as a systemd service on the same machine as Asterisk.

**Key features:**
- Edit `rpt.conf` field-by-field (validated dropdowns and range-checked inputs sourced from the official ASL3 docs) or switch to a raw text editor
- Connect, disconnect, and **Monitor** (listen-only, `ilink 2`) remote nodes from the browser
- **Stream live receive audio** from your node to any browser tab — WebM/Opus over HTTP, multiple simultaneous listeners
- **Status Board** (`/status`) — full-screen kiosk display for TVs and public screens: connected nodes, global activity feed, network map with grayline, weather bar
- **Smart Connector** — automatically link to a net node on a schedule (daily, weekly, monthly, one-time, and more), wait for the local node to go idle before connecting, then disconnect after an idle timeout
- **Announcements** — upload audio files and schedule them to play on a node at configured times
- **Node ID** — FCC-compliant background ID monitor: plays a sound file on key-up, on interval during continuous activity, and after the node goes idle
- **Multi-user accounts** — Superuser, Admin, and User (Kiosk) roles; kiosk accounts can connect/disconnect nodes but cannot access settings
- **Asterisk Console** — live log viewer, CLI command runner, and verbosity control, all from the browser
- Automatic `rpt.conf` backups on every save
- Dashboard with system vitals, AMI diagnostics, and a Reload/Restart Asterisk button
- 12 color themes, mobile-responsive layout

---

## Requirements

- AllStarLink 3 on **Debian 12 (Bookworm)** or **Debian 13 (Trixie)**
- Asterisk already installed and running (`systemctl status asterisk`)
- Python 3.8 or later
- Root access (required to write `/etc/asterisk/rpt.conf` and restart Asterisk)
- `ffmpeg` — required only if you use the Announcements or Node ID audio upload features (`sudo apt install ffmpeg`)

---

## Step 1 — Install

```bash
git clone https://github.com/GooseThings/HenWen.git
cd HenWen
sudo bash install.sh
```

The installer:
- Installs Python dependencies into a virtual environment
- Creates and enables the `ASL3-EZ` systemd service (runs on port 5000)  <!-- service unit name stays ASL3-EZ on existing installs -->
- Starts the service immediately

Verify it is running:

```bash
systemctl status ASL3-EZ
```

---

## Step 2 — First Launch: Create Your Account

Open a browser and go to:

```
http://YOUR_NODE_IP:5000
```

The first time you visit, you will be prompted to create the **initial Superuser account**. This account has full access to everything. Set a strong password — it is stored as a salted hash and cannot be recovered if lost.

After creating the account you will be logged in and taken to the Dashboard.

> **Tip:** You can add more accounts later under **Manager → User Management**. Use **Admin** for operators who need full access but not raw config editing. Use **User (Kiosk)** for accounts that can only connect and disconnect nodes.

---

## Step 3 — Commission: AMI Setup

Most features (node connect/disconnect, monitor, status board, smart connector, audio streaming, node ID) require a working AMI connection. This is set up once.

### 3a — Configure manager.conf

Edit `/etc/asterisk/manager.conf`:

```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1

[asl3ez]
secret = your_secret_here
read  = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
permit = 127.0.0.1/255.255.255.0
```

> The stanza name (`asl3ez` above) becomes your `AMI_USER`. Choose any name and secret you like.

Reload Asterisk to apply:

```bash
sudo asterisk -rx "module reload manager"
```

### 3b — Add credentials to the service file

```bash
sudo nano /etc/systemd/system/ASL3-EZ.service
```

Set these two lines to match what you put in `manager.conf`:

```
Environment="AMI_USER=asl3ez"
Environment="AMI_SECRET=your_secret_here"
```

Apply and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ASL3-EZ
```

### 3c — Verify

In the web UI, go to **Dashboard → AMI Diagnostics** and click **Run Test**. All checks should show green. If any fail, the output includes an exact fix instruction.

---

## Step 4 — Commission: Rotate the Secret Key

HenWen ships with a default `SECRET_KEY` that signs session cookies. You should replace it before putting the system into service.

Go to **Settings** in the web UI and click **Generate & Apply New Key** (or type your own, 16+ characters). This writes the new key to the service file and restarts the service. The Dashboard shows a warning banner until the key has been rotated.

---

## Step 5 — Verify Your rpt.conf

Go to **Manager** in the web UI. Your node stanzas from `/etc/asterisk/rpt.conf` will be listed in the sidebar. Click any node to see and edit its settings.

- Use **Reload rpt.conf** on the Dashboard after saving changes (runs `rpt restart` — no full Asterisk restart needed).
- Use **Restart Asterisk** for changes that require a full restart.

---

## Updating HenWen

HenWen's code installs to `/opt/ASL3-EZ`, while your configuration and data live elsewhere and are **not** touched by a code update:

- AMI credentials and `SECRET_KEY` — `/etc/systemd/system/ASL3-EZ.service`
- Users, favorites, connectors, announcements — `/etc/asterisk/asl3ez.db`
- rpt.conf backups — `/etc/asterisk/rpt_backups/`

The SQLite schema migrates automatically on startup, so new features need no manual database steps.

### Quick update (recommended)

Pulls the latest code and restarts the service. Your service file, database, and backups are all preserved.

```bash
cd ~/HenWen          # the directory you originally cloned into
git pull
sudo cp -r app.py templates static /opt/ASL3-EZ/
sudo systemctl restart ASL3-EZ
```

If a release adds new Python dependencies (check `requirements.txt`), also refresh the virtual environment:

```bash
sudo /opt/ASL3-EZ/venv/bin/pip install -r /opt/ASL3-EZ/requirements.txt
sudo systemctl restart ASL3-EZ
```

### Full reinstall

Re-running the installer also refreshes Python dependencies and the systemd unit. **It overwrites the service file** (`/etc/systemd/system/ASL3-EZ.service`) with the default template and re-runs AMI setup — so back up your service file first and restore it afterward, or you will lose your AMI credentials and `SECRET_KEY`:

```bash
cd ~/HenWen
git pull
sudo cp /etc/systemd/system/ASL3-EZ.service ~/ASL3-EZ.service.bak   # save your credentials
sudo bash install.sh
sudo cp ~/ASL3-EZ.service.bak /etc/systemd/system/ASL3-EZ.service   # restore them
sudo systemctl daemon-reload && sudo systemctl restart ASL3-EZ
```

### Verify

```bash
systemctl status ASL3-EZ
```

Then open the web UI and run **Dashboard → AMI Diagnostics → Run Test**. Hard-refresh the browser (Ctrl-Shift-R) to pick up any updated UI.

---

## Using the Status Board

The Status Board at `/status` (or **Status Board ↗** in the sidebar) is designed for TV or kiosk display. It requires no login to view.

Features: live node status, connected node list, global activity feed, network map with grayline, and a weather bar.

Admin and Superuser accounts can connect/disconnect nodes directly from the Status Board. Kiosk (User) accounts see a login prompt and are limited to one active connection at a time.

To set it up on a dedicated display, open `http://YOUR_NODE_IP:5000/status` in a browser and press F11 for fullscreen. Configure the map, themes, and pin duration under **Manager → Kiosk Settings**.

---

## Multi-User Accounts

Manage accounts under **Manager → User Management**.

| Role | Access |
|------|--------|
| **Superuser** | Full access including raw `rpt.conf` editor |
| **Admin** | Full access except raw editor |
| **User (Kiosk)** | Connect/disconnect nodes only; no settings |

Kiosk accounts can be given a **Favorites** list of pre-configured nodes to connect to quickly from the Status Board.

---

## Smart Connector (Auto Connector)

Found under **Manager → Smart Connector**.

Automatically links to a remote node on a schedule, waits for the local node to go idle before connecting, observes a settle period, then disconnects after a configurable idle timeout.

**Schedule types:** Manual only, Daily, Weekly (choose days), Bi-Weekly, Monthly, Every 2 Months, Quarterly, Yearly, One-Time (auto-disables after firing).

Before enabling a connector, run the **Pre-flight Diagnostics** in the same section to verify all AMI paths work correctly.

---

## Announcements

Found under **Manager → Announcements**.

Upload an audio file (mp3, wav, ogg, flac, m4a) and schedule it to play on a node. Files are converted to 8 kHz mono ULAW automatically via `ffmpeg`. Each announcement has a time window, repeat interval, and local-only vs all-links playback mode.

---

## Node ID

Found under **Manager → Node ID**.

Plays a configurable sound file for FCC-required station identification. Triggers on initial key-up (optional), every N seconds of continuous activity, and M seconds after the node goes idle. Monitors multiple nodes simultaneously.

---

## Troubleshooting

**Service won't start:**
```bash
journalctl -u ASL3-EZ -n 50
systemctl status ASL3-EZ
```

**Can't reach the web UI:**
- Confirm the service is running: `systemctl status ASL3-EZ`
- Check that port 5000 is not blocked by a firewall: `ss -tlnp | grep 5000`
- Try `http://127.0.0.1:5000` directly on the node

**Permission denied saving rpt.conf:**
- The service must run as root. Check: `grep User= /etc/systemd/system/ASL3-EZ.service` — should say `User=root`.

**rpt.conf changes don't take effect:**
- Click **Reload rpt.conf** on the Dashboard after saving. If that doesn't help, use **Restart Asterisk**.

**AMI login failed:**
- Confirm `AMI_USER` and `AMI_SECRET` in the service file exactly match the stanza name and secret in `manager.conf`.
- Verify `enabled = yes` in `[general]` of `manager.conf`.
- Confirm the AMI user stanza has `write` permissions including `command`.
- Check Asterisk is running: `systemctl status asterisk`
- Run the AMI Diagnostics test in the Dashboard for a step-by-step report.

**Node connect/disconnect has no effect:**
- Confirm AMI test passes first.
- Verify your node number appears in the local node dropdown (it must be in `rpt.conf`).
- Check the rpt.conf has a valid `[NODENUMBER]` stanza with a `rxchannel` configured.

**Asterisk restart fails from the UI:**
- Service must run as root.
- Verify Asterisk is managed by systemd: `systemctl status asterisk`

**Audio streaming (Listen) produces no sound:**
- Requires an active Asterisk channel on the node. The node must be keyed or have an active link.
- Check that `app_mixmonitor.so` is loaded: `asterisk -rx "module show like mixmonitor"`

**Announcements or Node ID audio won't upload:**
- Confirm `ffmpeg` is installed: `ffmpeg -version`
- Install if missing: `sudo apt install ffmpeg`

---

## Environment Variables

All settings are configured in the systemd service file (`/etc/systemd/system/ASL3-EZ.service`). After editing, run `sudo systemctl daemon-reload && sudo systemctl restart ASL3-EZ`. (The service unit file retains the `ASL3-EZ` name on existing installs.)

| Variable | Default | Description |
|----------|---------|-------------|
| `AMI_USER` | *(none)* | AMI username — must match `manager.conf` stanza name |
| `AMI_SECRET` | *(none)* | AMI password — must match `manager.conf` secret |
| `AMI_HOST` | `127.0.0.1` | Asterisk host |
| `AMI_PORT` | `5038` | AMI TCP port |
| `RPT_CONF_PATH` | `/etc/asterisk/rpt.conf` | Path to rpt.conf |
| `MANAGER_CONF` | `/etc/asterisk/manager.conf` | Path to manager.conf |
| `BACKUP_DIR` | `/etc/asterisk/rpt_backups` | Backup directory for rpt.conf saves |
| `DB_PATH` | `/etc/asterisk/asl3ez.db` | SQLite database (users, favorites, connectors, etc.) |
| `SOUNDS_DIR` | `/var/lib/asterisk/sounds/asl3ez` | Uploaded audio files for Announcements and Node ID |
| `PORT` | `5000` | Web server port |
| `HOST` | `0.0.0.0` | Bind address |
| `SECRET_KEY` | `henwen-change-me` | Flask session key — rotate via the Settings page |
| `SERVICE_NAME` | `ASL3-EZ` | systemd unit name (used when applying a new SECRET_KEY) |
| `SERVICE_FILE_PATH` | `/etc/systemd/system/<SERVICE_NAME>.service` | Path to the unit file the Settings page edits |
| `LOG_LEVEL` | `INFO` | HenWen log verbosity: `INFO` or `DEBUG` (full trace in journald) |
| `ASTERISK_LOG_PATH` | `/var/log/asterisk/messages.log` | Asterisk log shown in the Asterisk Console page |

---

## File Structure

```
HenWen/
├── app.py                      # Flask backend — all routes, AMI, scheduler threads
├── templates/
│   ├── index.html              # Manager shell (nav + page loader)
│   ├── henwen-manager.html     # All manager pages (settings, connectors, users, etc.)
│   ├── login.html              # Login / first-run account creation
│   └── status.html             # Status Board / kiosk display (/status)
├── requirements.txt            # Python dependencies (flask, gunicorn, werkzeug)
├── ASL3-EZ.service             # systemd unit file template
├── install.sh                  # Installer
├── uninstall.sh                # Uninstaller
├── README.md
└── CHANGELOG.md
```

---

## License

GPL-3.0 — use at your own risk. Not affiliated with AllStarLink, Inc.

*73 de N8GMZ dit dit*
