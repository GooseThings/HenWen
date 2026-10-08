// audio-worklet-lowlatency.js
//
// AudioWorkletProcessor backing the low-latency ("lowlatency") RX audio
// path's playback -- runs on the browser's dedicated real-time audio
// rendering thread, decoupled from the main thread's own event loop (the
// entire reason this path exists: an <audio> element's/MediaSource's
// playback pipeline has more inherent latency than this lower-level API).
//
// Originally adapted from the jitter-buffer design in a different local
// project (rigcontrolweb's public/audio-processor.js, studied as a reference
// for a low-latency RX architecture). Its drop-oldest-on-overflow philosophy
// is no longer followed literally -- see "Catching up" below.
//
// Fed by status.html's _doStartListenWS(): each WebCodecs AudioDecoder
// output callback posts one decoded Float32Array of PCM here via
// port.postMessage({type: 'pcm', pcm: <Float32Array>}, [<transferred
// buffer>]). This processor also answers {type: 'stats-request'} with a
// {type: 'stats', bufferedSamples, underruns, overflows, resyncs, rate,
// targetMs} reply, polled every 10s by the main thread for the
// [AUDIO-CLIENT] telemetry report (mirrors the MSE path's own periodic
// report, so both paths' health is comparable side by side in the server
// log).
//
// ---------------------------------------------------------------------------
// Why this buffer is adaptive (2026-10-07)
// ---------------------------------------------------------------------------
// This buffer originally ran a *fixed* 120ms pre-roll / 350ms hard cap, and
// shed latency by clamping occupancy to that cap on the spot. That is
// smaller than the delivery jitter this install actually sees, which made
// Listen audibly gap, drop, and pick back up again. Measured from a week of
// this path's own [AUDIO-CLIENT] telemetry-ws lines (per-10s-window maximum
// WS frame arrival gap, two independent remote client networks, 1350
// samples): median 300ms, p90 723ms, p99 1908ms, max 9136ms -- against a
// 350ms cap. So roughly half of all windows contained a gap at or over the
// cap, and the floor (120ms) was itself well under the median gap. The
// failure loop that produces:
//
//   * a gap longer than the cushion drains the buffer -> underrun -> the
//     processor stops and waits for a full pre-roll to refill, so every
//     underrun costs at least a pre-roll of hard silence   ("gap")
//   * the catch-up burst that follows immediately exceeds the cap, so the
//     oldest samples are discarded -- with a 350ms cap a 1900ms burst threw
//     away ~1500ms of speech in one splice                 ("drop")
//   * it refills and resumes                               ("picks up again")
//
// Server-side was ruled out before changing anything here: the relay sends
// one WS frame per 20ms with TCP_NODELAY set on every client socket
// (audio_ws_relay.py), the box was at load 0.36 across 6 cores, and the
// arrival rate measured at the client was a correct ~50 frames/sec with no
// shortfall -- the frames all arrive, just bunched rather than paced.
//
// The legacy WebM/MSE path had this same bug and already fixed it the same
// way; its comment in status.html describes this path's symptom exactly:
// "some remote networks (traffic shapers, TLS-inspecting middleboxes)
// deliver the stream in multi-second bursts rather than the steady 200 ms
// cadence the server emits. A fixed 0.5 s cushion underruns on every burst
// gap -- audibly choppy. So the cushion grows each time playback actually
// stalls and decays back toward the baseline only after a sustained
// stall-free stretch." That path grows its cushion to a 4.0s ceiling
// (_LISTEN_TARGET_MAX_S); this one keeps a much lower ceiling, since trading
// away latency is the one thing this path exists not to do -- a listener who
// wants an unconditionally smooth stream over a badly-shaped link is better
// served by the legacy path.
//
// So the cushion is adaptive, on the same grow-on-stall / decay-when-clean
// shape as that path:
//   * TARGET_MIN_MS is the floor, and what a clean link settles back to.
//   * each underrun adds TARGET_STEP_MS, up to TARGET_MAX_MS.
//   * after DECAY_AFTER_MS with no underrun, shave DECAY_STEP_MS, at most
//     once per DECAY_TICK_MS. The tick rate-limit is load-bearing: decaying
//     once per process() call instead would shave the cushion 375 times a
//     second and collapse it to the floor within ~60ms of becoming
//     eligible, which is indistinguishable from never having adapted.
//
// ---------------------------------------------------------------------------
// Catching up: play slightly fast, don't throw audio away
// ---------------------------------------------------------------------------
// Once a burst has inflated the buffer, that latency has to come back off
// somehow, and the choice of mechanism is the difference between an
// inaudible adjustment and a lost word. Discarding samples -- what the
// original hard clamp did, and what a fixed-size periodic trim would also do
// -- loses speech, and a big burst loses a lot of it at once.
//
// Instead the read pointer advances at a slightly faster-than-real-time rate
// while the buffer is long, resampling on the way out (linear interpolation
// over the ring). No audio is discarded at all; it is played marginally
// early until the backlog is gone. This mirrors the MSE path's own
// playbackRate controller, including its banded shape and wide dead-band,
// with one difference worth stating plainly: that path gets the browser's
// pitch-preserving WSOLA for free, whereas resampling here does shift pitch
// by the rate factor. That caps the usable rates much lower -- RATE_MAX is
// 1.08 (an ~8% shift, slight and brief on voice) where the MSE path happily
// uses 1.25.
//
// Because the rates are capped low, catch-up is bounded at ~80ms of latency
// shed per second, which is not enough for a pathological multi-second
// burst (the measured max gap was 9136ms -- nearly two minutes of catch-up
// at that rate, by which time more bursts have landed). So the third tier
// mirrors the MSE path's last resort too: past RESYNC_MS of excess, give up
// on catching up gracefully and discard down to the target in one declicked
// splice, cooldown-gated so a buffer hovering near the threshold can't
// cause repeated splices. That is the only path that still loses audio, it
// is what `resyncs` counts, and on the measured distribution it is rare
// rather than routine.
//
// Splices -- resuming after an underrun, and a resync discard -- are sample
// discontinuities, audible as a click/pop independently of the dropout
// itself. Each gets a short linear ramp, the same declick treatment and for
// the same reason as audio_relay.py's own _fade_frame() on its
// real<->silence and overflow-drop splices.
//
// ---------------------------------------------------------------------------
// Backing store
// ---------------------------------------------------------------------------
// A fixed-size ring buffer, replacing the original allocate-and-concatenate-
// per-message. That pattern rebuilt the entire buffer on every inbound 20ms
// frame -- at 48kHz a full buffer is ~67KB, so 50 frames/sec meant several
// MB/s of allocation and copying *on the real-time audio rendering thread*,
// whose GC pauses show up as exactly the underruns this file exists to
// avoid. The ring is sized once for the worst case the adaptation can reach
// and steady-state playback allocates nothing at all.

const TARGET_MIN_MS      = 200;   // floor / clean-link cushion
const TARGET_MAX_MS      = 1500;  // adaptive ceiling (cf. MSE path's 4000ms)
const TARGET_STEP_MS     = 400;   // cushion added per underrun
const DECAY_AFTER_MS     = 60000; // underrun-free time before shaving the cushion
const DECAY_STEP_MS      = 50;    // shaved per decay step
const DECAY_TICK_MS      = 2000;  // minimum interval between decay steps

/* Catch-up rate bands, keyed on how far above target the buffer is sitting.
   Rates stay low because resampling shifts pitch (see note above); the
   dead-band is wide so ordinary jitter never engages catch-up at all. */
const RATE_DEADBAND_MS   = 150;   // excess below this plays at exactly 1.0
const RATE_BANDS = [
  { overMs: 150,  rate: 1.02 },
  { overMs: 500,  rate: 1.04 },
  { overMs: 1000, rate: 1.08 },
];
const RESYNC_MS          = 2500;  // excess past this discards down to target
const RESYNC_COOLDOWN_MS = 5000;  // minimum interval between resync splices

const HARD_CAP_EXTRA_MS  = 4000;  // ring headroom above the cushion ceiling
const DECLICK_MS         = 5;     // linear ramp over a splice (matches audio_relay.py)

class LowLatencyPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    // sampleRate here is a global AudioWorkletGlobalScope binding equal to
    // the owning AudioContext's rate -- _doStartListenWS() constructs that
    // context with sampleRate: 48000 to match WebCodecs' Opus decoder
    // output exactly, so these both work out to the same 48000 in
    // practice; computed from `sampleRate` rather than hardcoded so this
    // stays correct if that ever changes on either side.
    const toSamples = (ms) => Math.max(1, Math.round(sampleRate * ms / 1000));
    this._toSamples = toSamples;

    this.TARGET_MIN  = toSamples(TARGET_MIN_MS);
    this.TARGET_MAX  = toSamples(TARGET_MAX_MS);
    this.TARGET_STEP = toSamples(TARGET_STEP_MS);
    this.DECAY_STEP  = toSamples(DECAY_STEP_MS);
    this.DEADBAND    = toSamples(RATE_DEADBAND_MS);
    this.RESYNC      = toSamples(RESYNC_MS);
    this.DECLICK     = toSamples(DECLICK_MS);
    this._rateBands  = RATE_BANDS.map((b) => ({
      over: toSamples(b.overMs), rate: b.rate,
    })).sort((a, b) => b.over - a.over);   // highest threshold first

    // Current adaptive cushion: both the pre-roll before playback starts or
    // resumes, and the occupancy playback tries to ride at.
    this.target = this.TARGET_MIN;

    // Ring sized for the worst case the adaptation can reach, so the
    // backing store is allocated exactly once.
    this.capacity = this.TARGET_MAX + toSamples(HARD_CAP_EXTRA_MS) + toSamples(100);
    this.ring = new Float32Array(this.capacity);

    // readPos is fractional because the catch-up read resamples; writeIdx is
    // a plain integer since writes are always whole frames. `available` is
    // the buffered sample count and tracks both (fractional for the same
    // reason readPos is).
    this.readPos = 0;
    this.writeIdx = 0;
    this.available = 0;

    this.isPlaying = false;
    this._underruns = 0;
    this._overflows = 0;
    this._resyncs = 0;
    this._rate = 1.0;

    // currentTime is an AudioWorkletGlobalScope global (seconds, on the
    // audio clock) -- used rather than Date.now() so these timers run on
    // the same clock as playback itself.
    this._lastUnderrunAt = currentTime;
    this._lastDecayAt = currentTime;
    this._lastResyncAt = -Infinity;

    // Samples of declick ramp still owed on the output, set at a splice.
    this._rampRemaining = 0;
    this._rampTotal = this.DECLICK;

    this.port.onmessage = (e) => {
      const msg = e.data;
      if (msg.type === 'pcm') {
        this._write(msg.pcm);
      } else if (msg.type === 'stats-request') {
        this.port.postMessage({
          type: 'stats',
          bufferedSamples: Math.round(this.available),
          underruns: this._underruns,
          overflows: this._overflows,
          resyncs: this._resyncs,
          rate: this._rate,
          targetMs: this.target / sampleRate * 1000,
        });
      }
    };
  }

  /** Discards `n` oldest buffered samples, declicking the resulting splice. */
  _dropOldest(n) {
    const drop = Math.min(n, this.available);
    if (drop <= 0) return;
    this.readPos = (this.readPos + drop) % this.capacity;
    this.available -= drop;
    this._armDeclick();
  }

  /** Appends decoded PCM, enforcing only the ring's physical ceiling. */
  _write(data) {
    const cap = this.capacity;
    let n = data.length;
    if (n <= 0) return;

    // A single posted frame larger than the whole ring can only mean a
    // pathological decoder output; keep its newest tail rather than
    // overrunning the write.
    if (n > cap) {
      data = data.subarray(n - cap);
      n = cap;
    }

    // Physical ceiling only. Routine burst absorption is the whole point of
    // the headroom above the cushion, and shedding it is process()'s job
    // (rate catch-up, or a resync past RESYNC_MS) -- so reaching this means
    // the ring itself filled, which the sizing is meant to make unreachable.
    if (this.available + n > cap) {
      this._dropOldest(this.available + n - cap);
      this._overflows++;
    }

    const w = this.writeIdx;
    const first = Math.min(n, cap - w);
    this.ring.set(data.subarray(0, first), w);
    if (n > first) this.ring.set(data.subarray(first), 0);
    this.writeIdx = (w + n) % cap;
    this.available += n;

    if (!this.isPlaying && this.available >= this.target) {
      this.isPlaying = true;
      this._armDeclick();
    }
  }

  /** Reads `need` output samples, advancing the read pointer by `rate` per sample. */
  _readResampled(out, need, rate) {
    const cap = this.capacity;
    const ring = this.ring;
    let pos = this.readPos;
    for (let i = 0; i < need; i++) {
      const i0 = pos | 0;
      const frac = pos - i0;
      const a = ring[i0];
      const b = ring[i0 + 1 === cap ? 0 : i0 + 1];
      out[i] = a + (b - a) * frac;
      pos += rate;
      if (pos >= cap) pos -= cap;
    }
    this.readPos = pos;
    this.available -= need * rate;
    if (this.available < 0) this.available = 0;
  }

  /** Schedules a short linear fade-in over an upcoming sample discontinuity. */
  _armDeclick() {
    this._rampRemaining = this.DECLICK;
    this._rampTotal = this.DECLICK;
  }

  /** Grows the cushion after a stall. */
  _adaptAfterUnderrun() {
    this._underruns++;
    this.target = Math.min(this.TARGET_MAX, this.target + this.TARGET_STEP);
    this._lastUnderrunAt = currentTime;
    this._lastDecayAt = currentTime;
  }

  /** Shaves the cushion back toward the floor after a sustained clean stretch. */
  _maybeDecay() {
    if (this.target <= this.TARGET_MIN) return;
    const now = currentTime;
    if ((now - this._lastUnderrunAt) * 1000 < DECAY_AFTER_MS) return;
    if ((now - this._lastDecayAt) * 1000 < DECAY_TICK_MS) return;
    this.target = Math.max(this.TARGET_MIN, this.target - this.DECAY_STEP);
    this._lastDecayAt = now;
  }

  /** Picks this block's playback rate from how far above target the buffer is. */
  _chooseRate() {
    const excess = this.available - this.target;
    if (excess <= this.DEADBAND) return 1.0;
    for (const band of this._rateBands) {
      if (excess > band.over) return band.rate;
    }
    return 1.0;
  }

  /** Last resort for an excess too large to play off at the capped rates. */
  _maybeResync() {
    if (this.available - this.target <= this.RESYNC) return;
    const now = currentTime;
    if ((now - this._lastResyncAt) * 1000 < RESYNC_COOLDOWN_MS) return;
    this._dropOldest(this.available - this.target);
    this._lastResyncAt = now;
    this._resyncs++;
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const channel = output && output[0];
    if (!channel) return true;

    const need = channel.length;

    this._maybeResync();

    const rate = this._chooseRate();
    // Interpolation reads one sample past the final position, so require a
    // little more than the nominal consumption before committing to a read.
    const required = need * rate + 2;

    if (this.isPlaying && this.available >= required) {
      this._rate = rate;
      this._readResampled(channel, need, rate);
      if (this._rampRemaining > 0) {
        // Linear fade-in across the splice, carried over successive blocks
        // if the ramp is longer than one render quantum.
        const total = this._rampTotal;
        for (let i = 0; i < need && this._rampRemaining > 0; i++, this._rampRemaining--) {
          channel[i] *= (total - this._rampRemaining) / total;
        }
      }
      this._maybeDecay();
    } else {
      // Underflow: pause and wait for a full cushion to refill rather than
      // playing back whatever partial data exists -- a hard silence gap
      // here is preferable to a discontinuity mid-buffer. Only counts as an
      // underrun (and only grows the cushion) on the transition out of
      // playing, not for every silent block while it waits to refill.
      if (this.isPlaying) this._adaptAfterUnderrun();
      this.isPlaying = false;
      this._rate = 1.0;
      channel.fill(0);
    }

    return true;  // keep this node alive for the life of the stream
  }
}

registerProcessor('henwen-lowlatency-playback', LowLatencyPlaybackProcessor);
