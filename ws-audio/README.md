# Low-latency RX audio (WebSocket Listen path)

Optional, opt-in alternative to the default WebM/MSE Listen pipeline. The
default path mixes into a WebM container, batches into ~200ms clusters, and
streams over chunked HTTP with an AGC (`dynaudnorm`) filter that adds ~0.4s
of lookahead by itself — a steady-state RX latency floor of roughly
1.3-1.4s, measured end to end. This path instead streams raw Opus packets
over a plain WebSocket to `WebCodecs`/`AudioWorklet` in the browser, no
container, no AGC lookahead — trading the default path's loudness
consistency and jitter tolerance for substantially lower latency.

Two things have to both be true for the low-latency path to actually serve
a listener:

1. **`rx_audio_config.path` is set to `lowlatency`** — a Manager-level,
   owner-only setting (Manager > Audio, near TX Diagnostics), not a
   per-browser preference, since it changes which capture/encode pipeline
   runs server-side for every listener on that node. `recording.py` and
   `stream_relay.py` are unaffected by this setting either way — both keep
   using the default WebM `_AudioBroadcast` pipeline exactly as before,
   regardless of what Listen is doing.
2. **This script has been applied** — `audio_ws_relay.py` (the process that
   actually does the low-latency encoding and serves the WebSocket) is
   always running once HenWen starts, at near-zero idle cost, whether or
   not this script has ever been run. What this script adds is the
   *network path to it*: an Apache `ProxyPass` so a browser outside this
   box can actually reach its WebSocket listener, mirroring exactly how
   `tx-spike/apply.sh` proxies `/asterisk-ws` for browser TX.

## What it does

`apply.sh` adds one Apache `ProxyPass /ws-audio ws://127.0.0.1:8098/` line
to whichever HenWen vhost is present — `henwen-ssl.conf` if
`tx-spike/setup-https.sh` has been run, else `henwen.conf` for a plain-HTTP
install. Unlike browser TX, this feature does **not** require HTTPS: Listen
already works over plain HTTP today (there's no `getUserMedia`-style
secure-context requirement here), so a LAN-only install with Apache
fronting HenWen over plain HTTP can use this too. An install with no Apache
in front of HenWen at all (gunicorn reachable directly on the LAN) isn't
supported by this script — same limitation `tx-spike/apply.sh` already has.

It also ensures Apache's `proxy_wstunnel` module is enabled (usually already
true if browser TX's `/asterisk-ws` proxy has been set up, since that needs
the same module).

## Applying

```
sudo bash ws-audio/apply.sh
```

Idempotent and marker-guarded (safe to re-run), backs up the vhost file
first, and does **not** restart HenWen or Asterisk — `audio_ws_relay.py` is
already running regardless; this just makes it reachable.

After running this, switch the RX Audio Path to "Low-Latency" from
Manager > Audio to actually start using it for Listen.

## Rollback

```
sudo bash ws-audio/rollback.sh
```

Restores the backed-up vhost file (most recent run by default, or pass a
backup dir). Doesn't stop `audio_ws_relay.py` — app.py keeps
spawning/supervising that process regardless, since it costs nothing idle.
If `rx_audio_config.path` was set to `lowlatency`, switch it back to
`legacy` in Manager separately — this script only removes the network path,
not the setting.

## Files

- `apply.sh` / `rollback.sh` — see above.
