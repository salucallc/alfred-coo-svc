// Alfred voice puck firmware.
//
// Tickets:
//   SAL-3999 (P1.1): project skeleton, USB-CDC, I2C smoke test, heartbeat.
//   SAL-4000 (P1.2): I2S slave RX from XVF3800 + channel-0 RMS log.
//   SAL-4001 (P1.3): Opus voice encode (16 kHz mono 20 ms 24 kbps VBR) +
//                    outbound EncodedFrame queue + drain logger task.
//   SAL-4006 (P1.6): Wi-Fi STA + WebSocket client to fleet_voice gateway;
//                    encoded Opus frames are now shipped over the wire
//                    instead of being logged + dropped. The drain task
//                    is replaced by lib/transport/transport.cpp which
//                    owns the WS connection + outbound pump.
//
// Hardware: Seeed XIAO ESP32-S3 mounted on Seeed ReSpeaker XVF3800 carrier.
// Toolchain: PlatformIO + arduino-esp32 framework (see platformio.ini).
//
// What this firmware proves so far:
//   1. The XIAO boots and talks over its native USB-CDC serial.
//   2. PSRAM is enabled and visible (non-zero ESP.getFreePsram()).
//   3. The custom huge_app partition table (3 MB app slot) is honored.
//   4. The XVF3800 control servicer at I2C 0x2C still ACKs from firmware
//      (matches the bench result captured in p0_i2c.log).
//   5. The XVF3800 (i2s_master test5 firmware) is driving I2S into the
//      XIAO, the XIAO is consuming it as I2S slave RX, and channel-0 RMS
//      tracks acoustic energy in the room.
//   6. The captured 48 kHz stereo blocks are anti-alias-filtered, decimated
//      to 16 kHz mono int16, encoded by Opus into ~20 ms voice frames,
//      and pushed into an outbound queue at a steady ~50 fps. A drain task
//      on core 0 logs queue stats so we can see encoding is staying ahead
//      of audio capture.
//
// Task topology (as of SAL-4007):
//   * loopTask (Arduino, core 0)        : heartbeats every 5 s; now also
//                                          dumps transport + decoder + I2S
//                                          TX stats so ops can see the full
//                                          mic-to-speaker pipeline from
//                                          the same one-line view.
//   * encoderTask (core 1, prio 5)      : drains audio_io blocks, encodes,
//                                          pushes EncodedFrame to outbound q.
//   * playbackTask (core 1, prio 4)     : drains the inbound playback q
//                                          (Opus packets pushed by the WS
//                                          callback), decodes 16k mono ->
//                                          int16, upsamples 3x to 48k,
//                                          stereo-duplicates, promotes to
//                                          int32, pushes to I2S TX. SAL-4007.
//   * voice_ws (core 0, prio 3)         : owns Wi-Fi STA + WebSocket
//                                          client; pumps EncodedFrame
//                                          out as binary WS messages,
//                                          handles inbound JSON control
//                                          + binary TTS frames; for SAL-4007
//                                          fires the audio_start /
//                                          audio_bin / audio_end callbacks
//                                          which copy packets onto the
//                                          playback queue. See
//                                          lib/transport/transport.cpp.
//
// Why these pinnings: PLAN.md risk #1 says Opus on ESP32-S3 may starve I2S
// DMA. We pin both the I2S DMA reader and the encoder to core 1 so they
// share a CPU and never get preempted by the Wi-Fi/network stack which
// always runs on core 0. The drain logger lives on core 0 because it does
// nearly no work and is naturally where the WS client will land.
//
// Future tickets plug in here:
//   * SAL-4007 (Opus decode + I2S TX) - DONE in this file. Inbound Opus
//                                       packets shipped down by the gateway
//                                       are queued by the transport task,
//                                       drained by playbackTask, decoded
//                                       to 16 kHz mono int16 PCM, upsampled
//                                       to 48 kHz, stereo-duplicated, and
//                                       pushed out I2S DOUT (GPIO44) into
//                                       the TLV320AIC3104 codec on the
//                                       ReSpeaker carrier.

#include <Arduino.h>
#include <Wire.h>
#include <atomic>
#include <string.h>
#include <esp_chip_info.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include "audio_io.h"
#include "i2s_tx.h"
#include "opus_decoder.h"
#include "transport.h"
#include "voice_codec.h"

namespace {

// XIAO ESP32-S3 default I2C pins per the Seeed reference pinout.
constexpr int kI2cSdaPin = 5;
constexpr int kI2cSclPin = 6;
constexpr uint32_t kI2cFreqHz = 100000;

// Known peers on the ReSpeaker XVF3800 carrier.
constexpr uint8_t kAddrTlv320Codec = 0x18;
constexpr uint8_t kAddrXvf3800Ctl  = 0x2C;

// Heartbeat cadence; matches the SAL-3999 acceptance criterion.
constexpr uint32_t kHeartbeatIntervalMs = 5000;

// Audio block size in stereo frames at 48 kHz: 20 ms * 48 = 960. Matches
// alfred::codec::kInputBlockFrames so the encoder can consume one block
// per encode_block call.
constexpr size_t kAudioBlockFrames = alfred::codec::kInputBlockFrames;

// Outbound queue capacity. Each EncodedFrame is ~210 bytes (200 payload +
// metadata). 100 entries = ~21 KB of internal RAM plus FreeRTOS overhead,
// covering 2 s of 50 fps voice — comfortable headroom for any momentary
// stall in the WS sender (SAL-4006). If 21 KB on internal RAM ever bites,
// move the queue to PSRAM via xQueueCreateStatic in PSRAM-backed memory.
constexpr UBaseType_t kOutboundQueueDepth = 100;

// SAL-4007 inbound playback queue. Each PlaybackPacket carries one Opus
// packet (max 256 bytes payload + length). 100 entries = ~26 KB of
// internal RAM, covers 2 s of 50 fps TTS with comfortable headroom for
// I2S backpressure on a stalled speaker. Queue lives in internal RAM
// because the playback task hits it at 50 Hz and PSRAM would blow the
// per-access budget.
constexpr UBaseType_t kPlaybackQueueDepth = 100;

// Maximum Opus packet bytes we'll buffer per WS frame. Matches
// alfred::codec::kDecodeMaxPacketBytes upper bound but capped tight
// since the TTS adapter ships at 32 kbps / 20 ms / mono ≈ 80 bytes/frame.
constexpr size_t kPlaybackPacketMaxBytes = 256;

struct PlaybackPacket {
  uint16_t length;                          // bytes used in `data`
  uint8_t  data[kPlaybackPacketMaxBytes];   // raw Opus packet
};

uint32_t g_last_heartbeat_ms = 0;
uint32_t g_heartbeat_seq = 0;

// Outbound encoded-frame queue. encoderTask pushes; drainTask pops.
QueueHandle_t g_outbound_q = nullptr;

// SAL-4007 inbound playback queue. transport's audio_bin callback
// pushes PlaybackPacket items; playbackTask drains them.
QueueHandle_t g_playback_q = nullptr;

// SAL-4007 playback counters. Updated only by playbackTask + the
// audio sink callbacks (single writer per field), read by the
// heartbeat. Atomic for safe cross-core reads.
std::atomic<uint32_t> g_playback_starts{0};      // audio_start envelopes received
std::atomic<uint32_t> g_playback_ends{0};        // audio_end envelopes received
std::atomic<uint32_t> g_playback_pkts_queued{0}; // binary frames pushed onto playback_q
std::atomic<uint32_t> g_playback_pkts_dropped{0};// binary frames dropped (queue full)
std::atomic<uint32_t> g_playback_pkts_decoded{0};// packets the playback task decoded
std::atomic<uint32_t> g_playback_frames_tx{0};   // I2S TX frame count (stereo frames)

// Monotonic sequence number for outbound frames.
std::atomic<uint32_t> g_frame_seq{0};

// Returns the WiFi MAC formatted as a stable device id, e.g. "alfred-puck-aabbccddeeff".
String deviceId() {
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char buf[32] = {0};
  snprintf(buf, sizeof(buf), "alfred-puck-%02x%02x%02x%02x%02x%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(buf);
}

// One-shot probe of an I2C address. Returns true on ACK.
bool i2cProbe(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void printBanner() {
  esp_chip_info_t chip = {};
  esp_chip_info(&chip);

  Serial.println();
  Serial.println(F("================================================================"));
  Serial.println(F(" Alfred Voice Puck firmware"));
  Serial.println(F("================================================================"));
  Serial.printf( " device_id      : %s\r\n", deviceId().c_str());
  Serial.printf( " fw_version     : %s\r\n", ALFRED_FW_VERSION);
  Serial.printf( " build_ts       : %s %s\r\n", __DATE__, __TIME__);
  Serial.printf( " chip_model     : ESP32-S3 rev %d, %d core(s)\r\n",
                 chip.revision, chip.cores);
  Serial.printf( " idf_version    : %s\r\n", esp_get_idf_version());
  Serial.printf( " free_heap      : %lu bytes\r\n",
                 (unsigned long)ESP.getFreeHeap());
  Serial.printf( " free_psram     : %lu bytes\r\n",
                 (unsigned long)ESP.getFreePsram());
  Serial.printf( " psram_size     : %lu bytes\r\n",
                 (unsigned long)ESP.getPsramSize());
  Serial.printf( " flash_size     : %lu bytes\r\n",
                 (unsigned long)ESP.getFlashChipSize());
  Serial.printf( " sketch_size    : %lu / %lu bytes\r\n",
                 (unsigned long)ESP.getSketchSize(),
                 (unsigned long)ESP.getFreeSketchSpace() + ESP.getSketchSize());
  Serial.println(F("================================================================"));
}

// Reproducible smoke test for the I2C control surface; matches p0 bench
// results so a regression here is loud rather than silent.
void runI2cSmokeTest() {
  Wire.begin(kI2cSdaPin, kI2cSclPin, kI2cFreqHz);
  delay(50);

  const bool codec_ack = i2cProbe(kAddrTlv320Codec);
  const bool xvf_ack   = i2cProbe(kAddrXvf3800Ctl);

  Serial.println(F("[i2c-smoke] scanning known peers..."));
  Serial.printf(" 0x%02X TLV320AIC3104 codec : %s\r\n",
                kAddrTlv320Codec, codec_ack ? "ACK" : "NO-ACK");
  Serial.printf(" 0x%02X XVF3800 control     : %s\r\n",
                kAddrXvf3800Ctl,  xvf_ack  ? "ACK" : "NO-ACK");

  if (xvf_ack) {
    Serial.println(F("[i2c-smoke] PASS: 0x2C ACK confirms XVF3800 control surface."));
  } else {
    Serial.println(F("[i2c-smoke] FAIL: 0x2C did NOT ACK. Re-check XMOS firmware "
                     "(needs respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_test5.bin)."));
  }
}

void printHeartbeat() {
  ++g_heartbeat_seq;
  alfred::audio::AudioStats a = alfred::audio::audio_io_get_stats();
  alfred::codec::CodecStats c = alfred::codec::voice_codec_get_stats();
  alfred::codec::DecoderStats d = alfred::codec::opus_decoder_get_stats();
  alfred::audio::I2sTxStats x = alfred::audio::i2s_tx_get_stats();
  alfred::transport::TransportStats t = alfred::transport::transport_get_stats();
  Serial.printf("[heartbeat seq=%lu uptime=%lus heap=%lu psram=%lu "
                "audio_frames=%llu under=%lu opus_frames=%llu opus_bytes=%llu "
                "max_enc_us=%lu over=%lu wifi=%d rssi=%lu ws=%d "
                "tx_frames=%llu tx_bytes=%llu rx_text=%llu rx_bin=%llu "
                "drop_off=%llu "
                "pb_starts=%lu pb_ends=%lu pb_qd=%lu pb_drop=%lu "
                "dec_pkts=%llu dec_err=%llu dec_samples=%llu max_dec_us=%lu "
                "i2stx_frames=%llu i2stx_err=%lu i2stx_short=%lu max_w_us=%lu]\r\n",
                (unsigned long)g_heartbeat_seq,
                (unsigned long)(millis() / 1000UL),
                (unsigned long)ESP.getFreeHeap(),
                (unsigned long)ESP.getFreePsram(),
                (unsigned long long)a.frames_in,
                (unsigned long)a.underruns,
                (unsigned long long)c.frames_out,
                (unsigned long long)c.bytes_out,
                (unsigned long)c.max_encode_us,
                (unsigned long)c.encode_overruns,
                (int)t.wifi_connected,
                (unsigned long)t.wifi_rssi_dbm,
                (int)t.ws_connected,
                (unsigned long long)t.frames_sent,
                (unsigned long long)t.bytes_sent,
                (unsigned long long)t.frames_recv_text,
                (unsigned long long)t.frames_recv_binary,
                (unsigned long long)t.frames_dropped_offline,
                (unsigned long)g_playback_starts.load(std::memory_order_relaxed),
                (unsigned long)g_playback_ends.load(std::memory_order_relaxed),
                (unsigned long)g_playback_pkts_queued.load(std::memory_order_relaxed),
                (unsigned long)g_playback_pkts_dropped.load(std::memory_order_relaxed),
                (unsigned long long)d.packets_decoded,
                (unsigned long long)d.packets_errored,
                (unsigned long long)d.samples_out,
                (unsigned long)d.max_decode_us,
                (unsigned long long)x.frames_out,
                (unsigned long)x.write_errors,
                (unsigned long)x.write_short,
                (unsigned long)x.max_write_us);
}

// FreeRTOS task: drain audio_io, encode, push EncodedFrame onto the
// outbound queue. Pinned to core 1 (PLAN.md risk #1).
//
// Why a single task does both reading and encoding: keeping the I2S read
// and the Opus encode on the same task and same core eliminates any
// inter-task latency, lets us measure end-to-end "block-arrived to
// frame-emitted" wall time as one unit, and makes back-pressure trivial.
// If the encoder ever falls behind, audio_io_read_block will start
// returning short or 0 (the DMA buffers fill up and i2s_read times out)
// which surfaces as audio.underruns. Keeping the two pieces apart on
// different tasks would just hide the back-pressure inside a queue.
void encoderTask(void* /*arg*/) {
  // 20 ms stereo at 48 kHz = 960 frames * 2 ch * 4 bytes = 7680 bytes.
  // Allocated on the heap rather than the task stack so we keep the task
  // stack small (8 KB is enough for opus_encode's working set since the
  // encoder uses its own internal allocations on the heap; see
  // FIXED_POINT in opus's config.h).
  int32_t* block = (int32_t*)heap_caps_malloc(
      kAudioBlockFrames * alfred::audio::kChannelCount * sizeof(int32_t),
      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (block == nullptr) {
    Serial.println(F("[encoder] FATAL: heap_caps_malloc for I2S block failed"));
    vTaskDelete(nullptr);
    return;
  }

  // EncodedFrame allocated on the heap too, copied into queue by value.
  alfred::codec::EncodedFrame* frame = (alfred::codec::EncodedFrame*)
      heap_caps_malloc(sizeof(alfred::codec::EncodedFrame),
                       MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (frame == nullptr) {
    Serial.println(F("[encoder] FATAL: heap_caps_malloc for EncodedFrame failed"));
    free(block);
    vTaskDelete(nullptr);
    return;
  }

  Serial.println(F("[encoder] task running; pulling 20 ms blocks @ 48k stereo, "
                   "encoding to 16k mono Opus."));

  for (;;) {
    const size_t got = alfred::audio::audio_io_read_block(
        block, kAudioBlockFrames, /*timeout_ms=*/100);
    if (got == 0) {
      // Audio underrun, briefly yield then retry.
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    if (got < kAudioBlockFrames) {
      // Partial read — DMA gave us less than a full 20 ms. Skip; next
      // call will get the remainder. Reflected as audio underrun stat.
      continue;
    }

    size_t out_len = 0;
    const bool ok = alfred::codec::voice_codec_encode_block(
        block, frame->data, &out_len);
    if (!ok) {
      // Encode error already accounted in codec stats.
      continue;
    }
    frame->seq = g_frame_seq.fetch_add(1, std::memory_order_relaxed);
    frame->timestamp_ms = millis();
    frame->length = static_cast<uint16_t>(out_len);

    // Try to push into outbound queue. If full (drain task starved),
    // drop the oldest by popping then pushing — this gives "drop oldest
    // on overflow" semantics which is what voice apps want over WS:
    // the listener cares about the latest audio, not stale.
    if (xQueueSend(g_outbound_q, frame, 0) != pdTRUE) {
      alfred::codec::EncodedFrame stale;
      xQueueReceive(g_outbound_q, &stale, 0);
      xQueueSend(g_outbound_q, frame, 0);
    }
  }
}

// ---------------------------------------------------------------------------
// SAL-4007 audio sink callbacks (fired by transport on the voice_ws task).
//
// Keep these short. They MUST NOT block the WS receive loop:
//   * onAudioStart: open the decoder. Cheap (~10s of microseconds).
//   * onAudioBin:   copy the packet into the playback queue. If the
//                   queue is full, drop the OLDEST item then push (drop-
//                   oldest semantics, same as the encoder side, so a
//                   transient I2S stall doesn't accumulate stale audio).
//   * onAudioEnd:   close the decoder. Decoder destroy is heavy-ish
//                   (frees ~10 KB) but not blocking on anything outside
//                   itself; runs on the WS task which is fine.
// ---------------------------------------------------------------------------

void onAudioStart(const alfred::transport::AudioStartInfo& info) {
  g_playback_starts.fetch_add(1, std::memory_order_relaxed);
  Serial.printf("[playback] audio_start seq=%lu sample_rate=%lu ch=%u frame_ms=%u\r\n",
                (unsigned long)info.seq,
                (unsigned long)info.sample_rate,
                (unsigned)info.channels,
                (unsigned)info.frame_ms);
  // Sanity-check the format. Decoder is hard-wired to 16k mono right
  // now; if the gateway ships a different envelope we still try (the
  // decoder is created with kDecodeSampleRateHz/kDecodeChannels) but
  // log a warning so the bench can spot the mismatch.
  if (info.sample_rate != alfred::codec::kDecodeSampleRateHz ||
      info.channels != alfred::codec::kDecodeChannels) {
    Serial.printf("[playback] WARN: envelope says %lu Hz x %u ch but decoder is %lu Hz x %u\r\n",
                  (unsigned long)info.sample_rate, (unsigned)info.channels,
                  (unsigned long)alfred::codec::kDecodeSampleRateHz,
                  (unsigned)alfred::codec::kDecodeChannels);
  }
  if (!alfred::codec::opus_decoder_open()) {
    Serial.println(F("[playback] FAIL: opus_decoder_open"));
  }
}

void onAudioBin(const uint8_t* data, size_t length) {
  if (g_playback_q == nullptr || data == nullptr || length == 0) {
    return;
  }
  if (length > kPlaybackPacketMaxBytes) {
    Serial.printf("[playback] drop oversized packet %u > %u\r\n",
                  (unsigned)length, (unsigned)kPlaybackPacketMaxBytes);
    g_playback_pkts_dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  // Stack-allocate the packet; FreeRTOS copies by value into the queue.
  PlaybackPacket pkt;
  pkt.length = static_cast<uint16_t>(length);
  memcpy(pkt.data, data, length);
  if (xQueueSend(g_playback_q, &pkt, 0) != pdTRUE) {
    // Drop oldest, push new (same policy as encoder outbound).
    PlaybackPacket stale;
    xQueueReceive(g_playback_q, &stale, 0);
    g_playback_pkts_dropped.fetch_add(1, std::memory_order_relaxed);
    xQueueSend(g_playback_q, &pkt, 0);
  }
  g_playback_pkts_queued.fetch_add(1, std::memory_order_relaxed);
}

void onAudioEnd(const alfred::transport::AudioEndInfo& info) {
  g_playback_ends.fetch_add(1, std::memory_order_relaxed);
  Serial.printf("[playback] audio_end seq=%lu gw_frames=%lu gw_bytes=%lu "
                "(local: queued=%lu dropped=%lu decoded=%lu i2s_frames=%lu)\r\n",
                (unsigned long)info.seq,
                (unsigned long)info.frames,
                (unsigned long)info.bytes,
                (unsigned long)g_playback_pkts_queued.load(std::memory_order_relaxed),
                (unsigned long)g_playback_pkts_dropped.load(std::memory_order_relaxed),
                (unsigned long)g_playback_pkts_decoded.load(std::memory_order_relaxed),
                (unsigned long)g_playback_frames_tx.load(std::memory_order_relaxed));
  // Don't close the decoder synchronously — there may still be packets
  // queued behind us that the playbackTask hasn't drained yet. A
  // sentinel zero-length packet enqueued here would let the playback
  // task close at the right boundary, but the simpler equivalent for
  // SAL-4007 is to just leave the session open; the next audio_start
  // will reset it. opus_decoder_open() handles that case.
}

// FreeRTOS task: drain g_playback_q, decode each Opus packet, upsample
// 16k mono → 48k mono linearly, stereo-duplicate + promote int16 → int32,
// push to I2S TX. Pinned to core 1 alongside the encoder so the network
// stack on core 0 never preempts audio (PLAN.md risk #1).
void playbackTask(void* /*arg*/) {
  // Scratch buffers. Sized for the largest single decode call we'll
  // make: kDecodeMaxSamplesPerCall samples = 1920 = 120 ms at 16 kHz.
  // PCM16 mono after decode: 1920 * 2 = 3840 bytes.
  // PCM16 mono after 3x upsample: 5760 samples = 11520 bytes.
  // PCM32 stereo after promote: 5760 * 2 * 4 = 46080 bytes.
  // Total ~62 KB; allocate from internal RAM since the I2S DMA path is
  // hot. We allocate on the heap (not the task stack) for the same
  // reason voice_codec/encoderTask does: keeps the task stack small.
  constexpr size_t kPcmInMax    = alfred::codec::kDecodeMaxSamplesPerCall;
  constexpr size_t kPcmUpMax    = kPcmInMax * 3;
  constexpr size_t kPcmStereoMax = kPcmUpMax * 2;

  int16_t* pcm_in     = (int16_t*)heap_caps_malloc(kPcmInMax * sizeof(int16_t),
                                                   MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  int16_t* pcm_up     = (int16_t*)heap_caps_malloc(kPcmUpMax * sizeof(int16_t),
                                                   MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  int32_t* pcm_stereo = (int32_t*)heap_caps_malloc(kPcmStereoMax * sizeof(int32_t),
                                                   MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (pcm_in == nullptr || pcm_up == nullptr || pcm_stereo == nullptr) {
    Serial.println(F("[playback] FATAL: heap_caps_malloc for decode buffers failed"));
    if (pcm_in)     free(pcm_in);
    if (pcm_up)     free(pcm_up);
    if (pcm_stereo) free(pcm_stereo);
    vTaskDelete(nullptr);
    return;
  }

  Serial.println(F("[playback] task running; draining playback_q -> decode -> "
                   "upsample 3x -> stereo int32 -> I2S TX"));

  // Carry the last input sample across blocks so linear interpolation
  // is continuous at block boundaries.
  int16_t last_in_for_upsample = 0;

  PlaybackPacket pkt;
  for (;;) {
    // Block up to 50 ms waiting for a packet. When idle, this just
    // releases the CPU; the I2S TX side has tx_desc_auto_clear=true so
    // the DMA emits silence on its own.
    if (xQueueReceive(g_playback_q, &pkt, pdMS_TO_TICKS(50)) != pdTRUE) {
      continue;
    }

    size_t samples_decoded = 0;
    const bool ok = alfred::codec::opus_decoder_decode(
        pkt.data, pkt.length,
        pcm_in, kPcmInMax,
        &samples_decoded);
    if (!ok || samples_decoded == 0) {
      continue;
    }
    g_playback_pkts_decoded.fetch_add(1, std::memory_order_relaxed);

    // Upsample 16k → 48k linearly (3x).
    last_in_for_upsample = alfred::audio::upsample_3x_linear_mono(
        pcm_in, samples_decoded, last_in_for_upsample, pcm_up);
    const size_t up_samples = samples_decoded * 3;

    // Stereo-duplicate + promote int16 → int32.
    alfred::audio::mono_int16_to_stereo_int32(pcm_up, up_samples, pcm_stereo);

    // Push to I2S TX. Block up to 50 ms per call — at 48 kHz stereo
    // 5760 samples = 120 ms of audio so DMA should always have room
    // unless the speaker is wedged.
    const size_t got = alfred::audio::i2s_tx_write_block(
        pcm_stereo, up_samples, /*timeout_ms=*/50);
    g_playback_frames_tx.fetch_add(static_cast<uint32_t>(got),
                                   std::memory_order_relaxed);
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  // Give the host's USB-CDC enumeration a moment so we don't lose the banner.
  // Native USB-CDC on ESP32-S3 re-enumerates from scratch after a hard reset,
  // and the host can take 2..3 s to reopen the port. Cap at 3.5 s so we never
  // block forever if the device is running headless.
  const uint32_t deadline = millis() + 3500;
  while (!Serial && millis() < deadline) {
    delay(10);
  }
  // Belt-and-braces: even after Serial is "ready," some hosts buffer the very
  // first writes. Sleep a touch more so the banner survives a fresh open.
  delay(250);

  printBanner();
  runI2cSmokeTest();

  // SAL-4007: enable the carrier's external class-D speaker amp.
  //
  // The TLV320AIC3104 codec on the XVF3800 carrier has only HP and line-out
  // stages -- the loud "5 W" speaker is driven by an external class-D amp.
  // The amp's enable pin is wired to XMOS GPO X0D31 ("Amplifier enable, low
  // = enabled" per Seeed wiki). On the i2s_master test5 firmware, X0D31
  // boots HIGH (amp disabled). We drive it LOW via the XVF control servicer
  // I2C protocol (XMOS at 0x2C, GPO_SERVICER_RESID=20, GPO_WRITE_VALUE=1).
  //
  // Wire format: [RESID, CMDID, PAYLOAD_LEN, PIN_INDEX, VALUE]
  // See formatBCE/Respeaker-XVF3800-ESPHome-integration:
  //   esphome/components/respeaker_xvf3800/respeaker_xvf3800.cpp:390.
  {
    constexpr uint8_t kAddrXmosCtrl       = 0x2C;
    constexpr uint8_t kGpoServicerResid   = 20;
    constexpr uint8_t kGpoWriteValueCmd   = 1;
    constexpr uint8_t kAmpEnablePin       = 31;  // X0D31, active LOW
    delay(50);  // breathing room after smoke probe
    // Drive X0D31 LOW (amp enable) AND X0D33 LOW (likely mute, since on
    // boot it's the only GPO HIGH on the test5 firmware). Best-guess unmute.
    auto write_gpo = [](uint8_t pin, uint8_t val) {
      uint8_t pl[] = {kGpoServicerResid, kGpoWriteValueCmd, 2, pin, val};
      Wire.beginTransmission(kAddrXmosCtrl);
      Wire.write(pl, sizeof(pl));
      uint8_t e = Wire.endTransmission();
      Serial.printf("[xvf] GPO write pin=%u value=%u rc=%u\r\n",
                    (unsigned)pin, (unsigned)val, (unsigned)e);
      delay(20);
    };
    write_gpo(kAmpEnablePin, 0);  // X0D31 LOW = amp ENABLED
    write_gpo(33, 0);             // X0D33 LOW = best-guess UNMUTE
    delay(20);

    // Bump TLV320AIC3104 DAC + LEFT_LOP/RIGHT_LOP output to 0 dB unmuted.
    // Per the codec-init research (CODEC_INIT_RESEARCH.md), if XMOS already
    // inits the codec it likely leaves the line-out at default (often muted
    // or heavily attenuated). Override here to ensure full signal to amp.
    //   Reg 43 (Left DAC vol)        = 0x00 -> 0 dB
    //   Reg 44 (Right DAC vol)       = 0x00 -> 0 dB
    //   Reg 86 (LEFT_LOP/M output)   = 0x09 -> 0 dB, unmute, power on
    //   Reg 93 (RIGHT_LOP/M output)  = 0x09 -> 0 dB, unmute, power on
    //   Reg 51 (HPLOUT level)        = 0x09 -> 0 dB, unmute, power on
    //   Reg 65 (HPROUT level)        = 0x09 -> 0 dB, unmute, power on
    constexpr uint8_t kAddrCodec = 0x18;
    auto codec_write = [](uint8_t reg, uint8_t val) {
      Wire.beginTransmission(kAddrCodec);
      Wire.write(reg);
      Wire.write(val);
      uint8_t e = Wire.endTransmission();
      Serial.printf("[codec] write reg=0x%02X val=0x%02X rc=%u\r\n",
                    (unsigned)reg, (unsigned)val, (unsigned)e);
    };
    // Page select 0 first (defensive).
    codec_write(0x00, 0x00);
    codec_write(0x2B, 0x00);  // Left DAC vol 0 dB
    codec_write(0x2C, 0x00);  // Right DAC vol 0 dB
    codec_write(0x56, 0x09);  // LEFT_LOP/M  0 dB, unmute, power on
    codec_write(0x5D, 0x09);  // RIGHT_LOP/M 0 dB, unmute, power on
    codec_write(0x33, 0x09);  // HPLOUT       0 dB, unmute, power on
    codec_write(0x41, 0x09);  // HPROUT       0 dB, unmute, power on
    delay(50);
    // Read all 5 GPO statuses so we can see what XMOS thinks the world looks
    // like. Order returned: status, X0D11, X0D30, X0D31, X0D33, X0D39.
    constexpr uint8_t kGpoReadValuesCmd  = 0;
    constexpr uint8_t kGpoReadCount      = 5;
    constexpr uint8_t kGpoReadRespBytes  = kGpoReadCount + 1;  // status byte + 5 pins
    uint8_t read_req[] = {kGpoServicerResid,
                          (uint8_t)(kGpoReadValuesCmd | 0x80),  // read bit
                          kGpoReadRespBytes};
    Wire.beginTransmission(kAddrXmosCtrl);
    Wire.write(read_req, sizeof(read_req));
    uint8_t err_w = Wire.endTransmission(false);  // repeated start
    uint8_t resp[kGpoReadRespBytes] = {0};
    uint8_t got = Wire.requestFrom((uint8_t)kAddrXmosCtrl,
                                   (uint8_t)kGpoReadRespBytes, (uint8_t)true);
    for (uint8_t i = 0; i < got && i < sizeof(resp); ++i) {
      resp[i] = Wire.read();
    }
    Serial.printf("[xvf] GPO read: write_rc=%u got=%u status=0x%02X "
                  "X0D11=%u X0D30=%u X0D31=%u X0D33=%u X0D39=%u\r\n",
                  (unsigned)err_w, (unsigned)got,
                  (unsigned)resp[0], (unsigned)resp[1], (unsigned)resp[2],
                  (unsigned)resp[3], (unsigned)resp[4], (unsigned)resp[5]);
  }

  // Bring up I2S RX. If this fails, we still want the heartbeat loop running
  // so the bench can see the firmware is alive and trigger a re-flash; just
  // skip spawning the audio + encoder tasks in that case.
  const bool audio_ok = alfred::audio::audio_io_init();
  if (!audio_ok) {
    Serial.println(F("[audio] FAIL: audio_io_init returned false; encoder NOT spawned."));
    g_last_heartbeat_ms = millis();
    return;
  }
  Serial.println(F("[audio] audio_io_init OK."));

  // SAL-4007: bring up I2S TX (duplex slave RX+TX) immediately after
  // audio_io_init() and BEFORE the encoder task spawns. i2s_tx_init()
  // calls i2s_driver_uninstall + reinstall to flip the port from RX-only
  // to RX+TX; if the encoder task is already running, its in-flight
  // i2s_read() collides with the uninstall and trips the IDF spinlock
  // assert (`spinlock_acquire ... result == core_id || SPINLOCK_FREE`,
  // verified on the first run after this code landed). Doing the
  // duplex flip here, before any task is consuming I2S, sequences the
  // driver state cleanly and the encoder/playback tasks both see the
  // final RX+TX configuration on first read/write.
  if (!alfred::audio::i2s_tx_init()) {
    Serial.println(F("[i2s_tx] FAIL: i2s_tx_init returned false; playback degraded."));
  } else {
    Serial.println(F("[i2s_tx] i2s_tx_init OK (duplex slave RX+TX on I2S0)."));
#ifdef ALFRED_BOOT_TEST_TONE_HZ
    // Boot-time speaker check: synthesize a 1-second sine wave through the
    // I2S TX path. Validates GPIO44 -> codec -> speaker electrical chain
    // without needing WiFi / WS / gateway. Only compiled in if
    // ALFRED_BOOT_TEST_TONE_HZ is defined in secrets.ini build_flags.
    {
      const float freq_hz = (float)(ALFRED_BOOT_TEST_TONE_HZ);
      const int kSampleRate = 48000;
      const int kBlockFrames = 480;       // 10 ms blocks
      const int kTotalFrames = kSampleRate * 2;  // 2 seconds
      const float two_pi_f_over_sr = 2.0f * 3.14159265358979f * freq_hz / (float)kSampleRate;
      const int16_t amp = 15000;          // ~-7 dBFS, audible-not-painful
      static int32_t block[480 * 2];      // stereo
      Serial.printf("[boot_tone] playing %.0f Hz for 1 s through I2S TX...\r\n", freq_hz);
      for (int frame_off = 0; frame_off < kTotalFrames; frame_off += kBlockFrames) {
        for (int i = 0; i < kBlockFrames; ++i) {
          float phase = two_pi_f_over_sr * (float)(frame_off + i);
          int16_t s = (int16_t)(amp * sinf(phase));
          int32_t s32 = ((int32_t)s) << 16;
          block[i * 2 + 0] = s32;
          block[i * 2 + 1] = s32;
        }
        alfred::audio::i2s_tx_write_block(block, kBlockFrames, /*timeout_ms=*/100);
      }
      Serial.println(F("[boot_tone] done."));
    }
#endif
  }

  const bool codec_ok = alfred::codec::voice_codec_init();
  if (!codec_ok) {
    Serial.println(F("[codec] FAIL: voice_codec_init returned false; encoder NOT spawned."));
    g_last_heartbeat_ms = millis();
    return;
  }
  Serial.println(F("[codec] voice_codec_init OK."));

  // Allocate outbound queue. Queue items are EncodedFrame by-value (~210 B)
  // so 100 entries cost ~21 KB of internal RAM. uxQueueCreate on Arduino-
  // ESP32 allocates from internal RAM by default which is what we want for
  // hot-path data; PSRAM access is too slow for per-frame work.
  g_outbound_q = xQueueCreate(kOutboundQueueDepth,
                              sizeof(alfred::codec::EncodedFrame));
  if (g_outbound_q == nullptr) {
    Serial.println(F("[encoder] FAIL: xQueueCreate returned NULL; encoder NOT spawned."));
    g_last_heartbeat_ms = millis();
    return;
  }
  Serial.printf("[encoder] outbound queue: %u entries x %u bytes = %u bytes\r\n",
                (unsigned)kOutboundQueueDepth,
                (unsigned)sizeof(alfred::codec::EncodedFrame),
                (unsigned)(kOutboundQueueDepth * sizeof(alfred::codec::EncodedFrame)));

  BaseType_t rc;
  rc = xTaskCreatePinnedToCore(
      encoderTask, "voice_enc",
      /*stack=*/32 * 1024,        // opus_encode in FIXED_POINT mode allocates
                                   // large autocorrelation / LPC working
                                   // buffers on the stack. Empirically a
                                   // 16 KB stack tripped the canary on the
                                   // first encode_block call (see
                                   // sal_4001_first_run.log). 32 KB is the
                                   // sh123/esp32_opus example task size and
                                   // is the documented safe value.
      /*arg=*/nullptr,
      /*priority=*/5,            // Above idle, below Wi-Fi (which is 23).
      /*handle=*/nullptr,
      /*core=*/1);
  if (rc != pdPASS) {
    Serial.println(F("[encoder] FAIL: xTaskCreatePinnedToCore returned !pdPASS."));
  }
  // SAL-4007: allocate the playback queue + spawn the playbackTask.
  // i2s_tx_init() already ran above (before the encoder task) so the
  // driver is in duplex mode by now. The playback queue + task are
  // independent of the actual I2S TX init result; if TX init failed
  // we still boot and the heartbeat surfaces it.
  g_playback_q = xQueueCreate(kPlaybackQueueDepth, sizeof(PlaybackPacket));
  if (g_playback_q == nullptr) {
    Serial.println(F("[playback] FAIL: xQueueCreate returned NULL; playback disabled."));
  } else {
    Serial.printf("[playback] queue: %u entries x %u bytes = %u bytes\r\n",
                  (unsigned)kPlaybackQueueDepth,
                  (unsigned)sizeof(PlaybackPacket),
                  (unsigned)(kPlaybackQueueDepth * sizeof(PlaybackPacket)));

    // Spawn playbackTask BEFORE wiring callbacks so the first incoming
    // audio_start has a queue + drain ready. Stack: 12 KB. opus_decode
    // in FIXED_POINT mode is lighter than encode (no autocorrelation /
    // LPC search) so 12 KB is comfortable; 16 KB if anything fluctuates.
    BaseType_t prc = xTaskCreatePinnedToCore(
        playbackTask, "voice_play",
        /*stack=*/16 * 1024,
        /*arg=*/nullptr,
        /*priority=*/4,        // Below encoder (5), above transport (3) so
                                // playback drains the queue ahead of WS.
        /*handle=*/nullptr,
        /*core=*/1);            // Same core as encoder; both audio paths
                                 // share core 1, network stays on core 0.
    if (prc != pdPASS) {
      Serial.println(F("[playback] FAIL: xTaskCreatePinnedToCore !pdPASS"));
    }
  }

  // Register transport audio sink callbacks BEFORE transport_init so
  // any audio frames that arrive during the first WS connect race land
  // in the playback path. Idempotent and lock-free; safe pre-init.
  alfred::transport::transport_set_audio_callbacks(
      onAudioStart, onAudioBin, onAudioEnd);

  // SAL-4006: bring up Wi-Fi STA + WebSocket client. The transport
  // module owns its own task (voice_ws on core 0) and pulls
  // EncodedFrame items out of g_outbound_q. The setup is fire-and-
  // forget; Wi-Fi association and WS connect happen asynchronously,
  // and the encoder task keeps producing into the queue regardless of
  // network state. If transport_init returns false the firmware still
  // boots and the heartbeat keeps running so the bench can see what
  // went wrong.
  if (!alfred::transport::transport_init(g_outbound_q)) {
    Serial.println(F("[transport] FAIL: transport_init returned false; running offline."));
  }

  g_last_heartbeat_ms = millis();
  Serial.printf("[boot] entering loop, heartbeat every %lu ms\r\n",
                (unsigned long)kHeartbeatIntervalMs);
}

void loop() {
  const uint32_t now = millis();
  if (now - g_last_heartbeat_ms >= kHeartbeatIntervalMs) {
    g_last_heartbeat_ms = now;
    printHeartbeat();
  }
  delay(50);
}
