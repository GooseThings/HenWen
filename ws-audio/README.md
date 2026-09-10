# Low-latency RX audio (WebSocket Listen path)

Alternative to the legacy WebM/MSE Listen pipeline. The
default path mixes into a WebM container, batches into ~200ms clusters, and
streams over chunked HTTP with an AGC (`dynaudnorm`) filter that adds ~0.4s
of lookahead by itself — a steady-state RX latency floor of roughly
1.3-1.4s, measured end to end. This path instead streams raw Opus packets
over a plain WebSocket to `WebCodecs`/`AudioWorklet` in the browser, no
container, no AGC lookahead — trading the default path's loudness
consistency and jitter tolerance for substantially lower latency.

Three things have to all be true for the low-latency path to actually serve
a listener:

1. **The page is loaded over HTTPS (or from `localhost`).** The browser side
   decodes audio with WebCodecs (`AudioDecoder`), which browsers only expose
   in a secure context. On a plain-HTTP install, `AudioDecoder` is simply
   undefined for any real visitor, so `status.html`'s own capability check
   always falls through to the legacy MSE pipeline no matter what
   `rx_audio_config.path` says. This is a real, unavoidable browser
   restriction, not something this project can route around. See
   `tx-spike/setup-https.sh` for the same HTTPS setup browser TX already
   needs, since it satisfies this requirement too.
2. **`rx_audio_config.path` is set to `lowlatency`.** This is a Manager-level,
   owner-only setting (Manager > Audio, near TX Diagnostics), not a
   per-browser preference. It changes which capture/encode pipeline runs
   server-side for every listener on that node. `recording.py` and
   `stream_relay.py` are unaffected by this setting either way. Both keep
   using the default WebM `_AudioBroadcast` pipeline exactly as before,
   regardless of what Listen is doing.
3. **This script has been applied.** `audio_ws_relay.py` (the process that
   actually does the low-latency encoding and serves the WebSocket) is
   always running once HenWen starts, at near-zero idle cost, whether or
   not this script has ever been run. What this script adds is the
   *network path to it*: an Apache `ProxyPass` so a browser outside this
   box can actually reach its WebSocket listener. This mirrors exactly how
   `tx-spike/apply.sh` proxies `/asterisk-ws` for browser TX.

## What it does

`apply.sh` adds one Apache `ProxyPass /ws-audio ws://127.0.0.1:8098/` line
to whichever HenWen vhost is present: `henwen-ssl.conf` if
`tx-spike/setup-https.sh` has been run, else `henwen.conf` for a plain-HTTP
install. This script itself works fine against a plain-HTTP vhost. What
needs HTTPS is the feature as a whole, per requirement 1 above, since the
browser's `AudioDecoder` won't exist without it. Wiring the proxy on a
plain-HTTP install still isn't wasted: it's ready the moment HTTPS gets
added later, and `install.sh` does exactly that (see below).

`install.sh` provisions a plain-HTTP vhost automatically on every fresh
install (via `tx-spike/setup-https.sh --http-only`, unless the owner opts
into full HTTPS during install instead) and runs `apply.sh` right after, so
the network path is ready by default either way. An install with no Apache
in front of HenWen at all (gunicorn reachable directly on the LAN) isn't
supported by this script, the same limitation `tx-spike/apply.sh` already
has, but that's now only the case if `install.sh`'s own Apache step failed
or was declined, or on an install that predates this change.

It also ensures Apache's `proxy_wstunnel` module is enabled (usually already
true if browser TX's `/asterisk-ws` proxy has been set up, since that needs
the same module).

## Applying

`install.sh` already runs this automatically on every fresh install, right
after provisioning an Apache vhost (see above). It also seeds
`rx_audio_config.path` to `lowlatency`, but only when the install got HTTPS
set up too, since that's a hard requirement for the feature to work at all
(see requirement 1 above). A plain-HTTP install still gets this script
applied so the network path is ready, but keeps `rx_audio_config.path` at
`legacy` since flipping it would be a no-op label with no real effect.
Only run this by hand on an install that predates this change, if the
automatic step failed or was skipped, or to re-apply after a rollback:

```
sudo bash ws-audio/apply.sh
```

Idempotent and marker-guarded (safe to re-run), backs up the vhost file
first, and does **not** restart HenWen or Asterisk — `audio_ws_relay.py` is
already running regardless; this just makes it reachable.

If `rx_audio_config.path` wasn't already seeded to `lowlatency`, switch the
RX Audio Path to "Low-Latency" from Manager > Audio to start using it for
Listen. Make sure HTTPS is set up first (`tx-spike/setup-https.sh`), since
otherwise the setting will show as Low-Latency in the Manager UI but every
real browser will keep silently using the legacy pipeline anyway.

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
