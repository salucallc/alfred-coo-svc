// Alfred voice puck firmware.
//
// Tickets:
//   SAL-3999 (P1.1): project skeleton, USB-CDC, I2C smoke test, heartbeat.
//   SAL-4000 (P1.2): I2S slave RX from XVF3800 + channel-0 RMS log.
//
// Hardware: Seeed XIAO ESP32-S3 mounted on Seeed ReSpeaker XVF3800 carrier.
// Toolchain: PlatformIO + arduino-esp32 framework (see platformio.ini).
//
// What this firmware proves:
//   1. The XIAO boots and talks over its native USB-CDC serial.
//   2. PSRAM is enabled and visible (non-zero ESP.getFreePsram()).
//   3. The custom huge_app partition table (3 MB app slot) is honored.
//   4. The XVF3800 control servicer at I2C 0x2C still ACKs from firmware
//      (matches the bench result captured in p0_i2c.log).
//   5. The XVF3800 (i2s_master test5 firmware) is driving I2S into the
//      XIAO, the XIAO is consuming it as I2S slave RX, and channel-0 RMS
//      tracks acoustic energy in the room (rises with speech, drops back
//      to noise floor in silence).
//
// Future Phase 1 tickets plug in here:
//   * SAL-4001 (Opus encode + ring buffer)   -> add a codec component
//   * SAL-4003 (WebSocket client to gateway) -> add a transport component

#include <Arduino.h>
#include <Wire.h>
#include <esp_chip_info.h>
#include <esp_mac.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "audio_io.h"

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

// Audio task cadence: log RMS every 500 ms, read 20 ms blocks (matches the
// Phase 1 Opus framing target so SAL-4001 can hook straight in).
constexpr uint32_t kAudioRmsLogIntervalMs = 500;
constexpr size_t   kAudioBlockFrames = (alfred::audio::kSampleRateHz / 1000) * 20;

uint32_t g_last_heartbeat_ms = 0;
uint32_t g_heartbeat_seq = 0;

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
  alfred::audio::AudioStats s = alfred::audio::audio_io_get_stats();
  Serial.printf("[heartbeat seq=%lu uptime=%lus heap=%lu psram=%lu "
                "audio_frames=%llu underruns=%lu errors=%lu]\r\n",
                (unsigned long)g_heartbeat_seq,
                (unsigned long)(millis() / 1000UL),
                (unsigned long)ESP.getFreeHeap(),
                (unsigned long)ESP.getFreePsram(),
                (unsigned long long)s.frames_in,
                (unsigned long)s.underruns,
                (unsigned long)s.read_errors);
}

// FreeRTOS task: continually drain the I2S DMA into a stack-allocated block,
// have audio_io compute RMS / peak on channel 0, and log a one-line summary
// every kAudioRmsLogIntervalMs. Pinned to core 1 so it never competes with
// the Arduino main loop on core 0 and so future networking traffic on core 0
// (Wi-Fi + WebSocket) cannot starve audio capture.
void audioTask(void* /*arg*/) {
  // 20 ms stereo at 48 kHz = 960 frames * 2 ch * 4 bytes = 7680 bytes.
  // Stays well under the 16 KB task stack we provision below.
  static int32_t block[kAudioBlockFrames * alfred::audio::kChannelCount];

  uint32_t last_log_ms = millis();
  uint32_t blocks_since_log = 0;
  for (;;) {
    const size_t got = alfred::audio::audio_io_read_block(
        block, kAudioBlockFrames, /*timeout_ms=*/100);
    if (got == 0) {
      // Brief sleep on a starvation path so we don't spin a core.
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    ++blocks_since_log;

    const uint32_t now = millis();
    if (now - last_log_ms >= kAudioRmsLogIntervalMs) {
      alfred::audio::AudioStats s = alfred::audio::audio_io_get_stats();
      Serial.printf("[audio rms_ch0=%.1f dBFS peak_ch0=%.1f dBFS "
                    "blocks=%lu/%lums frames=%llu under=%lu]\r\n",
                    s.rms_ch0_dbfs, s.peak_ch0_dbfs,
                    (unsigned long)blocks_since_log,
                    (unsigned long)(now - last_log_ms),
                    (unsigned long long)s.frames_in,
                    (unsigned long)s.underruns);
      last_log_ms = now;
      blocks_since_log = 0;
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
  // skip spawning the audio task in that case.
  const bool audio_ok = alfred::audio::audio_io_init();
  if (audio_ok) {
    Serial.println(F("[audio] audio_io_init OK; spawning capture task on core 1."));
    BaseType_t rc = xTaskCreatePinnedToCore(
        audioTask, "audio_rx",
        /*stack=*/16 * 1024,
        /*arg=*/nullptr,
        /*priority=*/5,           // Above idle, below Wi-Fi (which is 23).
        /*handle=*/nullptr,
        /*core=*/1);
    if (rc != pdPASS) {
      Serial.println(F("[audio] FAIL: xTaskCreatePinnedToCore returned !pdPASS."));
    }
  } else {
    Serial.println(F("[audio] FAIL: audio_io_init returned false; capture task not spawned."));
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
