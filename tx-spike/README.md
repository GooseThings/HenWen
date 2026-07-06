# Browser-transmitter spike (phase 1)

Proves the chosen architecture for browser-mic TX before any product code:
**browser WebRTC (JsSIP over WSS) → Asterisk PJSIP → `Rpt(643930,P)`** —
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
  (configtest-gated), generates the SIP secret, prints test credentials
- `rollback.sh` — restores the backups
- `../static/tx-test.html` — standalone test page (JsSIP self-hosted in
  `static/vendor/`); shows a live linked-node count and warns against
  keying while any node is connected

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
