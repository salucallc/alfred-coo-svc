// Implementation of the Opus voice decoder.
//
// SAL-4007 (P1.7). Mirror of voice_codec.cpp. Same library
// (sh123/esp32_opus, FIXED_POINT) so we get one Opus dependency for
// both halves of the codec.

#include "opus_decoder.h"

#include <atomic>
#include <string.h>

#include "esp_log.h"
#include "opus.h"

namespace alfred {
namespace codec {

namespace {

constexpr const char* kTag = "opus_dec";

// Single decoder handle. Reset on every audio_start envelope so that
// inter-utterance state (LSP history etc.) doesn't leak across the
// boundary; the gateway tells us explicitly when one stream ends and
// the next begins, so we honor that.
OpusDecoder* g_dec = nullptr;
bool g_open = false;

std::atomic<uint64_t> g_packets_in{0};
std::atomic<uint64_t> g_packets_decoded{0};
std::atomic<uint64_t> g_packets_errored{0};
std::atomic<uint64_t> g_samples_out{0};
std::atomic<uint32_t> g_plc_invocations{0};
std::atomic<uint32_t> g_max_decode_us{0};
std::atomic<uint32_t> g_last_decode_us{0};
std::atomic<uint32_t> g_last_packet_bytes{0};
std::atomic<uint32_t> g_last_samples{0};
std::atomic<uint32_t> g_sessions_opened{0};
std::atomic<uint32_t> g_sessions_closed{0};

}  // namespace

bool opus_decoder_open() {
  // If a session is already open, tear it down first. The gateway is
  // the source of truth on stream boundaries; resetting state here
  // matches what its audio_start envelope means.
  if (g_open && g_dec != nullptr) {
    opus_decoder_destroy(g_dec);
    g_dec = nullptr;
    g_open = false;
  }
  int err = OPUS_OK;
  g_dec = opus_decoder_create(static_cast<opus_int32>(kDecodeSampleRateHz),
                              kDecodeChannels, &err);
  if (g_dec == nullptr || err != OPUS_OK) {
    ESP_LOGE(kTag, "opus_decoder_create failed: %d", err);
    g_dec = nullptr;
    return false;
  }
  g_open = true;
  g_sessions_opened.fetch_add(1, std::memory_order_relaxed);
  ESP_LOGI(kTag, "decoder session open: %lu Hz mono",
           static_cast<unsigned long>(kDecodeSampleRateHz));
  return true;
}

void opus_decoder_close() {
  if (!g_open) {
    return;
  }
  if (g_dec != nullptr) {
    opus_decoder_destroy(g_dec);
    g_dec = nullptr;
  }
  g_open = false;
  g_sessions_closed.fetch_add(1, std::memory_order_relaxed);
  ESP_LOGI(kTag, "decoder session closed");
}

bool opus_decoder_is_open() {
  return g_open;
}

bool opus_decoder_decode(const uint8_t* packet, size_t packet_bytes,
                         int16_t* out, size_t out_max_samples,
                         size_t* out_samples) {
  if (out == nullptr || out_samples == nullptr) {
    return false;
  }
  *out_samples = 0;
  if (!g_open || g_dec == nullptr) {
    // Auto-open: gateway might have started shipping binary frames
    // before the audio_start envelope reached us (or we missed it).
    // Decoding without a fresh session is still correct; we just lose
    // the explicit reset boundary.
    if (!opus_decoder_open()) {
      return false;
    }
  }
  if (packet_bytes > kDecodeMaxPacketBytes) {
    ESP_LOGW(kTag, "packet too big (%u > %u); dropping",
             (unsigned)packet_bytes, (unsigned)kDecodeMaxPacketBytes);
    return false;
  }

  g_packets_in.fetch_add(1, std::memory_order_relaxed);
  g_last_packet_bytes.store(static_cast<uint32_t>(packet_bytes),
                            std::memory_order_relaxed);

  const uint32_t t0 = micros();

  // opus_decode signature:
  //   int opus_decode(OpusDecoder *st, const unsigned char *data,
  //                   opus_int32 len, opus_int16 *pcm, int frame_size,
  //                   int decode_fec)
  //
  // For PLC (packet-loss-concealment) call with data=NULL, len=0, and
  // frame_size = expected number of samples for the missed frame
  // (typically 320 = 20 ms at 16 kHz).
  const bool is_plc = (packet == nullptr || packet_bytes == 0);
  if (is_plc) {
    g_plc_invocations.fetch_add(1, std::memory_order_relaxed);
  }
  const int frame_size = static_cast<int>(
      is_plc ? kDecodeFrameSamples : out_max_samples);

  const int decoded = opus_decode(
      g_dec,
      is_plc ? nullptr : packet,
      static_cast<opus_int32>(packet_bytes),
      out,
      frame_size,
      /*decode_fec=*/0);

  const uint32_t t1 = micros();
  const uint32_t dt = t1 - t0;
  g_last_decode_us.store(dt, std::memory_order_relaxed);
  uint32_t prev_max = g_max_decode_us.load(std::memory_order_relaxed);
  while (dt > prev_max && !g_max_decode_us.compare_exchange_weak(prev_max, dt)) {}

  if (decoded < 0) {
    g_packets_errored.fetch_add(1, std::memory_order_relaxed);
    ESP_LOGW(kTag, "opus_decode error %d (packet_bytes=%u)",
             decoded, (unsigned)packet_bytes);
    return false;
  }
  *out_samples = static_cast<size_t>(decoded);
  g_last_samples.store(static_cast<uint32_t>(decoded),
                       std::memory_order_relaxed);
  g_packets_decoded.fetch_add(1, std::memory_order_relaxed);
  g_samples_out.fetch_add(static_cast<uint64_t>(decoded),
                          std::memory_order_relaxed);
  return true;
}

DecoderStats opus_decoder_get_stats() {
  DecoderStats s = {};
  s.packets_in        = g_packets_in.load(std::memory_order_relaxed);
  s.packets_decoded   = g_packets_decoded.load(std::memory_order_relaxed);
  s.packets_errored   = g_packets_errored.load(std::memory_order_relaxed);
  s.samples_out       = g_samples_out.load(std::memory_order_relaxed);
  s.plc_invocations   = g_plc_invocations.load(std::memory_order_relaxed);
  s.max_decode_us     = g_max_decode_us.load(std::memory_order_relaxed);
  s.last_decode_us    = g_last_decode_us.load(std::memory_order_relaxed);
  s.last_packet_bytes = g_last_packet_bytes.load(std::memory_order_relaxed);
  s.last_samples      = g_last_samples.load(std::memory_order_relaxed);
  s.sessions_opened   = g_sessions_opened.load(std::memory_order_relaxed);
  s.sessions_closed   = g_sessions_closed.load(std::memory_order_relaxed);
  return s;
}

}  // namespace codec
}  // namespace alfred
