// Alfred voice puck firmware.
//
// Tickets:
//   SAL-3999 (P1.1): project skeleton, USB-CDC, I2C smoke test, heartbeat.
//   SAL-4000 (P1.2): I2S slave RX from XVF3800 + channel-0 RMS log.
//   SAL-4001 (P1.3): Opus voice encode (16 kHz mono 20 ms 24 kbps VBR) +
//                    outbound EncodedFrame queue + drain logger task.
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
// Task topology (as of SAL-4001):
//   * loopTask (Arduino, core 0)        : heartbeats every 5 s.
//   * encoderTask (core 1, prio 5)      : drains audio_io blocks, encodes,
//                                          pushes EncodedFrame to outbound q.
//   * drainTask (core 0, prio 4)        : pops EncodedFrame from outbound q,
//                                          logs rolling stats every 1 s.
//                                          Will be replaced by the WS sender
//                                          in SAL-4006 (see README).
//
// Why these pinnings: PLAN.md risk #1 says Opus on ESP32-S3 may starve I2S
// DMA. We pin both the I2S DMA reader and the encoder to core 1 so they
// share a CPU and never get preempted by the Wi-Fi/network stack which
// always runs on core 0. The drain logger lives on core 0 because it does
// nearly no work and is naturally where the WS client will land.
//
// Future Phase 1 / Phase 2 tickets plug in here:
//   * SAL-4002/4003 (WS client to gateway) -> swap drainTask for WS sender
//   * SAL-4014 (I2S TX + Opus decode)      -> add a second I2S DMA + decoder

#include <Arduino.h>
#include <Wire.h>
#include <atomic>
#include <esp_chip_info.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include "audio_io.h"
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

// Drain logger cadence: one summary line per second (50 frames/s expected).
constexpr uint32_t kDrainLogIntervalMs = 1000;

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

uint32_t g_last_heartbeat_ms = 0;
uint32_t g_heartbeat_seq = 0;

// Outbound encoded-frame queue. encoderTask pushes; drainTask pops.
QueueHandle_t g_outbound_q = nullptr;

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
  Serial.printf("[heartbeat seq=%lu uptime=%lus heap=%lu psram=%lu "
                "audio_frames=%llu under=%lu opus_frames=%llu opus_bytes=%llu "
                "max_enc_us=%lu over=%lu]\r\n",
                (unsigned long)g_heartbeat_seq,
                (unsigned long)(millis() / 1000UL),
                (unsigned long)ESP.getFreeHeap(),
                (unsigned long)ESP.getFreePsram(),
                (unsigned long long)a.frames_in,
                (unsigned long)a.underruns,
                (unsigned long long)c.frames_out,
                (unsigned long long)c.bytes_out,
                (unsigned long)c.max_encode_us,
                (unsigned long)c.encode_overruns);
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

// Drain the outbound queue, log a one-line summary every kDrainLogIntervalMs.
// Pinned to core 0. SAL-4006 will replace the body with a WebSocket sender
// (see README.md "Future hooks").
void drainTask(void* /*arg*/) {
  alfred::codec::EncodedFrame frame;
  uint32_t window_start_ms = millis();
  uint32_t window_frames = 0;
  uint32_t window_bytes = 0;
  uint16_t window_min_bytes = 0xFFFF;
  uint16_t window_max_bytes = 0;

  Serial.println(F("[drain] task running; will log Opus frame stats every 1 s."));

  for (;;) {
    if (xQueueReceive(g_outbound_q, &frame, pdMS_TO_TICKS(kDrainLogIntervalMs))
        == pdTRUE) {
      ++window_frames;
      window_bytes += frame.length;
      if (frame.length < window_min_bytes) window_min_bytes = frame.length;
      if (frame.length > window_max_bytes) window_max_bytes = frame.length;
    }

    const uint32_t now = millis();
    if (now - window_start_ms >= kDrainLogIntervalMs) {
      alfred::codec::CodecStats c = alfred::codec::voice_codec_get_stats();
      const uint32_t elapsed = now - window_start_ms;
      const float fps = (1000.0f * window_frames) / elapsed;
      const float bps = (8000.0f * window_bytes) / elapsed;  // bits/sec
      const float avg = window_frames > 0
                            ? (1.0f * window_bytes) / window_frames
                            : 0.0f;
      const uint16_t lo = (window_min_bytes == 0xFFFF) ? 0 : window_min_bytes;
      Serial.printf("[opus seq=%lu fps=%.1f avg=%.1f min=%u max=%u bps=%.0f "
                    "qd=%u/%u tot_frames=%llu tot_bytes=%llu enc_us_max=%lu "
                    "enc_overruns=%lu enc_errors=%lu]\r\n",
                    (unsigned long)frame.seq, fps, avg, lo, window_max_bytes,
                    bps,
                    (unsigned int)uxQueueMessagesWaiting(g_outbound_q),
                    (unsigned int)kOutboundQueueDepth,
                    (unsigned long long)c.frames_out,
                    (unsigned long long)c.bytes_out,
                    (unsigned long)c.max_encode_us,
                    (unsigned long)c.encode_overruns,
                    (unsigned long)c.encode_errors);
      window_start_ms = now;
      window_frames = 0;
      window_bytes = 0;
      window_min_bytes = 0xFFFF;
      window_max_bytes = 0;
    }
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
  rc = xTaskCreatePinnedToCore(
      drainTask, "voice_drain",
      /*stack=*/4 * 1024,
      /*arg=*/nullptr,
      /*priority=*/4,            // Below the encoder so encoder always wins on contention.
      /*handle=*/nullptr,
      /*core=*/0);
  if (rc != pdPASS) {
    Serial.println(F("[drain] FAIL: xTaskCreatePinnedToCore returned !pdPASS."));
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
