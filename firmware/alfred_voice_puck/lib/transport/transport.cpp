// SAL-4006 (P1.6) transport: Wi-Fi STA + WebSocket client to the
// alfred_coo.fleet_voice gateway. See transport.h for the contract.

#include "transport.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <WebSocketsClient.h>
#include <esp_mac.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include "voice_codec.h"

// All of the gateway addressing comes from build_flags via secrets.ini.
// Sanity-check that the build is wired up correctly so a missing
// secrets.ini fails compile rather than silently shipping junk strings.
#ifndef ALFRED_WIFI_SSID
#error "ALFRED_WIFI_SSID must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_WIFI_PASSWORD
#error "ALFRED_WIFI_PASSWORD must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_GATEWAY_HOST
#error "ALFRED_GATEWAY_HOST must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_GATEWAY_PORT
#error "ALFRED_GATEWAY_PORT must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_GATEWAY_PATH
#error "ALFRED_GATEWAY_PATH must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_GATEWAY_BEARER
#error "ALFRED_GATEWAY_BEARER must be defined via secrets.ini build_flags"
#endif
#ifndef ALFRED_GATEWAY_TLS
#define ALFRED_GATEWAY_TLS 0
#endif

// Optional secondary SSID. Multi-AP fallback via WiFiMulti.
// Define ALFRED_WIFI_SSID_2 + ALFRED_WIFI_PASSWORD_2 in secrets.ini to enable.

namespace alfred {
namespace transport {

namespace {

// Backoff bounds. We retry Wi-Fi and WS reconnects with exponential
// growth capped at 16 s so a long outage doesn't burn the CPU on
// reconnect storms but a brief blip recovers in ~250 ms.
constexpr uint32_t kBackoffMinMs = 250;
constexpr uint32_t kBackoffMaxMs = 16000;

// How long to wait for Wi-Fi association after .begin() before declaring
// the attempt failed. 12 s is a generous WPA2 + DHCP budget on a
// healthy AP; if your AP is slow, bump this.
constexpr uint32_t kWifiAssociateTimeoutMs = 12000;

// Outbound queue receive timeout. We tick ws.loop() at this cadence so
// inbound frames stay responsive even if the encoder produces nothing
// (e.g. DTX silence frames are still emitted, but if the encoder
// stalled, we still want to service the WS).
constexpr TickType_t kQueueWaitTicks = pdMS_TO_TICKS(20);

// Outbound queue we drain. Owned by main.cpp; we just hold a pointer.
QueueHandle_t g_outbound_q = nullptr;

// Singleton WS client. links2004/WebSockets requires single-threaded
// access; the transport task is the sole owner.
WebSocketsClient g_ws;

// Live stats.
TransportStats g_stats = {};

// Hello-frame seq counter (we use 0 for the first hello, then increment
// for any subsequent client-driven control frames; pong frames echo
// the server's seq so they don't share this counter).
uint32_t g_client_seq = 0;

// The device id baked into the hello frame; "alfred-puck-<mac>".
String g_device_id;

// Whether we've sent the hello on the current WS session yet. Reset
// on every disconnect.
bool g_hello_sent = false;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

String makeDeviceId() {
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char buf[40] = {0};
  snprintf(buf, sizeof(buf), "alfred-puck-%02x%02x%02x%02x%02x%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(buf);
}

// JSON helpers that build the few control frames we emit. Hand-rolled
// rather than pulling in ArduinoJson because the payloads are tiny and
// fixed-shape; saves ~25 KB of flash.
String helloJson() {
  // Schema matches the SAL-4006 brief: fixed audio descriptor so the
  // gateway can validate the device's codec at connect time.
  String s;
  s.reserve(256);
  s += "{\"type\":\"hello\",\"seq\":";
  s += g_client_seq++;
  s += ",\"device_id\":\"";
  s += g_device_id;
  s += "\",\"audio\":{\"format\":\"opus\",\"sample_rate\":16000,\"channels\":1,\"frame_ms\":20},\"fw_version\":\"";
  s += ALFRED_FW_VERSION;
  s += "\"}";
  return s;
}

String pongJson(int seq) {
  String s;
  s.reserve(48);
  s += "{\"type\":\"pong\",\"seq\":";
  s += seq;
  s += "}";
  return s;
}

// Quick-and-dirty integer parse for `"seq": <int>` inside a JSON payload.
// We don't ship ArduinoJson; the only field we actually need to decode
// inbound is `seq` for ping responses, and `type` for routing. A loose
// substring scan is fine here because the gateway only sends well-
// formed JSON. Returns -1 on failure.
int extractIntField(const String& json, const char* field) {
  String key = "\"";
  key += field;
  key += "\"";
  int i = json.indexOf(key);
  if (i < 0) return -1;
  // Skip past the key and whitespace + colon.
  i = json.indexOf(':', i + key.length());
  if (i < 0) return -1;
  ++i;
  while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) ++i;
  // Read digits.
  int sign = 1;
  if (i < (int)json.length() && json[i] == '-') { sign = -1; ++i; }
  int val = 0;
  bool any = false;
  while (i < (int)json.length() && json[i] >= '0' && json[i] <= '9') {
    val = val * 10 + (json[i] - '0');
    any = true;
    ++i;
  }
  return any ? sign * val : -1;
}

// Extract a string field's value (no escape handling beyond the minimum;
// the gateway emits compact JSON without weird escapes for `type`).
String extractStringField(const String& json, const char* field) {
  String key = "\"";
  key += field;
  key += "\"";
  int i = json.indexOf(key);
  if (i < 0) return String();
  i = json.indexOf(':', i + key.length());
  if (i < 0) return String();
  i = json.indexOf('"', i + 1);
  if (i < 0) return String();
  int j = json.indexOf('"', i + 1);
  if (j < 0) return String();
  return json.substring(i + 1, j);
}

// ---------------------------------------------------------------------------
// WebSocket event handler. Runs on the transport task thread.
// ---------------------------------------------------------------------------

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      if (g_stats.ws_connected) {
        ++g_stats.ws_disconnect_count;
      }
      g_stats.ws_connected = false;
      g_hello_sent = false;
      Serial.println(F("[ws] disconnected"));
      break;

    case WStype_CONNECTED: {
      g_stats.ws_connected = true;
      ++g_stats.ws_connect_count;
      // payload is the URL the client connected to (informational).
      Serial.printf("[ws] connected to %s://%s:%d%s\r\n",
                    ALFRED_GATEWAY_TLS ? "wss" : "ws",
                    ALFRED_GATEWAY_HOST,
                    (int)ALFRED_GATEWAY_PORT,
                    ALFRED_GATEWAY_PATH);
      // Send hello so the gateway registers the session and we get a
      // welcome frame back in the log. We don't gate audio on the
      // welcome; the gateway buffers binary frames before welcome too.
      String hello = helloJson();
      if (g_ws.sendTXT(hello)) {
        g_hello_sent = true;
        Serial.printf("[ws] hello sent: %s\r\n", hello.c_str());
      } else {
        Serial.println(F("[ws] hello sendTXT FAILED"));
      }
      break;
    }

    case WStype_TEXT: {
      ++g_stats.frames_recv_text;
      // Always log inbound text frames so the bench can see the full
      // server-side conversation. They're tiny (control plane).
      String body((char*)payload, length);
      Serial.printf("[ws] rx text len=%u: %s\r\n",
                    (unsigned)length, body.c_str());
      String typ = extractStringField(body, "type");
      if (typ == "ping") {
        ++g_stats.pings_recv;
        int seq = extractIntField(body, "seq");
        if (seq < 0) seq = 0;
        String pong = pongJson(seq);
        g_ws.sendTXT(pong);
      } else if (typ == "transcript") {
        ++g_stats.transcripts_recv;
      } else if (typ == "assistant_text") {
        ++g_stats.assistant_text_recv;
      }
      // welcome / ack / audio_start / audio_end / error: just logged
      // above. Nothing else to do device-side for SAL-4006.
      break;
    }

    case WStype_BIN:
      ++g_stats.frames_recv_binary;
      g_stats.bytes_recv_binary += length;
      // SAL-4007 will Opus-decode + I2S-TX these; for now just log.
      Serial.printf("[ws] rx binary len=%u\r\n", (unsigned)length);
      break;

    case WStype_PING:
      // links2004 lib auto-pongs WS protocol pings; nothing for us to do.
      break;

    case WStype_PONG:
      break;

    case WStype_ERROR:
      Serial.printf("[ws] error: %.*s\r\n", (int)length, (char*)payload);
      break;

    default:
      break;
  }
}

// ---------------------------------------------------------------------------
// Wi-Fi lifecycle
// ---------------------------------------------------------------------------

bool wifiAssociate() {
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.disconnect(true, true);  // Drop any stale state from a previous attempt.
  delay(50);

  // Enable WPA3 SAE auth on top of the default WPA2-PSK so we associate with
  // pure-WPA3 APs (e.g. Cristian's "saluca" network). Without this the
  // arduino-esp32 v2.x default rejects WPA3-only APs with status=4.
  // Using esp_wifi_sta_pmf_required + setting auth_threshold to WPA2/WPA3.
  WiFi.setMinSecurity(WIFI_AUTH_WPA2_PSK);  // Allow WPA2/WPA3 mixed and pure WPA3.

  // WiFiMulti picks the AP with the strongest RSSI among configured SSIDs.
  // Add primary; secondary is added only if defined in secrets.ini.
  static WiFiMulti wifiMulti;
  static bool registered = false;
  if (!registered) {
    wifiMulti.addAP(ALFRED_WIFI_SSID, ALFRED_WIFI_PASSWORD);
    Serial.printf("[wifi] registered ssid=\"%s\"\r\n", ALFRED_WIFI_SSID);
#if defined(ALFRED_WIFI_SSID_2) && defined(ALFRED_WIFI_PASSWORD_2)
    wifiMulti.addAP(ALFRED_WIFI_SSID_2, ALFRED_WIFI_PASSWORD_2);
    Serial.printf("[wifi] registered ssid=\"%s\" (fallback)\r\n", ALFRED_WIFI_SSID_2);
#endif
    registered = true;
  }

  Serial.printf("[wifi] associating (multi-AP, WPA2/WPA3)...\r\n");
  const uint32_t deadline = millis() + kWifiAssociateTimeoutMs;
  while (wifiMulti.run(200) != WL_CONNECTED && millis() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[wifi] FAILED to associate within %lu ms (status=%d)\r\n",
                  (unsigned long)kWifiAssociateTimeoutMs, (int)WiFi.status());
    return false;
  }

  ++g_stats.wifi_connect_count;
  g_stats.wifi_connected = true;
  g_stats.wifi_rssi_dbm = abs(WiFi.RSSI());
  Serial.printf("[wifi] connected ssid=\"%s\" ip=%s rssi=%d dBm channel=%d\r\n",
                WiFi.SSID().c_str(),
                WiFi.localIP().toString().c_str(),
                (int)WiFi.RSSI(),
                (int)WiFi.channel());
  return true;
}

// ---------------------------------------------------------------------------
// WebSocket bring-up
// ---------------------------------------------------------------------------

void wsBegin() {
  // Build the auth header. links2004 takes a single header string; we
  // only need Authorization for SAL-4006.
  String headers = "Authorization: Bearer ";
  headers += ALFRED_GATEWAY_BEARER;

  g_ws.setExtraHeaders(headers.c_str());
  // Reconnect interval inside the library; we run our own outer loop
  // too but this keeps brief blips from forcing a full re-init.
  g_ws.setReconnectInterval(2000);
  // Heartbeat: send WS protocol pings every 15s and tear down if no
  // pong in 5s. Belt-and-braces against silent socket death.
  g_ws.enableHeartbeat(15000, 5000, 2);

#if ALFRED_GATEWAY_TLS
  g_ws.beginSSL(ALFRED_GATEWAY_HOST,
                ALFRED_GATEWAY_PORT,
                ALFRED_GATEWAY_PATH);
#else
  g_ws.begin(ALFRED_GATEWAY_HOST,
             ALFRED_GATEWAY_PORT,
             ALFRED_GATEWAY_PATH);
#endif
  g_ws.onEvent(onWsEvent);

  Serial.printf("[ws] bring-up target=%s://%s:%d%s\r\n",
                ALFRED_GATEWAY_TLS ? "wss" : "ws",
                ALFRED_GATEWAY_HOST,
                (int)ALFRED_GATEWAY_PORT,
                ALFRED_GATEWAY_PATH);
}

// ---------------------------------------------------------------------------
// Outbound pump
// ---------------------------------------------------------------------------

// Pop one EncodedFrame (waiting up to kQueueWaitTicks) and ship it as
// a WS binary message. If the WS is not connected we still pop (so the
// queue doesn't grow unbounded behind a dead link) and bump the
// dropped-offline counter; the encoder's drop-oldest semantics already
// keep audio fresh on reconnect.
void pumpOnce() {
  alfred::codec::EncodedFrame frame;
  if (xQueueReceive(g_outbound_q, &frame, kQueueWaitTicks) != pdTRUE) {
    return;
  }
  if (!g_stats.ws_connected) {
    ++g_stats.frames_dropped_offline;
    return;
  }
  // links2004 sendBIN takes (uint8_t*, size_t).
  if (!g_ws.sendBIN(frame.data, frame.length)) {
    ++g_stats.ws_send_failures;
    return;
  }
  ++g_stats.frames_sent;
  g_stats.bytes_sent += frame.length;
}

// ---------------------------------------------------------------------------
// Transport task
// ---------------------------------------------------------------------------

void transportTask(void* /*arg*/) {
  uint32_t backoff_ms = kBackoffMinMs;

  Serial.printf("[transport] task running; device_id=%s\r\n", g_device_id.c_str());

  // First Wi-Fi attempt up front, then WS bring-up. Subsequent failures
  // fall through to the loop where we monitor + reconnect.
  while (!wifiAssociate()) {
    g_stats.wifi_connected = false;
    Serial.printf("[wifi] retry in %lu ms\r\n", (unsigned long)backoff_ms);
    vTaskDelay(pdMS_TO_TICKS(backoff_ms));
    backoff_ms = backoff_ms * 2;
    if (backoff_ms > kBackoffMaxMs) backoff_ms = kBackoffMaxMs;
  }
  backoff_ms = kBackoffMinMs;
  wsBegin();

  uint32_t last_wifi_check_ms = millis();
  for (;;) {
    // Service the WS first (events fire from inside loop()).
    g_ws.loop();
    // Then pump one outbound frame (blocks up to 20 ms inside the
    // queue receive). This single-task ordering guarantees no two
    // threads ever touch g_ws simultaneously.
    pumpOnce();

    // Cheap Wi-Fi health check every second.
    const uint32_t now = millis();
    if (now - last_wifi_check_ms >= 1000) {
      last_wifi_check_ms = now;
      const bool up = WiFi.status() == WL_CONNECTED;
      if (g_stats.wifi_connected && !up) {
        ++g_stats.wifi_disconnect_count;
        g_stats.wifi_connected = false;
        g_stats.wifi_rssi_dbm = 0;
        Serial.println(F("[wifi] link DOWN; retry pending"));
      }
      if (!g_stats.wifi_connected) {
        if (wifiAssociate()) {
          backoff_ms = kBackoffMinMs;
          // Force a WS reconnect so it picks up the new association.
          g_ws.disconnect();
          wsBegin();
        } else {
          vTaskDelay(pdMS_TO_TICKS(backoff_ms));
          backoff_ms = backoff_ms * 2;
          if (backoff_ms > kBackoffMaxMs) backoff_ms = kBackoffMaxMs;
        }
      } else if (up) {
        // Refresh RSSI for the heartbeat dump.
        g_stats.wifi_rssi_dbm = abs(WiFi.RSSI());
      }
    }
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool transport_init(QueueHandle_t outbound_q) {
  if (outbound_q == nullptr) {
    Serial.println(F("[transport] init FAILED: outbound_q is null"));
    return false;
  }
  g_outbound_q = outbound_q;
  g_device_id = makeDeviceId();

  // Stack: 8 KB. The WS client + TLS path keeps a few KB of state
  // around; 8 KB has been comfortable in field testing of fleet_gateway
  // siblings. If we add wss:// and the mbedTLS handshake balloons the
  // stack, bump to 12-16 KB.
  BaseType_t rc = xTaskCreatePinnedToCore(
      transportTask, "voice_ws",
      /*stack=*/8 * 1024,
      /*arg=*/nullptr,
      /*priority=*/3,            // Below the encoder (5) and drain
                                  // logger (4) so audio always wins on
                                  // contention.
      /*handle=*/nullptr,
      /*core=*/0);
  if (rc != pdPASS) {
    Serial.println(F("[transport] init FAILED: xTaskCreatePinnedToCore !pdPASS"));
    return false;
  }
  Serial.println(F("[transport] init OK; voice_ws task spawned on core 0"));
  return true;
}

TransportStats transport_get_stats() {
  return g_stats;
}

}  // namespace transport
}  // namespace alfred
