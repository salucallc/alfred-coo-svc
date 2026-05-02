// Alfred voice puck firmware (skeleton).
//
// Ticket: SAL-3999 (P1.1, ESP-IDF / arduino-esp32 project skeleton).
// Hardware: Seeed XIAO ESP32-S3 mounted on Seeed ReSpeaker XVF3800 carrier.
// Toolchain: PlatformIO + arduino-esp32 framework (see platformio.ini).
//
// What this skeleton proves:
//   1. The XIAO boots and talks over its native USB-CDC serial.
//   2. PSRAM is enabled and visible (non-zero ESP.getFreePsram()).
//   3. The custom huge_app partition table (3 MB app slot) is honored.
//   4. The XVF3800 control servicer at I2C 0x2C still ACKs from firmware
//      (matches the bench result captured in p0_i2c.log).
//
// Future Phase 1 tickets plug in here:
//   * SAL-4000 (I2S RX from XVF3800)         -> add an audio_io component
//   * SAL-4001 (Opus encode + ring buffer)   -> add a codec component
//   * SAL-4003 (WebSocket client to gateway) -> add a transport component

#include <Arduino.h>
#include <Wire.h>
#include <esp_chip_info.h>
#include <esp_mac.h>
#include <esp_system.h>

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
  Serial.printf("[heartbeat seq=%lu uptime=%lus heap=%lu psram=%lu]\r\n",
                (unsigned long)g_heartbeat_seq,
                (unsigned long)(millis() / 1000UL),
                (unsigned long)ESP.getFreeHeap(),
                (unsigned long)ESP.getFreePsram());
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
