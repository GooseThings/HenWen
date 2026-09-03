# AudioSocket tap (low-latency Listen audio capture)

Optional, opt-in replacement for how HenWen captures a node's live RX audio
for the Status Board's Listen feature. Without this, Listen uses AMI
`MixMonitor` on the node's channel, writing to a FIFO through Asterisk's own
buffered stdio stream — that buffer flushes in ~32KB/~2s lumps, adding ~2s of
latency before any of HenWen's own pacing/encoding even starts (see
CLAUDE.md's "Audio streaming" section and issue #30). This instead uses
Asterisk's `AudioSocket` app to stream each 20ms frame over a plain TCP
socket as it's produced, with no such buffering.

## What it does

`apply.sh` installs:

- A dialplan context (`henwen-audiosocket-tap` in
  `/etc/asterisk/custom/extensions.conf`) that runs `AudioSocket()`,
  connecting out to a TCP server `audio_relay.py` starts on demand.
- Three Asterisk modules loaded live and persisted in `modules.conf`:
  `res_audiosocket.so`, `app_audiosocket.so` (load order matters — the app
  resolves a symbol from the res_ module at load time), and `app_chanspy.so`
  (`ChanSpy(<channel>,q)` is what actually captures the node channel's
  audio onto the tap leg — `q` suppresses the interactive tone/prompt
  behavior. Deliberately not `o`: that flag restricts capture to one
  direction of the target's own audio, which on an app_rpt channel (no
  conventional Asterisk Bridge — linked-node audio is composited and
  written directly to the channel by app_rpt itself) silently drops
  linked-node traffic, leaving only the local hardware receiver's own
  input. This is still strictly listen-only either way — whisper/barge
  require `w`/`W`/`B`, none of which are used — so it has zero effect on
  the node's own `Rpt()` execution).

`app.py`'s `_try_audiosocket_tap()` (see `_start_broadcast()` in app.py) then
originates a Local-channel bridge — one half runs `ChanSpy` on the node's
channel, the other runs the `AudioSocket()` context above — pointed at
`audio_relay.py`'s listener, each time Listen starts. **Purely additive**:
if this isn't installed (the default, every existing install today), or
anything about the handshake fails, HenWen falls straight back to the
existing MixMonitor path automatically — Listen keeps working either way.

## Applying

```
sudo bash audiosocket-tap/apply.sh
```

Idempotent and marker-guarded (safe to re-run), backs up every file it
touches first, and does **not** restart Asterisk — modules load live and
`app_rpt`/any in-progress Listen session keeps running throughout.

## Rollback

```
sudo bash audiosocket-tap/rollback.sh
```

Restores the backed-up files (most recent run by default, or pass a backup
dir). Already-loaded modules stay resident until Asterisk's next natural
restart — harmless, since nothing references them once the dialplan context
is gone. HenWen falls back to MixMonitor immediately on its own; no restart
needed there either.

## Files

- `modules.snippet` — the three `load = ...` lines appended to
  `/etc/asterisk/modules.conf`.
- `extensions-custom.snippet` — the `henwen-audiosocket-tap` dialplan
  context appended to `/etc/asterisk/custom/extensions.conf`.
- `apply.sh` / `rollback.sh` — see above.
