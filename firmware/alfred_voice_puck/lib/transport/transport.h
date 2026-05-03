// Alfred voice puck - Wi-Fi + WebSocket transport.
//
// Ticket: SAL-4006 (P1.6, end-to-end voice-in). Replaces the SAL-4001
// drain-and-log path with a real uplink to the alfred_coo.fleet_voice
// gateway, plus a downlink that logs server JSON / TTS binary frames.
//
// Responsibilities:
//   * Bring up Wi-Fi STA using SSID + password baked in via build_flags
//     from secrets.ini. Retries with exponential backoff on disconnect.
//   * Open and maintain a WebSocket to ws://<gateway>/v1/fleet/voice
//     with `Authorization: Bearer <ALFRED_GATEWAY_BEARER>` header.
//     Reconnect with exponential backoff if the socket drops.
//   * On WS open: send the device hello JSON control frame so the
//     gateway can register the session.
//   * Pump: dequeue alfred::codec::EncodedFrame from g_outbound_q and
//     ship `frame.data[0..length]` as a binary WS message. Drop frames
//     when the WS is not connected (the queue's drop-oldest semantics
//     plus this drain keep voice fresh; we never block audio capture).
//   * Inbound: log every server text frame (welcome, ping, transcript,
//     assistant_text) and the byte-length of every binary frame (Opus
//     from TTS; full decoding is SAL-4007).
//   * Periodic pong: when the server sends `{"type":"ping","seq":N}`,
//     reply `{"type":"pong","seq":N}` so the gateway's keepalive loop
//     doesn't 1011 us out.
//
// Threading model:
//   * Wi-Fi + WebSocket events run on the Arduino Wi-Fi thread (core 0).
//   * `transport_init` spawns ONE FreeRTOS task `voice_ws` pinned to
//     core 0 that owns the WebSocketsClient and runs ws.loop() at high
//     frequency. The same task drains the outbound queue between loop
//     ticks, so all socket access is single-threaded and we don't need
//     a mutex on the WebSockets library (which is decidedly not
//     thread-safe).
//   * The encoder task (core 1) keeps producing into g_outbound_q at
//     50 fps regardless of WS state; we never block audio.
//
// Why pin the transport task to core 0:
//   * arduino-esp32 runs the Wi-Fi stack on core 0 by default.
//   * Keeping the WS client on the same core eliminates cross-core
//     synchronization overhead for every send.
//   * Core 1 stays clear for I2S DMA + Opus encode (PLAN.md risk #1).
//
// Why pump from the same task that runs ws.loop():
//   * The links2004/WebSockets library's sendBIN call must not be
//     interleaved with poll(). Single task = no interleaving.
//
// Public surface is intentionally tiny. Everything is configured via
// build_flags; the main loop just calls transport_init once.

#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <stdint.h>

namespace alfred {
namespace transport {

// Live counters; the heartbeat in main.cpp dumps these so ops can see
// transport health without needing to attach a monitor mid-conversation.
struct TransportStats {
  // Wi-Fi.
  bool     wifi_connected;
  uint32_t wifi_connect_count;
  uint32_t wifi_disconnect_count;
  uint32_t wifi_rssi_dbm;          // Last reported RSSI (0 if not connected).

  // WebSocket.
  bool     ws_connected;
  uint32_t ws_connect_count;
  uint32_t ws_disconnect_count;
  uint32_t ws_send_failures;       // sendBIN returned false.
  uint64_t frames_sent;            // Outbound binary frames pushed to WS.
  uint64_t bytes_sent;             // Outbound binary bytes (Opus payload).
  uint64_t frames_recv_text;       // Inbound JSON control frames.
  uint64_t frames_recv_binary;     // Inbound binary frames (TTS Opus, SAL-4007).
  uint64_t bytes_recv_binary;      // Inbound binary bytes.
  uint32_t pings_recv;             // Server -> client pings answered.
  uint32_t transcripts_recv;       // Count of `transcript` JSON frames seen.
  uint32_t assistant_text_recv;    // Count of `assistant_text` JSON frames seen.

  // Frames dropped because the WS was not connected when they arrived.
  uint64_t frames_dropped_offline;
};

// Initialize Wi-Fi + WebSocket subsystems.
//   * `outbound_q`: FreeRTOS queue carrying alfred::codec::EncodedFrame
//     items from the encoder task. Owned by main.cpp; transport just
//     dequeues.
//   * Returns true if the transport task spawned. Wi-Fi/WS will come
//     up asynchronously; check transport_get_stats() to observe.
//   * The function does NOT block waiting for Wi-Fi association. If the
//     network is missing the transport task keeps retrying in the
//     background and the encoder keeps running into the queue.
bool transport_init(QueueHandle_t outbound_q);

// Snapshot of live counters. Safe to call from any task.
TransportStats transport_get_stats();

}  // namespace transport
}  // namespace alfred
