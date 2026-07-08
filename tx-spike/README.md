# Browser-transmitter spike (phase 1)

Proves the chosen architecture for browser-mic TX before any product code:
**browser WebRTC (JsSIP over WSS) → Asterisk PJSIP → `Rpt(<node>,P)`** —
phone-control mode, PTT = DTMF `*99`, unkey = `#`. Audio is G.711 µ-law
end-to-end (WebRTC-mandatory codec, app_rpt native — no transcoding, no
Opus module needed). No audio touches the HenWen/Flask process.

## Which HTTPS setup script?

The TX button needs a secure browser context, which plain HTTP doesn't
provide off localhost — pick one:

| Situation | Script |
|---|---|
| Public hostname, ports 80 and 443 (or another port) forwardable | `setup-https.sh` |
| Public hostname, but your ISP blocks port 80 (so no HTTP-01 challenge) | `setup-https.sh --dns-manual` |
| No port forwarding at all — blocked entirely, CGNAT, or you'd just rather not expose this to the public internet | `setup-tailscale.sh` |

`install.sh` offers to run `setup-https.sh` interactively at install time;
run either script manually otherwise. Both write
`/etc/asterisk/henwen-https-{port,mode,hostname}`, which `apply.sh` and
`check-ports.sh` read to adapt automatically — neither hardcodes port 443.

## Files

- `setup-https.sh` — provisions Apache + a Let's Encrypt certificate for a
  public hostname you point at this box (`certbot --apache`).
  `sudo bash setup-https.sh [--port N] [--dns-manual] <hostname> <email>`.
  `--port N` serves HTTPS on a nonstandard port (useful when an ISP blocks
  forwarding ports below 1000 but allows a high port — the TX button
  adapts on its own, it just reads the port from the page's own URL).
  `--dns-manual` switches to a manual DNS-01 challenge for ISPs that block
  port 80 itself (Let's Encrypt's HTTP-01 validator always hits port 80
  externally, regardless of `--port`, so this is the only option in that
  case — trade-off is it doesn't auto-renew). Produces
  `/etc/apache2/sites-enabled/henwen-ssl.conf` — the exact path `apply.sh`
  and `check-ports.sh` expect — even though certbot's own naming
  convention would otherwise call it `henwen-le-ssl.conf`.
- `setup-tailscale.sh` — the alternative when no inbound port works at all.
  Uses Tailscale's own `tailscale cert` to get a real, publicly-trusted
  certificate for a private `*.ts.net` hostname with zero port forwarding
  — Tailscale devices reach each other over its own mesh network, not the
  public internet. Apache is bound only to this box's Tailscale IP, not
  all interfaces, since the whole point is staying off the public
  internet. Only devices joined to your tailnet can reach the Kiosk or use
  TX. Installs a daily systemd timer (`renew-tailscale-cert.sh`) since
  `tailscale cert` doesn't auto-renew on its own.
  `sudo bash setup-tailscale.sh [--port N]`.
- `modules.snippet` — PJSIP/WebRTC module loads appended to `modules.conf`
  (ASL3 SIP-phone guide's set + websocket transport + SRTP)
- `pjsip.snippet` — WS transport + one `henwen-tx` endpoint (`webrtc=yes`,
  ulaw only, locked to a context that can dial exactly one extension)
- `extensions-custom.snippet` — that context, installed via the
  `#tryinclude "custom/extensions.conf"` hook ASL3 already ships
- `apply.sh` — backs everything up, applies the above, live-loads the
  modules (no Asterisk restart), adds the Apache `/asterisk-ws` WSS proxy
  (configtest-gated), generates the SIP secret, prints test credentials.
  Derives the local node number from `rpt.conf` (same rule as `app.py`'s
  `get_node_numbers()`: first top-level `[NNNN]` stanza) and substitutes it
  into `pjsip.snippet`/`extensions-custom.snippet` — pass it explicitly as
  `sudo bash apply.sh <node>` to override
- `rollback.sh` — restores the backups
- `check-ports.sh` — verifies every network requirement (local listeners,
  WSS handshake, STUN/NAT probe from the RTP range in public mode, or that
  `ice_host_candidates` actually maps to the current Tailscale IP in
  Tailscale mode) and reports PASS/FAIL
- `tx-test.html` — standalone debug page, deliberately NOT web-served
  (it was removed from `static/` once the kiosk integration landed); to
  use it, temporarily copy it into `static/` and remove it afterward. Once
  served through HenWen (and logged in as admin/superuser in that browser),
  it self-populates username/password/dial from `/api/tx/config` — no
  editing needed regardless of which node it's running on

## Network requirements (router port forwards)

**Public/certbot mode** (`setup-https.sh`) — the feature does not work
without these; **be explicit about them at install time**:

| Port | Proto | Direction | Purpose |
|------|-------|-----------|---------|
| 443 (or your `--port`) | TCP | inbound → this box | HTTPS kiosk + SIP-over-WSS signaling (Apache) |
| 80 | TCP | inbound → this box | Let's Encrypt HTTP-01 challenge — not needed with `--dns-manual` |
| 10000–10100 | UDP | inbound → this box | WebRTC RTP media (Asterisk; range set by apply.sh) |
| — | UDP | outbound | STUN to stun.l.google.com (both sides' ICE candidates) |

**Tailscale mode** (`setup-tailscale.sh`) — none of the above need
forwarding. Everything, including RTP media, rides the tailnet directly
between this box and any tailnet-joined browser. Since there's no
port-forwarded public IP for `stunaddr` to discover in this mode,
`apply.sh` skips it and instead writes a `[ice_host_candidates]` mapping
in `rtp.conf` (this box's LAN address(es) → its Tailscale IP,
`include_local_address` so on-LAN clients still connect directly too) —
without it, Asterisk would advertise its private LAN address in ICE
candidates, which a remote tailnet browser can't reach even though
signaling connects fine. `apply.sh` is safe to re-run after switching
modes: it always regenerates this section to match whichever mode
`henwen-https-mode` currently says.

Asterisk's builtin HTTP server (8088) stays loopback-only and must **not**
be forwarded, in either mode. LAN-only operators (no HTTPS at all, TX
disabled) need none of the inbound forwards either — ICE host candidates
connect directly.

Run `sudo bash tx-spike/check-ports.sh` to verify all of the above from
the box itself — it reads whichever HTTPS mode/port was actually
configured (falling back to public/443 if neither setup script has been
run yet) and adjusts its checks accordingly, including skipping the
public-NAT STUN probe entirely in Tailscale mode. Exposure profile of the
RTP forward in public mode: Asterisk binds ports only for active calls
(kernel drops the rest), active TX calls are DTLS-SRTP, and `strictrtp`
(default on) locks each session to its learned peer.

## Test procedure

0. If this box doesn't already have HTTPS: `sudo bash tx-spike/setup-https.sh <hostname> <email>` (or `setup-tailscale.sh` — see "Which HTTPS setup script?" above)
1. `sudo bash tx-spike/apply.sh`
2. Open `https://<host>/static/tx-test.html`, paste the printed password.
3. Connect & Register → expect REGISTERED.
4. Call node → expect CALL CONFIRMED and node audio in the browser.
5. **Only with zero linked nodes:** hold PTT briefly and speak; verify the
   node keys (kiosk keyed indicator / `rpt show variables` RPT_RXKEYED).

## Rollback

`sudo bash tx-spike/rollback.sh` — restores all configs; loaded modules
stay resident (harmless, unreferenced) until Asterisk next restarts.
