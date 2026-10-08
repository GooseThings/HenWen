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
// buffer>]). This processor also answers {type: 'stats-request'} with a
// {type: 'stats', bufferedSamples, underruns, resyncs, silenceSkipMs,
// rate, targetMs} reply, polled every 10s by the main thread for the
// [AUDIO-CLIENT] telemetry report.
//
// ---------------------------------------------------------------------------
// Latency budget: a fixed ~200ms, not an adaptive cushion
// ---------------------------------------------------------------------------
// TARGET_MS is a hard product requirement, not a tuning preference: this
// path exists to be low-latency and the budget is ~200ms. It deliberately
// does NOT grow the cushion to ride out a bad link, which is what the
// legacy WebM/MSE path does (baseline 750ms, adapting to a 4000ms ceiling).
// A listener on a link too jittery for this budget should use that path
// instead -- that trade is what it is for, and it is one Manager setting
// away (Manager > Audio, rx_audio_config.path). Trying to serve both goals
// from one buffer just produces a path that is neither low-latency nor
// smooth.
//
// The consequence has to be stated plainly, because it is unavoidable
// rather than a tuning artifact: **a delivery stall longer than the budget
// must produce either a gap or discarded audio.** Audio that has not
// arrived cannot be played, and audio that arrives late can only be played
// late (latency) or skipped (loss). With a fixed ~200ms budget and
// measured arrival stalls whose median was ~300ms, gaps are expected. What
// this file can control is *what* gets sacrificed, and that is what the
// shedding policy below is about.
//
// ---------------------------------------------------------------------------
// Shedding policy: spend silence, then pitch, then (last) speech
// ---------------------------------------------------------------------------
// When a burst leaves the buffer longer than the budget, the excess is shed
// in a strict order of increasing audible cost:
//
//   1. **Skip dead air.** A repeater feed is mostly silence -- and
//      audio_relay.py explicitly injects silence whenever the node is
//      quiet, so the backlog accumulated during a stall is very often dead
//      air rather than speech. Discarding that is free: nobody can hear a
//      shortened pause. The silence test reuses this project's own
//      established threshold, `recording_config.silence_rms_thresh`
//      (default 300 on the int16 scale, i.e. ~0.0092 of full scale) from
//      recording.py's SilenceGate, rather than inventing a second notion
//      of "quiet" for the same audio. It is applied as a *peak* bound
//      rather than an RMS average, which is both cheaper (early exit on
//      the first loud sample) and more conservative -- it errs toward
//      keeping audio.
//   2. **Play slightly fast.** Whatever excess remains is absorbed by
//      advancing the read pointer faster than real time and resampling on
//      the way out. No audio is lost; it is played marginally early. Rates
//      are capped low (1.08) because resampling shifts pitch, where the
//      MSE path gets the browser's pitch-preserving WSOLA for free.
//   3. **Discard, last.** Only past LATENCY_MAX_MS -- a stall so large
//      that neither of the above can hold the budget -- is audio dropped
//      outright, declicked, down to the target. This is the only step that
//      can cut speech, and `resyncs` counts it.
//
// After an underrun, playback resumes at RESUME_MS rather than refilling
// the full budget, so a gap costs about the stall itself instead of the
// stall plus a full pre-roll.
//
// ---------------------------------------------------------------------------
// Note on what this cannot fix
// ---------------------------------------------------------------------------
// This buffer is the last stage in the chain and can only choose how to
// spend jitter that already happened. It is not the right place to *reduce*
// that jitter. The arrival stalls measured on this install traced to the
// box's own uplink -- it was serving over Wi-Fi (5GHz, -68dBm) with power
// save enabled, which parks the radio between beacons and delivers in
// bursts; both independent client networks saw the same stall-then-burst
// signature, which a per-client problem would not explain. Wired Ethernet,
// or at minimum `iw dev <iface> set power_save off`, buys more here than any
// amount of tuning in this file, because it makes the 200ms budget
// achievable without spending anything at all.
//
// Also worth knowing before reaching for a per-packet fix: this path is
// Opus over a WebSocket, i.e. TCP. Delivery is in-order and lossless by
// construction -- there are no late or out-of-order packets to discard. A
// stall is TCP head-of-line blocking, after which the whole backlog
// arrives at once.
//
// ---------------------------------------------------------------------------
// Backing store
// ---------------------------------------------------------------------------
// A fixed-size ring buffer. The original implementation allocated and
// concatenated a whole new Float32Array per inbound 20ms frame -- at 48kHz
// that is ~67KB rebuilt 50 times a second *on the real-time audio thread*,
// whose GC pauses show up as exactly the underruns this file exists to
// avoid. Steady-state playback here allocates nothing.

const TARGET_MS        = 200;   // the latency budget; playback rides at this occupancy
const RESUME_MS        = 100;   // pre-roll after an underrun (shorter gap than a full refill)
const LATENCY_MAX_MS   = 320;   // past this, discard regardless of content

const SHED_SLACK_MS    = 40;    // dead-band: don't shed for trivial excess
const SKIP_CHUNK_MS    = 3.3;   // granularity of the dead-air scan
const SKIP_MAX_MS      = 10;    // most dead air discardable per render block
/* recording.py's SilenceGate default, 300 on the int16 scale, expressed for
   the float samples WebCodecs hands us. Same audio, same notion of quiet. */
const SILENCE_PEAK     = 300 / 32768;

const RATE_BANDS = [
  { overMs: 60,  rate: 1.02 },
  { overMs: 120, rate: 1.04 },
  { overMs: 200, rate: 1.08 },
];

const RING_EXTRA_MS    = 2000;  // ring headroom so a burst lands rather than wrapping
const DECLICK_MS       = 5;     // linear ramp over a splice (matches audio_relay.py)

class LowLatencyPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    // sampleRate here is a global AudioWorkletGlobalScope binding equal to
    // the owning AudioContext's rate -- _doStartListenWS() constructs that
    // context with sampleRate: 48000 to match WebCodecs' Opus decoder
    // output exactly, so these work out to 48000 in practice; computed
    // from `sampleRate` rather than hardcoded so this stays correct if that
    // ever changes on either side.
    const toSamples = (ms) => Math.max(1, Math.round(sampleRate * ms / 1000));

    this.TARGET      = toSamples(TARGET_MS);
    this.RESUME      = toSamples(RESUME_MS);
    this.LATENCY_MAX = toSamples(LATENCY_MAX_MS);
    this.SHED_SLACK  = toSamples(SHED_SLACK_MS);
    this.SKIP_CHUNK  = toSamples(SKIP_CHUNK_MS);
    this.SKIP_MAX    = toSamples(SKIP_MAX_MS);
    this.DECLICK     = toSamples(DECLICK_MS);
    this._rateBands  = RATE_BANDS
      .map((b) => ({ over: toSamples(b.overMs), rate: b.rate }))
      .sort((a, b) => b.over - a.over);   // highest threshold first

    this.capacity = this.LATENCY_MAX + toSamples(RING_EXTRA_MS);
    this.ring = new Float32Array(this.capacity);

    // readPos is fractional because the catch-up read resamples; writeIdx is
    // a plain integer since writes are always whole frames. `available` is
    // the buffered sample count, fractional for the same reason readPos is.
    this.readPos = 0;
    this.writeIdx = 0;
    this.available = 0;

    this.isPlaying = false;
    this._underruns = 0;
    this._resyncs = 0;
    this._silenceSkipped = 0;
    this._rate = 1.0;

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
          resyncs: this._resyncs,
          silenceSkipMs: Math.round(this._silenceSkipped / sampleRate * 1000),
          rate: this._rate,
          targetMs: TARGET_MS,
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

  _write(data) {
    const cap = this.capacity;
    let n = data.length;
    if (n <= 0) return;

    if (n > cap) {
      data = data.subarray(n - cap);
      n = cap;
    }
    if (this.available + n > cap) {
      // The ring itself is full -- sized so this needs a stall far beyond
      // LATENCY_MAX, which process() would already have resynced away.
      this._dropOldest(this.available + n - cap);
      this._resyncs++;
    }

    const w = this.writeIdx;
    const first = Math.min(n, cap - w);
    this.ring.set(data.subarray(0, first), w);
    if (n > first) this.ring.set(data.subarray(first), 0);
    this.writeIdx = (w + n) % cap;
    this.available += n;

    if (!this.isPlaying && this.available >= this.RESUME) {
      this.isPlaying = true;
      this._armDeclick();
    }
  }

  /**
   * Shedding step 1: advance the read pointer past leading dead air, up to
   * `maxSkip` samples. Returns how much was skipped.
   *
   * Scans in SKIP_CHUNK blocks and stops at the first chunk containing any
   * sample above SILENCE_PEAK, so speech is never skipped -- only a run of
   * quiet immediately at the playback point. Costs nothing audible: a
   * shortened pause between transmissions is not perceptible, which is what
   * makes this the cheapest latency available on a repeater feed.
   */
  _skipDeadAir(maxSkip) {
    const cap = this.capacity;
    const ring = this.ring;
    const chunk = this.SKIP_CHUNK;
    let pos = Math.floor(this.readPos);
    let skipped = 0;

    while (skipped + chunk <= maxSkip) {
      let loud = false;
      for (let i = 0; i < chunk; i++) {
        const v = ring[(pos + i) % cap];
        if (v > SILENCE_PEAK || v < -SILENCE_PEAK) { loud = true; break; }
      }
      if (loud) break;
      pos = (pos + chunk) % cap;
      skipped += chunk;
    }

    if (skipped > 0) {
      // Silence spliced to silence -- the step across the join is bounded by
      // SILENCE_PEAK on both sides, so no declick ramp is warranted here.
      this.readPos = pos;
      this.available -= skipped;
      this._silenceSkipped += skipped;
    }
    return skipped;
  }

  /** Shedding step 2: playback rate for this block, from the remaining excess. */
  _chooseRate(excess) {
    if (excess <= this.SHED_SLACK) return 1.0;
    for (const band of this._rateBands) {
      if (excess > band.over) return band.rate;
    }
    return 1.0;
  }

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

  _armDeclick() {
    this._rampRemaining = this.DECLICK;
    this._rampTotal = this.DECLICK;
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const channel = output && output[0];
    if (!channel) return true;
    const need = channel.length;

    // Shed in order of increasing audible cost: dead air, then pitch, then
    // (only past the ceiling) real audio.
    let excess = this.available - this.TARGET;
    if (excess > this.SHED_SLACK) {
      excess -= this._skipDeadAir(Math.min(this.SKIP_MAX, excess));
    }
    if (this.available > this.LATENCY_MAX) {
      this._dropOldest(this.available - this.TARGET);
      this._resyncs++;
      excess = this.available - this.TARGET;
    }

    const rate = this._chooseRate(excess);
    // Interpolation reads one sample past the final position, so require a
    // little more than the nominal consumption before committing to a read.
    const required = need * rate + 2;

    if (this.isPlaying && this.available >= required) {
      this._rate = rate;
      this._readResampled(channel, need, rate);
      if (this._rampRemaining > 0) {
        const total = this._rampTotal;
        for (let i = 0; i < need && this._rampRemaining > 0; i++, this._rampRemaining--) {
          channel[i] *= (total - this._rampRemaining) / total;
        }
      }
    } else {
      // Underflow: wait for RESUME_MS rather than playing a partial block --
      // a clean silence gap beats a discontinuity mid-buffer. Counted only
      // on the transition out of playing, not per silent block.
      if (this.isPlaying) this._underruns++;
      this.isPlaying = false;
      this._rate = 1.0;
      channel.fill(0);
    }

    return true;  // keep this node alive for the life of the stream
  }
}

registerProcessor('henwen-lowlatency-playback', LowLatencyPlaybackProcessor);
