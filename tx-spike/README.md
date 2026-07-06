# Browser-transmitter spike (phase 1)

Proves the chosen architecture for browser-mic TX before any product code:
**browser WebRTC (JsSIP over WSS) → Asterisk PJSIP → `Rpt(<node>,P)`** —
phone-control mode, PTT = DTMF `*99`, unkey = `#`. Audio is G.711 µ-law
end-to-end (WebRTC-mandatory codec, app_rpt native — no transcoding, no
Opus module needed). No audio touches the HenWen/Flask process.

## Files

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
| 443 | TCP | inbound → this box | HTTPS kiosk + SIP-over-WSS signaling (Apache) |
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

1. `sudo bash tx-spike/apply.sh`
2. Open `https://<host>/static/tx-test.html`, paste the printed password.
3. Connect & Register → expect REGISTERED.
4. Call node → expect CALL CONFIRMED and node audio in the browser.
5. **Only with zero linked nodes:** hold PTT briefly and speak; verify the
   node keys (kiosk keyed indicator / `rpt show variables` RPT_RXKEYED).

## Rollback

`sudo bash tx-spike/rollback.sh` — restores all configs; loaded modules
stay resident (harmless, unreferenced) until Asterisk next restarts.
