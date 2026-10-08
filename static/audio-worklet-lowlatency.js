// audio-worklet-lowlatency.js
//
// AudioWorkletProcessor backing the low-latency ("lowlatency") RX audio
// path's playback -- runs on the browser's dedicated real-time audio
// rendering thread, decoupled from the main thread's own event loop (the
// entire reason this path exists: an <audio> element's/MediaSource's
// playback pipeline has more inherent latency than this lower-level API).
//
// Fed by status.html's _doStartListenWS(): each WebCodecs AudioDecoder
// output callback posts one decoded Float32Array of PCM here via
// port.postMessage({type: 'pcm', pcm: <Float32Array>}, [<transferred
// buffer>]). Answers {type: 'stats-request'} with a {type: 'stats', ...}
// reply, polled every 10s by the main thread for the [AUDIO-CLIENT]
// telemetry report.
//
// ===========================================================================
// Design goal: intelligibility first, and never alter the audio
// ===========================================================================
// Playback here is ALWAYS at exactly 1.0x. Audio reaches the output
// sample-for-sample as it was decoded -- no resampling, no time compression,
// no frame dropping. That is a deliberate constraint, not an omission, and
// the reasons are worth keeping, because each alternative was built,
// measured against a real repeater recording, and rejected:
//
//   * **Linear resampling** (an earlier version of this file, capped at
//     1.08x) shifts pitch and every formant by the playback rate: measured
//     +12% spectral centroid at 1.08x, +107% at 2.0x. Audibly "sped up".
//   * **Dropping whole frames** shifts the spectrum up sharply *and* injects
//     strong periodic amplitude modulation -- the robotic/warbling artifact.
//     Measured +67% centroid and +5.7 dB modulation at 1-of-2, and
//     counterintuitively *worse* modulation when gentler (+28.5 dB at
//     1-of-8, whose 6.25Hz repetition sits right where the ear hears
//     flutter). "Only drop a few" is not the safe version of the idea.
//   * **SOLA / overlap-add time compression** genuinely does preserve pitch;
//     that was verified through this very processor (centroid shift -0.2%
//     to -1.9% with catch-up engaged). It was still removed, and the reason
//     matters more than the measurement: a spectral centroid captures tonal
//     balance and says nothing about SOLA's own artifacts, which at
//     aggressive rates are phoneme stuttering and transient smearing.
//     Reported from real listening as sounding sped up and hard to follow.
//     The goal was intelligibility, the metric used to justify the mechanism
//     did not measure intelligibility, and the listening test is the
//     authority. Do not reintroduce a catch-up rate on the strength of a
//     spectral metric alone.
//
// So latency is never recovered by altering playback. It is recovered the
// one way that costs nothing audible: by shortening silence.
//
// ===========================================================================
// How latency is managed without touching the audio
// ===========================================================================
// Buffer occupancy *is* latency, so riding out a delivery stall of N ms
// requires N ms of buffered audio. Two mechanisms, and that is all:
//
//   1. **Adaptive cushion.** The target grows each time playback actually
//      underruns, up to TARGET_MAX_MS, and decays back toward TARGET_MIN_MS
//      after a sustained stall-free stretch. A link that behaves settles at
//      the floor; a link that does not buys continuity with latency. Same
//      grow-on-stall/decay-when-clean shape the legacy WebM/MSE path uses
//      (see _listenTargetS in status.html).
//
//      Adaptive rather than a constant tuned to one site, on purpose: this
//      is shipped software, and every install has a different radio, uplink
//      and set of listeners. A constant fitted to one network's measured
//      jitter would be wrong everywhere else, whereas a control loop
//      converges on whatever link it actually finds. For the same reason
//      nothing here assumes a particular network topology, and no
//      environment-specific workaround belongs in this file.
//
//   2. **Dead-air shedding.** A repeater feed is mostly silence, and
//      audio_relay.py injects silence whenever the node is quiet, so the
//      backlog a stall leaves behind very often *is* dead air. Discarding
//      that is free -- nobody can hear a shortened pause between
//      transmissions. The silence test reuses this project's own established
//      threshold, recording_config.silence_rms_thresh (default 300 on the
//      int16 scale) from recording.py's SilenceGate, rather than inventing a
//      second notion of "quiet" for the same audio, applied as a
//      conservative peak bound that errs toward keeping audio.
//
//      It only skips quiet sitting at the playback point, which gives the
//      mechanism a useful shape on a repeater: latency accumulated during a
//      transmission is carried to the end of that transmission -- so the
//      transmission itself is heard complete and unaltered -- and then
//      collapses during the pause that follows. A generous cushion ceiling
//      therefore does not mean permanently high latency; every gap in
//      traffic resets it toward the floor.
//
// A hard ceiling (HARD_MAX_MS) still discards as an absolute last resort,
// declicked, for a stall too large for the cushion to cover. That is the
// only path that can cut speech, and `resyncs` counts it.
//
// Splices -- resuming after an underrun, a ceiling discard -- are sample
// discontinuities, audible as a click independently of the dropout itself.
// Each gets a short linear ramp, the same declick treatment and for the same
// reason as audio_relay.py's own _fade_frame().
//
// Note before reaching for a per-packet fix: this path is Opus over a
// WebSocket, i.e. TCP. Delivery is in-order and lossless by construction --
// there are no late or out-of-order packets to discard. A stall is TCP
// head-of-line blocking, after which the whole backlog arrives at once.
//
// ===========================================================================
// Backing store
// ===========================================================================
// A single fixed-size ring. The original implementation allocated and
// concatenated a whole new Float32Array per inbound 20ms frame -- ~67KB
// rebuilt 50 times a second on the real-time audio thread, whose GC pauses
// show up as exactly the underruns this file exists to avoid. Steady-state
// playback here allocates nothing and copies each sample exactly once.

/* Tuned by sweeping against this install's real measured arrival-gap
   distribution with a repeater traffic model (600s x 3 seeds), scoring
   audible damage only -- a gap or a discard inside dead air is inaudible,
   so only gaps landing inside speech count:
       step  decay   latency   gap-in-speech   underruns
        400    90s     894ms       3472ms          11
        400    45s     698ms       3729ms          16   <-- shipped
        600    90s     950ms       3762ms           9
        600    45s     745ms       4220ms          13
        250    20s     463ms       4597ms          25
   Shipped the middle: a 7% gap difference is not worth 28% more latency.
   Raise DECAY_AFTER_MS to 90000 to buy the remaining gaps with latency.
   TARGET_MAX_MS above ~2500 changes nothing -- the cushion never gets
   there on this distribution -- so it is a safety ceiling, not a tuning
   knob. Shedding dead air down to the *floor* instead of to the grown
   target was also tried and is clearly worse (5191-5908ms gaps): it throws
   away the cushion during the quiet before a transmission, which is exactly
   when it is about to be needed. */
const TARGET_MIN_MS    = 200;   // floor: where a well-behaved link settles
const TARGET_MAX_MS    = 2500;  // ceiling (cf. the MSE path's own 4000ms)
const TARGET_STEP_MS   = 400;   // cushion added per underrun
const RESUME_FRAC      = 0.6;   // fraction of target refilled before resuming
const DECAY_AFTER_MS   = 45000; // underrun-free time before shaving the cushion
const DECAY_STEP_MS    = 100;   // shaved per decay step
const DECAY_TICK_MS    = 1000;  // minimum interval between decay steps

const SHED_SLACK_MS    = 60;    // dead-band: don't shed for trivial excess
const SKIP_CHUNK_MS    = 3.3;   // granularity of the dead-air scan
const SKIP_MAX_MS      = 30;    // most dead air discardable per render block
/* recording.py's SilenceGate default, 300 on the int16 scale, expressed for
   the float samples WebCodecs hands us. Same audio, same notion of quiet. */
const SILENCE_PEAK     = 300 / 32768;

const HARD_MAX_MS      = 4000;  // absolute ceiling; past this, discard
const RING_EXTRA_MS    = 1000;  // headroom so a burst lands rather than wrapping
const DECLICK_MS       = 5;     // linear ramp over a splice (matches audio_relay.py)

class LowLatencyPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    // sampleRate is an AudioWorkletGlobalScope global equal to the owning
    // AudioContext's rate -- _doStartListenWS() builds that context with
    // sampleRate: 48000 to match WebCodecs' Opus output exactly, so these
    // work out to 48000 in practice; derived from `sampleRate` rather than
    // hardcoded so this stays correct if that ever changes on either side.
    const toSamples = (ms) => Math.max(1, Math.round(sampleRate * ms / 1000));

    this.TARGET_MIN  = toSamples(TARGET_MIN_MS);
    this.TARGET_MAX  = toSamples(TARGET_MAX_MS);
    this.TARGET_STEP = toSamples(TARGET_STEP_MS);
    this.DECAY_STEP  = toSamples(DECAY_STEP_MS);
    this.SHED_SLACK  = toSamples(SHED_SLACK_MS);
    this.SKIP_CHUNK  = toSamples(SKIP_CHUNK_MS);
    this.SKIP_MAX    = toSamples(SKIP_MAX_MS);
    this.HARD_MAX    = toSamples(HARD_MAX_MS);
    this.DECLICK     = toSamples(DECLICK_MS);

    this.target = this.TARGET_MIN;

    this.capacity = this.HARD_MAX + toSamples(RING_EXTRA_MS);
    this.ring = new Float32Array(this.capacity);
    this.readIdx = 0;
    this.writeIdx = 0;
    this.available = 0;

    this.isPlaying = false;
    this._underruns = 0;
    this._resyncs = 0;
    this._silenceSkipped = 0;

    // currentTime is an AudioWorkletGlobalScope global (seconds, on the
    // audio clock) -- used rather than Date.now() so these timers run on the
    // same clock as playback itself.
    this._lastUnderrunAt = currentTime;
    this._lastDecayAt = currentTime;

    this._rampRemaining = 0;

    this.port.onmessage = (e) => {
      const msg = e.data;
      if (msg.type === 'pcm') {
        this._write(msg.pcm);
      } else if (msg.type === 'stats-request') {
        this.port.postMessage({
          type: 'stats',
          bufferedSamples: this.available,
          underruns: this._underruns,
          resyncs: this._resyncs,
          silenceSkipMs: Math.round(this._silenceSkipped / sampleRate * 1000),
          // Always exactly 1. Reported anyway so the telemetry line keeps a
          // stable shape, and so any future change that reintroduced rate
          // alteration would show up in the logs rather than silently.
          rate: 1,
          targetMs: this.target / sampleRate * 1000,
        });
      }
    };
  }

  _armDeclick() {
    this._rampRemaining = this.DECLICK;
  }

  _dropOldest(n) {
    const drop = Math.min(n, this.available);
    if (drop <= 0) return;
    this.readIdx = (this.readIdx + drop) % this.capacity;
    this.available -= drop;
    this._armDeclick();
  }

  /** Enforces the absolute ceiling as ONE deliberate, declicked discard.
   *
   *  Called from both _write() and process(), because a burst can deliver
   *  seconds of audio across many postMessage calls with no process() in
   *  between -- the main thread drains the socket in one task, so every
   *  queued frame lands before the audio thread next runs. Leaving this to
   *  process() alone let the ring itself fill, and the physical-wrap guard
   *  then discarded in one-frame increments: hundreds of separate splices
   *  (and declick ramps) for a single large burst, which both inflated the
   *  resync count and sounded far worse than one clean cut. */
  _enforceCeiling() {
    if (this.available <= this.HARD_MAX) return;
    this._dropOldest(this.available - this.target);
    this._resyncs++;
  }

  _write(data) {
    const cap = this.capacity;
    let n = data.length;
    if (n <= 0) return;
    if (n > cap) { data = data.subarray(n - cap); n = cap; }
    // Physical wrap guard only; _enforceCeiling() is what deliberately
    // bounds latency, and the ring is sized so this cannot normally be hit.
    if (this.available + n > cap) this._dropOldest(this.available + n - cap);

    const w = this.writeIdx;
    const first = Math.min(n, cap - w);
    this.ring.set(data.subarray(0, first), w);
    if (n > first) this.ring.set(data.subarray(first), 0);
    this.writeIdx = (w + n) % cap;
    this.available += n;

    this._enforceCeiling();

    if (!this.isPlaying && this.available >= this.target * RESUME_FRAC) {
      this.isPlaying = true;
      this._armDeclick();
    }
  }

  /**
   * The only latency-recovery mechanism: advance the read pointer past
   * leading dead air, up to `maxSkip` samples.
   *
   * Scans in SKIP_CHUNK blocks and stops at the first chunk holding any
   * sample above SILENCE_PEAK, so speech is never skipped -- only quiet
   * sitting at the playback point.
   */
  _skipDeadAir(maxSkip) {
    const chunk = this.SKIP_CHUNK;
    const cap = this.capacity;
    const ring = this.ring;
    let pos = this.readIdx;
    let skipped = 0;

    while (skipped + chunk <= maxSkip && skipped + chunk <= this.available) {
      let loud = false;
      for (let i = 0; i < chunk; i++) {
        let k = pos + i;
        if (k >= cap) k -= cap;
        const v = ring[k];
        if (v > SILENCE_PEAK || v < -SILENCE_PEAK) { loud = true; break; }
      }
      if (loud) break;
      pos = (pos + chunk) % cap;
      skipped += chunk;
    }

    if (skipped > 0) {
      // Silence spliced to silence: the step across the join is bounded by
      // SILENCE_PEAK on both sides, so no declick ramp is warranted.
      this.readIdx = pos;
      this.available -= skipped;
      this._silenceSkipped += skipped;
    }
    return skipped;
  }

  _adaptAfterUnderrun() {
    this._underruns++;
    this.target = Math.min(this.TARGET_MAX, this.target + this.TARGET_STEP);
    this._lastUnderrunAt = currentTime;
    this._lastDecayAt = currentTime;
  }

  _maybeDecay() {
    if (this.target <= this.TARGET_MIN) return;
    const now = currentTime;
    if ((now - this._lastUnderrunAt) * 1000 < DECAY_AFTER_MS) return;
    if ((now - this._lastDecayAt) * 1000 < DECAY_TICK_MS) return;
    this.target = Math.max(this.TARGET_MIN, this.target - this.DECAY_STEP);
    this._lastDecayAt = now;
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const channel = output && output[0];
    if (!channel) return true;
    const need = channel.length;

    this._enforceCeiling();

    const excess = this.available - this.target;
    if (excess > this.SHED_SLACK) {
      this._skipDeadAir(Math.min(this.SKIP_MAX, excess));
    }

    if (this.isPlaying && this.available >= need) {
      // Straight copy at exactly 1.0x -- every sample as it was decoded.
      let r = this.readIdx;
      const cap = this.capacity;
      for (let i = 0; i < need; i++) {
        channel[i] = this.ring[r];
        r = r + 1 === cap ? 0 : r + 1;
      }
      this.readIdx = r;
      this.available -= need;

      if (this._rampRemaining > 0) {
        const total = this.DECLICK;
        for (let i = 0; i < need && this._rampRemaining > 0; i++, this._rampRemaining--) {
          channel[i] *= (total - this._rampRemaining) / total;
        }
      }
      this._maybeDecay();
    } else {
      // Underflow: wait for a partial refill rather than emitting a partial
      // block -- a clean silence gap beats a discontinuity mid-buffer.
      // Counted only on the transition out of playing.
      if (this.isPlaying) this._adaptAfterUnderrun();
      this.isPlaying = false;
      channel.fill(0);
    }

    return true;  // keep this node alive for the life of the stream
  }
}

registerProcessor('henwen-lowlatency-playback', LowLatencyPlaybackProcessor);
