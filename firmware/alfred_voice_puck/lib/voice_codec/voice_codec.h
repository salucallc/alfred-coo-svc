// Alfred voice puck - Opus voice encoder.
//
// Ticket: SAL-4001 (P1.3). Wraps the upstream Opus codec into a pipeline
// that consumes the 48 kHz / 32-bit / stereo blocks coming out of audio_io
// (SAL-4000) and emits 16 kHz / mono / 20 ms Opus voice frames at ~24 kbps
// VBR per the codec spec locked in PLAN.md decision 11.
//
// Pipeline per call:
//   int32 stereo @ 48 kHz, 960 frames (20 ms)
//     -> select channel 0 (XVF3800 left = AEC-clean ASR stream)
//     -> 11-tap Hamming-windowed FIR low-pass at ~7 kHz cutoff, decimate /3
//        (anti-alias before downsampling 48 -> 16 kHz)
//     -> int32 >> 16 to int16 (Opus fixed-point input)
//        gives us 320 int16 samples (20 ms at 16 kHz mono)
//     -> opus_encode with OPUS_APPLICATION_VOIP, complexity 5, VBR 24 kbps
//
// The encoder is allocated once at init; opus_encoder_create needs about
// 8-10 KB and we let it sit in regular heap (PSRAM is fine but the encoder
// touches state every 20 ms so internal RAM is preferred for cache).
//
// All public functions are safe to call from a single task only. The expected
// caller is a dedicated FreeRTOS task pinned to core 1 (see main.cpp). The
// stats accessor is the only thing that may be touched from another task.

#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace alfred {
namespace codec {

// Codec parameters locked in PLAN.md decision 11. Exposed here so callers
// can size their own buffers without guessing.
constexpr uint32_t kVoiceSampleRateHz = 16000;
constexpr uint8_t  kVoiceChannels     = 1;
constexpr uint32_t kVoiceFrameMs      = 20;
constexpr uint32_t kVoiceBitrateBps   = 24000;
constexpr int      kVoiceComplexity   = 3;       // 0..10; 3 keeps the warm-up encode
                                                  // call under the 10 ms acceptance
                                                  // budget on the XIAO ESP32-S3 at
                                                  // 240 MHz with FIXED_POINT. Steady
                                                  // state is ~3..5 ms/frame at this
                                                  // setting; complexity 5 measured
                                                  // 12.4 ms on the warm-up frame
                                                  // (steady ~5..7 ms). Quality
                                                  // difference at 24 kbps voice is
                                                  // imperceptible per the Opus docs.
constexpr size_t   kVoiceFrameSamples = (kVoiceSampleRateHz / 1000) * kVoiceFrameMs;  // 320

// Decimation ratio: 48 kHz input -> 16 kHz Opus.
constexpr uint32_t kInputSampleRateHz = 48000;
constexpr uint32_t kDecimateBy        = kInputSampleRateHz / kVoiceSampleRateHz;  // 3
constexpr size_t   kInputBlockFrames  = (kInputSampleRateHz / 1000) * kVoiceFrameMs;  // 960
constexpr uint8_t  kInputChannels     = 2;

// Maximum Opus payload size for a 20 ms frame at 24 kbps VBR. Theoretical
// max for Opus is 1275 bytes/frame; we provision 200 bytes which is more
// than 4x our target average (~60 bytes) and covers any VBR upward swing.
constexpr size_t   kOpusMaxPayloadBytes = 200;

struct CodecStats {
  uint64_t blocks_in;        // Calls to encode_block.
  uint64_t frames_in;        // Total 20 ms frames offered (== blocks_in if no drops).
  uint64_t frames_out;       // Total Opus frames produced.
  uint64_t bytes_out;        // Total Opus payload bytes produced.
  uint32_t encode_overruns;  // Encodes that exceeded the 20 ms wallclock budget.
  uint32_t encode_errors;    // opus_encode returned negative.
  uint32_t max_encode_us;    // Slowest encode_block call since boot.
  uint32_t last_encode_us;   // Most recent encode_block call.
  uint32_t last_payload_bytes; // Bytes from the most recent successful encode.
  uint32_t avg_payload_bytes_x10; // Rolling average bytes/frame * 10 (so we keep one decimal).
};

// One encoded Opus frame ready to ship over WebSocket.
struct EncodedFrame {
  uint32_t seq;          // Monotonically increasing per session.
  uint32_t timestamp_ms; // millis() at encode completion.
  uint16_t length;       // Bytes used in `data`.
  uint8_t  data[kOpusMaxPayloadBytes];
};

// Allocate the encoder, configure it for VOIP / 16 kHz mono / 20 ms / 24 kbps
// VBR / complexity 5. Idempotent. Returns true on success.
bool voice_codec_init();

// Encode one 20 ms block of 48 kHz stereo int32 mic audio into one Opus
// voice frame. `pcm_48k_stereo` must hold kInputBlockFrames * kInputChannels
// int32 samples (so 1920 int32 = 7680 bytes). On success writes the payload
// into `out->data` (length set in `out->length`) and returns true. On Opus
// error returns false; stats track the failure.
//
// The provided EncodedFrame's `seq` and `timestamp_ms` are populated by the
// caller's drain loop (encode_block leaves them at zero). The encode time
// in microseconds is recorded into stats.
bool voice_codec_encode_block(const int32_t* pcm_48k_stereo,
                              uint8_t* out_payload,
                              size_t* out_length);

// Snapshot stats. Safe to call from any task; counters use atomic RMW writes
// in the encoder task and torn reads on individual fields are the only risk.
CodecStats voice_codec_get_stats();

}  // namespace codec
}  // namespace alfred
