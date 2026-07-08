# Browser-transmitter spike (phase 1)

Proves the chosen architecture for browser-mic TX before any product code:
**browser WebRTC (JsSIP over WSS) → Asterisk PJSIP → `Rpt(<node>,P)`** —
phone-control mode, PTT = DTMF `*99`, unkey = `#`. Audio is G.711 µ-law
end-to-end (WebRTC-mandatory codec, app_rpt native — no transcoding, no
Opus module needed). No audio touches the HenWen/Flask process.

## HTTPS is required first

The TX button needs a secure browser context, which plain HTTP doesn't
provide off localhost. `setup-https.sh` provisions this via Apache + a
Let's Encrypt certificate for a public hostname pointed at this box — see
[Files](#files) below for its options (`--port`, `--dns-manual`).

If you don't have a public hostname or usable port forwarding at all
(CGNAT, a blocked ISP, a cloud box you don't want publicly exposed), that's
outside what these scripts automate — HenWen deliberately doesn't install
or manage a VPN/tunnel client on your box for you. Any third-party service
that can front HTTPS for a local port works instead (Tailscale Serve or
Funnel, Cloudflare Tunnel, a reverse proxy on another box you control,
etc.); set it up yourself, then run `apply.sh` as usual — it only cares
that `/etc/apache2/sites-enabled/henwen-ssl.conf` exists with a working
`ProxyPass / http://127.0.0.1:5000/`. Whatever you use needs to proxy two
things to this box: plain HTTP to Flask's port (default 5000), and a
WebSocket-capable proxy to Asterisk's `:8088` for `/asterisk-ws` (see
`apply.sh`'s "Apache WSS proxy" step for the exact line it inserts, if
you're wiring that path up by hand instead of via Apache).

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
  WSS handshake, STUN/NAT probe from the RTP range) and reports PASS/FAIL
- `tx-test.html` — standalone debug page, deliberately NOT web-served
  (it was removed from `static/` once the kiosk integration landed); to
  use it, temporarily copy it into `static/` and remove it afterward. Once
  served through HenWen (and logged in as admin/superuser in that browser),
  it self-populates username/password/dial from `/api/tx/config` — no
  editing needed regardless of which node it's running on

## Network requirements (router port forwards)

The feature does not work without these — **be explicit about them at
install time**:

| Port | Proto | Direction | Purpose |
|------|-------|-----------|---------|
| 443 (or your `--port`) | TCP | inbound → this box | HTTPS kiosk + SIP-over-WSS signaling (Apache) |
| 80 | TCP | inbound → this box | Let's Encrypt HTTP-01 challenge — not needed with `--dns-manual` |
| 10000–10100 | UDP | inbound → this box | WebRTC RTP media (Asterisk; range set by apply.sh) |
| — | UDP | outbound | STUN to stun.l.google.com (both sides' ICE candidates) |

Asterisk's builtin HTTP server (8088) stays loopback-only and must **not**
be forwarded. LAN-only operators need none of the inbound forwards — ICE
host candidates connect directly.

Run `sudo bash tx-spike/check-ports.sh` to verify all of the above from
the box itself (it also STUN-probes from inside the RTP range and reports
whether the NAT/forward preserves ports). Exposure profile of the RTP
forward: Asterisk binds ports only for active calls (kernel drops the
rest), active TX calls are DTLS-SRTP, and `strictrtp` (default on) locks
each session to its learned peer.

## Test procedure

0. If this box doesn't already have HTTPS: `sudo bash tx-spike/setup-https.sh <hostname> <email>`
1. `sudo bash tx-spike/apply.sh`
2. Open `https://<host>/static/tx-test.html`, paste the printed password.
3. Connect & Register → expect REGISTERED.
4. Call node → expect CALL CONFIRMED and node audio in the browser.
5. **Only with zero linked nodes:** hold PTT briefly and speak; verify the
   node keys (kiosk keyed indicator / `rpt show variables` RPT_RXKEYED).

## Rollback

`sudo bash tx-spike/rollback.sh` — restores all configs; loaded modules
stay resident (harmless, unreferenced) until Asterisk next restarts.
