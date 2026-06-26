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

See [CHANGELOG.md](CHANGELOG.md) for the full list of changes.

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
├── README.md
└── CHANGELOG.md
```

---

## License

GPL-2.0 — use freely, at your own risk. Not affiliated with AllStarLink, Inc.

*73 de N8GMZ*
