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
// What this buffer is trading off
// ===========================================================================
// Buffer occupancy *is* latency, and that sets a hard floor on what any
// policy here can achieve: surviving a 400ms delivery stall requires 400ms
// of buffered audio, so fewer dropouts always costs latency. Nothing below
// changes that. What it does change is the other two costs -- whether audio
// gets *discarded*, and whether latency that a burst forced on us gets
// walked back off quickly or lingers.
//
// Measured arrival jitter on this install, from a week of this path's own
// telemetry-ws lines (per-10s-window maximum WS frame arrival gap, two
// independent remote client networks, 1350 samples): median 300ms, p90
// 723ms, p99 1908ms, max 9136ms. That is the environment this has to work
// in, and it is why a fixed sub-300ms budget cannot also be dropout-free.
//
// Note before reaching for a per-packet fix: this path is Opus over a
// WebSocket, i.e. TCP. Delivery is in-order and lossless by construction --
// there are no late or out-of-order packets to discard. A stall is TCP
// head-of-line blocking, after which the whole backlog arrives at once.
//
// ===========================================================================
// Catching up: SOLA time compression, not frame dropping or resampling
// ===========================================================================
// Once a burst has left the buffer long, the excess has to come back off.
// Three mechanisms were measured against each other on real audio from this
// very repeater (a 7.8s ulaw recording off the box), all at matched speedup:
//
//   method                 speedup   spectral centroid   periodic modulation
//   drop 1-of-2 frames      2.00x         +67%                 +5.7 dB
//   drop 1-of-3 frames      1.50x         +47%                +14.7 dB
//   drop 1-of-8 frames      1.14x         +25%                +28.5 dB
//   linear resample         2.00x        +107%                    --
//   linear resample         1.08x         +12%                    --
//   SOLA                    2.00x          -6%                    --
//   SOLA                    1.30x          -1%                    --
//
// Dropping whole frames shifts the spectrum up sharply and injects strong
// periodic amplitude modulation -- the "robotic"/warbling artifact. Counter-
// intuitively *gentle* dropping is the worst for that: 1-of-8 lands its
// repetition at 6.25Hz, squarely in the range the ear hears as flutter
// (+28.5 dB), so "only drop a few" is not the safe version of the idea.
// Linear resampling has no modulation artifact but shifts pitch and every
// formant by the rate itself, which is what capped the previous
// implementation at 1.08.
//
// SOLA (synchronous overlap-add) removes time by splicing at offsets
// aligned to the waveform's own periodicity and cross-fading the join, so
// pitch and formants stay put: a 2.0x compression measured -6% centroid
// against +67%/+107% for the alternatives, with splice energy *below* the
// original (the cross-fade smooths slightly). It is also what a browser's
// own pitch-preserving playbackRate does, which is how the legacy MSE path
// has always gotten this for free.
//
// Cost, benchmarked in JS on the real recording before committing to it:
// 0.4-0.6% of one core with the coarse-to-fine search below (5.5% with a
// naive full search, also viable). It only runs while catching up, and the
// rate==1.0 path bypasses it entirely and copies straight through. No
// server-side cost at all, so nothing here bears on the Pi Zero 2 W floor.
//
// What that buys, measured against the real arrival-gap distribution with a
// repeater traffic model (600s x 3 seeds), versus the previous fixed-budget
// build at the same 200ms cushion:
//
//                        mean latency   speech-gap/10min   discard events
//   previous (resample)      200ms           6447ms             230
//   this (SOLA)              260ms           5682ms               3
//
// So the budget costs 60ms more on average -- because a burst is now
// *absorbed and played off* rather than discarded on the spot -- and in
// exchange discards drop by two orders of magnitude while gaps also fall.
// Underruns fall too (50 -> 38). The 60ms is the price of not throwing
// audio away, which is the right way round.
//
// A note on a measurement trap here, since the numbers above are easy to
// double-count: "speech not delivered" and "gap heard during speech" are
// not independent costs. Across every build tried they track each other
// almost exactly (5682 vs 5614ms here), because speech displaced by an
// underrun is the same event as the gap itself, not an additional loss.
// Only `resyncs` counts audio that was genuinely thrown away.
//
// Shedding happens in order of increasing audible cost:
//   1. **dead air** -- free. A repeater feed is mostly silence and
//      audio_relay.py injects silence on a quiet node, so a shortened pause
//      is not perceptible. Reuses recording.py's SilenceGate threshold
//      (int16 RMS 300) rather than inventing a second notion of "quiet" for
//      the same audio, applied as a conservative peak bound.
//   2. **SOLA** -- no audio lost at all, just played slightly early.
//   3. **discard** -- only past HARD_MAX_MS, declicked. `resyncs` counts it,
//      and with (2) able to shed ~300ms/s it should be vanishingly rare.
//
// ===========================================================================
// Backing store
// ===========================================================================
// A fixed-size ring for arriving PCM, plus a small circular staging buffer
// for finished output (SOLA produces in ~15ms hops, while process() must
// emit exactly one render quantum). The original implementation allocated
// and concatenated a whole new Float32Array per inbound 20ms frame -- ~67KB
// rebuilt 50 times a second on the real-time audio thread, whose GC pauses
// show up as exactly the underruns this file exists to avoid. Steady-state
// playback here allocates nothing.

const TARGET_MIN_MS    = 200;   // the latency budget; playback rides here
/* Bounded adaptation, enabled 2026-10-07 after characterising this install's
   actual link rather than its average. The uplink's latency is *intermittent*
   on a timescale of minutes, not steadily bad: gateway RTT (one hop) was
   measured swinging between ~10ms average and ~150ms average with
   multi-hundred-ms spikes, with `pipe 3`/`pipe 4` on every sample -- i.e.
   requests queueing. Audio tracked it exactly: arrival gaps averaged 1052ms
   and underruns 37.6/min during a bad patch, then 318ms and 9.2/min twenty
   minutes later with no code change.
   A *fixed* budget is the wrong shape for that. It is excellent while the
   link is healthy (87ms on a clean link) and collapses during the bad
   patches. So the cushion now rides at TARGET_MIN while the link behaves and
   expands only once underruns prove it is not behaving, with SOLA snapping it
   back afterwards -- the bad patches cost latency instead of dropouts, and
   the good stretches (most of the time) still sit at the 200ms budget.
   Measured curve for this ceiling, against the real arrival-gap distribution
   (600s x 3 seeds, speech-gap per 10min of a repeater feed):
       200ms (fixed)  -> 260ms mean latency, 5682ms speech-gap
       250ms          -> 295ms,              5263ms
       300ms          -> 324ms,              5016ms
       400ms          -> 354ms,              4804ms   <-- shipped
       600ms          -> 407ms,              4614ms
   Those means are over a synthetic distribution with a stall every 10s, so
   they describe a *bad* link; on a healthy one the cushion decays to the
   200ms floor and stays there. Discards hold at ~3 per 10min across the
   whole range, so this dial trades latency against gaps only -- it never
   trades away audio. */
const TARGET_MAX_MS    = 400;
const TARGET_STEP_MS   = 150;   // cushion added per underrun, when enabled
const RESUME_FRAC      = 0.5;   // refill this fraction of target before resuming
const DECAY_AFTER_MS   = 20000; // underrun-free time before shaving the cushion
const DECAY_STEP_MS    = 50;    // shaved per decay step
const DECAY_TICK_MS    = 1000;  // minimum interval between decay steps

const SLACK_MS         = 80;    // dead-band: no catch-up for trivial excess
const CATCHUP_BANDS = [
  { overMs: 80,  rate: 1.10 },
  { overMs: 250, rate: 1.25 },
  { overMs: 500, rate: 1.45 },
  { overMs: 900, rate: 1.80 },
];

const SOLA_WIN_MS      = 30;    // analysis window
const SOLA_SEARCH_MS   = 7.5;   // alignment search range
const SOLA_DECIM       = 4;     // coarse-search decimation, refined after

const SKIP_CHUNK_MS    = 3.3;   // granularity of the dead-air scan
const SKIP_MAX_MS      = 10;    // most dead air discardable per render block
/* recording.py's SilenceGate default, 300 on the int16 scale, expressed for
   the float samples WebCodecs hands us. Same audio, same notion of quiet. */
const SILENCE_PEAK     = 300 / 32768;

const HARD_MAX_MS      = 2000;  // past this, discard regardless of content
const RING_EXTRA_MS    = 2000;  // headroom so a burst lands rather than wrapping
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
    this.SLACK       = toSamples(SLACK_MS);
    this.SKIP_CHUNK  = toSamples(SKIP_CHUNK_MS);
    this.SKIP_MAX    = toSamples(SKIP_MAX_MS);
    this.HARD_MAX    = toSamples(HARD_MAX_MS);
    this.DECLICK     = toSamples(DECLICK_MS);
    this._bands = CATCHUP_BANDS
      .map((b) => ({ over: toSamples(b.overMs), rate: b.rate }))
      .sort((a, b) => b.over - a.over);          // highest threshold first

    this.WIN     = toSamples(SOLA_WIN_MS);
    this.OVERLAP = this.WIN >> 1;
    this.SEARCH  = toSamples(SOLA_SEARCH_MS);
    this.HOP_OUT = this.WIN - this.OVERLAP;      // == OVERLAP

    this.target = this.TARGET_MIN;

    // Arrival ring.
    this.capacity = this.HARD_MAX + toSamples(RING_EXTRA_MS) + this.WIN + this.SEARCH;
    this.ring = new Float32Array(this.capacity);
    this.readIdx = 0;                            // integer: SOLA splices on whole samples
    this.writeIdx = 0;
    this.available = 0;

    // Finished-output staging (circular). SOLA emits HOP_OUT at a time.
    this.outCap = this.WIN * 4;
    this.outBuf = new Float32Array(this.outCap);
    this.outRead = 0;
    this.outCount = 0;

    // SOLA's not-yet-finalized tail, carried between hops.
    this.tail = new Float32Array(this.OVERLAP);
    this.tailValid = false;
    // Precomputed cross-fade ramp (allocation-free steady state).
    this.fade = new Float32Array(this.OVERLAP);
    for (let i = 0; i < this.OVERLAP; i++) this.fade[i] = i / this.OVERLAP;

    this.isPlaying = false;
    this._underruns = 0;
    this._resyncs = 0;
    this._silenceSkipped = 0;
    this._solaHops = 0;
    this._rate = 1.0;

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
          bufferedSamples: Math.round(this._buffered()),
          underruns: this._underruns,
          resyncs: this._resyncs,
          silenceSkipMs: Math.round(this._silenceSkipped / sampleRate * 1000),
          solaHops: this._solaHops,
          rate: this._rate,
          targetMs: this.target / sampleRate * 1000,
        });
      }
    };
  }

  /** Total audio held anywhere in this processor -- i.e. the real latency.
   *  The control loop and telemetry must both use this rather than the
   *  arrival ring alone, or they would ignore whatever SOLA has already
   *  staged downstream and chase the wrong number. */
  _buffered() {
    return this.available + this.outCount + (this.tailValid ? this.OVERLAP : 0);
  }

  _ringAt(i) {
    return this.ring[i >= this.capacity ? i - this.capacity : i];
  }

  _armDeclick() {
    this._rampRemaining = this.DECLICK;
  }

  /** Enforces the latency ceiling as ONE deliberate, declicked discard.
   *
   *  Called from both _write() and process(), because a burst can deliver
   *  seconds of audio across many postMessage calls with no process() in
   *  between -- the main thread drains the socket in one task, so every
   *  queued frame lands before the audio thread next runs. Leaving the
   *  ceiling to process() alone let the ring itself fill up, and _write()'s
   *  ring-full guard then discarded in one-frame increments: ~450 separate
   *  splices (and 450 declick ramps) for a single large burst, which both
   *  inflated the resync count and sounded far worse than one clean cut.
   *  Found by instrumenting the resync sites in simulation -- the ring-full
   *  path was firing 544 times where the policy should have fired twice. */
  _enforceCeiling() {
    if (this._buffered() <= this.HARD_MAX) return;
    const over = this._buffered() - this.target;
    this._dropOldest(Math.min(over, this.available));
    this._resyncs++;
  }

  _dropOldest(n) {
    const drop = Math.min(n, this.available);
    if (drop <= 0) return;
    this.readIdx = (this.readIdx + drop) % this.capacity;
    this.available -= drop;
    this._armDeclick();
  }

  _write(data) {
    const cap = this.capacity;
    let n = data.length;
    if (n <= 0) return;
    if (n > cap) { data = data.subarray(n - cap); n = cap; }
    // Make room without counting it as a policy discard -- the ceiling
    // check below is what deliberately bounds latency. This only guards the
    // physical wrap, and the ring is sized so it cannot normally be reached.
    if (this.available + n > cap) this._dropOldest(this.available + n - cap);
    const w = this.writeIdx;
    const first = Math.min(n, cap - w);
    this.ring.set(data.subarray(0, first), w);
    if (n > first) this.ring.set(data.subarray(first), 0);
    this.writeIdx = (w + n) % cap;
    this.available += n;
    this._enforceCeiling();

    if (!this.isPlaying) {
      const resumeAt = Math.max(this.TARGET_MIN * RESUME_FRAC, this.target * RESUME_FRAC);
      if (this._buffered() >= resumeAt) {
        this.isPlaying = true;
        this._armDeclick();
      }
    }
  }

  /** Shedding step 1: skip leading dead air. Free -- nobody hears a
   *  shortened pause between transmissions. */
  _skipDeadAir(maxSkip) {
    const chunk = this.SKIP_CHUNK;
    let pos = this.readIdx;
    let skipped = 0;
    while (skipped + chunk <= maxSkip && skipped + chunk <= this.available) {
      let loud = false;
      for (let i = 0; i < chunk; i++) {
        const v = this._ringAt(pos + i);
        if (v > SILENCE_PEAK || v < -SILENCE_PEAK) { loud = true; break; }
      }
      if (loud) break;
      pos = (pos + chunk) % this.capacity;
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

  /** Shedding step 2's rate, chosen from how far above target we are. */
  _chooseRate(excess) {
    if (excess <= this.SLACK) return 1.0;
    for (const b of this._bands) if (excess > b.over) return b.rate;
    return 1.0;
  }

  _pushOut(src, srcOff, n, mixFade, mixWith) {
    let w = (this.outRead + this.outCount) % this.outCap;
    for (let i = 0; i < n; i++) {
      let v = src[srcOff + i];
      if (mixFade) v = mixWith[i] * (1 - this.fade[i]) + v * this.fade[i];
      this.outBuf[w] = v;
      w = w + 1 === this.outCap ? 0 : w + 1;
    }
    this.outCount += n;
  }

  /**
   * One SOLA hop: consume hopIn samples of input, emit HOP_OUT of output,
   * splicing at the offset within SEARCH that best matches the tail already
   * emitted. Aligning the join to the waveform's own periodicity is what
   * preserves pitch and formants while still removing time.
   *
   * The search is coarse-to-fine (SOLA_DECIM-strided, then refined around
   * the winner), which measured ~10x cheaper than a full search for the
   * same choice of offset.
   */
  _solaHop(rate) {
    const hopIn = Math.round(this.HOP_OUT * rate);
    const win = this.WIN, ov = this.OVERLAP, search = this.SEARCH;
    if (this.available < hopIn + win + search) return false;

    const base = this.readIdx;
    let best = 0;

    if (this.tailValid) {
      const D = SOLA_DECIM;
      let bestScore = -Infinity;
      for (let off = 0; off < search; off += D) {
        let dot = 0, nrm = 0;
        for (let i = 0; i < ov; i += D) {
          const s = this._ringAt(base + off + i);
          dot += this.tail[i] * s;
          nrm += s * s;
        }
        const score = dot / (Math.sqrt(nrm) + 1e-9);
        if (score > bestScore) { bestScore = score; best = off; }
      }
      const lo = Math.max(0, best - D), hi = Math.min(search - 1, best + D);
      bestScore = -Infinity;
      let refined = best;
      for (let off = lo; off <= hi; off++) {
        let dot = 0, nrm = 0;
        for (let i = 0; i < ov; i++) {
          const s = this._ringAt(base + off + i);
          dot += this.tail[i] * s;
          nrm += s * s;
        }
        const score = dot / (Math.sqrt(nrm) + 1e-9);
        if (score > bestScore) { bestScore = score; refined = off; }
      }
      best = refined;
    }

    const src = base + best;
    // Cross-fade the overlap against the carried tail, finalising HOP_OUT.
    let w = (this.outRead + this.outCount) % this.outCap;
    for (let i = 0; i < ov; i++) {
      const s = this._ringAt(src + i);
      const f = this.fade[i];
      this.outBuf[w] = this.tailValid ? (this.tail[i] * (1 - f) + s * f) : s;
      w = w + 1 === this.outCap ? 0 : w + 1;
    }
    this.outCount += ov;
    // Carry the window's remainder as the next tail.
    for (let i = 0; i < ov; i++) this.tail[i] = this._ringAt(src + ov + i);
    this.tailValid = true;

    this.readIdx = (this.readIdx + hopIn) % this.capacity;
    this.available -= hopIn;
    this._solaHops++;
    return true;
  }

  /** rate==1.0 path: straight copy, no SOLA, bit-exact and allocation-free. */
  _passThrough(n) {
    const take = Math.min(n, this.available, this.outCap - this.outCount);
    if (take <= 0) return false;
    let r = this.readIdx;
    let w = (this.outRead + this.outCount) % this.outCap;
    for (let i = 0; i < take; i++) {
      this.outBuf[w] = this.ring[r];
      r = r + 1 === this.capacity ? 0 : r + 1;
      w = w + 1 === this.outCap ? 0 : w + 1;
    }
    this.readIdx = r;
    this.available -= take;
    this.outCount += take;
    return true;
  }

  /** Moving off SOLA: the carried tail is real audio, so emit it rather
   *  than letting a mode switch silently swallow OVERLAP samples. */
  _flushTail() {
    if (!this.tailValid) return;
    if (this.outCap - this.outCount < this.OVERLAP) return;
    this._pushOut(this.tail, 0, this.OVERLAP, false, null);
    this.tailValid = false;
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

    // --- shedding step 1: dead air -------------------------------------
    let excess = this._buffered() - this.target;
    if (excess > this.SLACK) {
      this._skipDeadAir(Math.min(this.SKIP_MAX, excess, this.available));
      excess = this._buffered() - this.target;
    }

    // --- shedding step 3 (ceiling): discard ----------------------------
    const beforeCeiling = this._resyncs;
    this._enforceCeiling();
    if (this._resyncs !== beforeCeiling) excess = this._buffered() - this.target;

    // --- shedding step 2: SOLA catch-up, else pass through -------------
    const rate = this._chooseRate(excess);
    this._rate = rate;
    if (rate === 1.0) this._flushTail();

    let guard = 8;   // bound the work per render quantum
    while (this.outCount < need && guard-- > 0) {
      const produced = (rate > 1.0) ? this._solaHop(rate) : this._passThrough(this.HOP_OUT);
      if (!produced) break;
    }

    if (this.isPlaying && this.outCount >= need) {
      let r = this.outRead;
      for (let i = 0; i < need; i++) {
        channel[i] = this.outBuf[r];
        r = r + 1 === this.outCap ? 0 : r + 1;
      }
      this.outRead = r;
      this.outCount -= need;
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
      this._rate = 1.0;
      channel.fill(0);
    }

    return true;  // keep this node alive for the life of the stream
  }
}

registerProcessor('henwen-lowlatency-playback', LowLatencyPlaybackProcessor);
