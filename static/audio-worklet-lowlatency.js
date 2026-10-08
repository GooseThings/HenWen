// audio-worklet-lowlatency.js
//
// AudioWorkletProcessor backing the low-latency ("lowlatency") RX audio
// path's playback -- runs on the browser's dedicated real-time audio
// rendering thread, decoupled from the main thread's own event loop (the
// entire reason this path exists: an <audio> element's/MediaSource's
// playback pipeline has more inherent latency than this lower-level API).
//
// Adapted from the jitter-buffer design in a different local project
// (rigcontrolweb's public/audio-processor.js, studied this session as a
// reference for a low-latency RX architecture) -- same drop-oldest-on-
// overflow philosophy (a deliberate departure from this project's own
// default WebM/MSE path, which grows its cushion instead of ever dropping
// audio -- see status.html's MSE live-edge tuning comments), tuned with
// larger numbers for this pipeline's own extra hop: audio_relay.py's
// dual-write -> a per-node ffmpeg encode -> a WebSocket -> a WebCodecs
// decode, one more stage than that project's direct native-Opus-binding ->
// WebSocket path.
//
// Fed by status.html's _doStartListenWS(): each WebCodecs AudioDecoder
// output callback posts one decoded Float32Array of PCM here via
// port.postMessage({type: 'pcm', pcm: <Float32Array>}, [<transferred
// buffer>]). This processor also answers {type: 'stats-request'} with a
// {type: 'stats', bufferedSamples, underruns, overflows} reply, polled
// every 10s by the main thread for the [AUDIO-CLIENT] telemetry report
// (mirrors the MSE path's own periodic report, so both paths' health is
// comparable side by side in the server log).

class LowLatencyPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(0);
    this.isPlaying = false;

    // sampleRate here is a global AudioWorkletGlobalScope binding equal to
    // the owning AudioContext's rate -- _doStartListenWS() constructs that
    // context with sampleRate: 48000 to match WebCodecs' Opus decoder
    // output exactly, so these both work out to the same 48000 in
    // practice; computed from `sampleRate` rather than hardcoded so this
    // stays correct if that ever changes on either side.
    this.MIN_BUFFER_SAMPLES = Math.round(sampleRate * 0.12);  // 120ms pre-roll before starting playback
    this.MAX_BUFFER_SAMPLES = Math.round(sampleRate * 0.35);  // 350ms hard cap -- drop oldest rather than grow latency

    this._underruns = 0;
    this._overflows = 0;

    this.port.onmessage = (e) => {
      if (e.data.type === 'pcm') {
        const newData = e.data.pcm;
        const newBuffer = new Float32Array(this.buffer.length + newData.length);
        newBuffer.set(this.buffer, 0);
        newBuffer.set(newData, this.buffer.length);

        // Overflow protection: if the buffer grows past the cap (a burst
        // arrived, or this processor's own thread was briefly starved),
        // drop the oldest samples to catch back up to real time instead of
        // letting latency grow -- the entire point of this mode.
        if (newBuffer.length > this.MAX_BUFFER_SAMPLES) {
          this.buffer = newBuffer.subarray(newBuffer.length - this.MAX_BUFFER_SAMPLES);
          this._overflows++;
        } else {
          this.buffer = newBuffer;
        }

        if (!this.isPlaying && this.buffer.length >= this.MIN_BUFFER_SAMPLES) {
          this.isPlaying = true;
        }
      } else if (e.data.type === 'stats-request') {
        this.port.postMessage({
          type: 'stats',
          bufferedSamples: this.buffer.length,
          underruns: this._underruns,
          overflows: this._overflows,
        });
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const channel = output && output[0];
    if (!channel) return true;

    if (this.isPlaying && this.buffer.length >= channel.length) {
      channel.set(this.buffer.subarray(0, channel.length));
      this.buffer = this.buffer.subarray(channel.length);
    } else {
      // Underflow: pause and wait for MIN_BUFFER_SAMPLES to refill rather
      // than playing back whatever partial data exists -- a hard silence
      // gap here is preferable to a discontinuity mid-buffer.
      if (this.isPlaying) this._underruns++;
      this.isPlaying = false;
      channel.fill(0);
    }

    return true;  // keep this node alive for the life of the stream
  }
}

registerProcessor('henwen-lowlatency-playback', LowLatencyPlaybackProcessor);
