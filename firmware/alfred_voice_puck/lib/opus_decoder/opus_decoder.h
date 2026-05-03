// Alfred voice puck — Opus decoder.
//
// Ticket: SAL-4007 (P1.7). Mirror of voice_codec's encoder. Consumes
// 16 kHz / mono / 20 ms Opus packets shipped down the WS by the gateway
// (SAL-4005 TTS path: audio_start envelope, N binary frames, audio_end)
// and emits 16 kHz mono int16 PCM, ready to be upsampled + stereo-
// duplicated + promoted to int32 by i2s_tx for playback.
//
// We use the same sh123/esp32_opus library as the encoder so we have one
// Opus dependency on the build (FIXED_POINT, OPUS_BUILD; correct for
// ESP32-S3). The library exposes both halves of the codec; we just call
// the decoder side here.
//
// Threading: like voice_codec, decode() is intended to be called from a
// single owner task (the WS receive task that lives in transport.cpp).
// stats() may be touched from any task. opus_decoder_create allocates
// from heap; we keep one decoder instance alive for the duration of an
// audio_start..audio_end window, freed on audio_end so a long idle
// period doesn't pin RAM. (Decoder state is ~10-15 KB.)

#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace alfred {
namespace codec {

// Decoder runs at the same audio params as the encoder: 16 kHz mono,
// 20 ms frames. Exposed so callers can size their PCM buffers.
constexpr uint32_t kDecodeSampleRateHz = 16000;
constexpr uint8_t  kDecodeChannels     = 1;
constexpr uint32_t kDecodeFrameMs      = 20;
constexpr size_t   kDecodeFrameSamples =
    (kDecodeSampleRateHz / 1000) * kDecodeFrameMs;  // 320

// Maximum Opus packet size we'll accept on the wire. Theoretical Opus
// max is 1275 bytes; gateway-side TTS encoder ships at 32 kbps which is
// ~80 bytes/frame avg. 256 is a generous ceiling that covers PLC
// expansions and high-bitrate spikes.
constexpr size_t kDecodeMaxPacketBytes = 1500;

// Maximum samples we'll write per decode call. A single Opus packet at
// 16 kHz can encode up to 120 ms (per the spec) which is 1920 samples.
// Cap at 2 * kDecodeFrameSamples for safety; if we ever see a 40 ms or
// longer packet we can revisit, but the gateway always ships 20 ms.
constexpr size_t kDecodeMaxSamplesPerCall = 1920;

struct DecoderStats {
  uint64_t packets_in;        // Total decode() calls.
  uint64_t packets_decoded;   // Successful decodes (returned >0 samples).
  uint64_t packets_errored;   // opus_decode returned negative.
  uint64_t samples_out;       // Total int16 samples emitted.
  uint32_t plc_invocations;   // Times we asked the decoder to do PLC (NULL packet).
  uint32_t max_decode_us;     // Slowest decode call.
  uint32_t last_decode_us;    // Most recent decode call.
  uint32_t last_packet_bytes; // Bytes of the most recent packet.
  uint32_t last_samples;      // Samples written by the most recent decode.
  uint32_t sessions_opened;   // open() calls.
  uint32_t sessions_closed;   // close() calls.
};

// Open a fresh decoder session. Allocates the OpusDecoder via
// opus_decoder_create. Returns true on success. If a decoder is already
// open, this resets it (free + recreate) so the new session starts
// clean — the gateway's `audio_start` envelope is the boundary.
bool opus_decoder_open();

// Close the current decoder session. Frees the OpusDecoder. Safe to
// call when no session is open. Called on `audio_end`.
void opus_decoder_close();

// Decode one Opus packet into 16 kHz mono int16 PCM. `packet` may be
// NULL with `packet_bytes==0` to request packet-loss-concealment for a
// missed frame; in that case the decoder synthesizes
// `expected_frame_samples` samples (default 320 = 20 ms). On success
// writes up to `out_max_samples` samples into `out`, sets
// `*out_samples`, and returns true.
//
// `out` must hold at least kDecodeMaxSamplesPerCall int16_t.
bool opus_decoder_decode(const uint8_t* packet, size_t packet_bytes,
                         int16_t* out, size_t out_max_samples,
                         size_t* out_samples);

// True iff a decoder session is currently open.
bool opus_decoder_is_open();

// Snapshot stats. Safe to call from any task.
DecoderStats opus_decoder_get_stats();

}  // namespace codec
}  // namespace alfred
