// Alfred voice puck — I2S RX subsystem.
//
// Ticket: SAL-4000 (P1.2). Captures the post-AEC stream that the XVF3800
// emits over I2S in master mode (test5 firmware: i2s_master, 48 kHz, 32-bit,
// 2 channels). The XIAO ESP32-S3 acts as I2S slave RX.
//
// Pin map verified from:
//   * Seeed wiki: https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/
//   * formatBCE/Respeaker-XVF3800-ESPHome-integration satellite YAML
//     (i2s_mode: secondary == ESP32 slave; lrclk=GPIO7, bclk=GPIO8,
//      din=GPIO43, dout=GPIO44).
//
// Brief originally listed BCK=8, WS=9, DIN=7 and 16 kHz / 4-channel. That was
// wrong. Updated to match the production ESPHome integration which is the only
// known-good driver for this exact carrier + test5 firmware.

#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace alfred {
namespace audio {

// Hardware-level constants for the XIAO ESP32-S3 + XVF3800 stack with
// test5 i2s_master firmware. Exposed here so callers can size their own
// buffers and consumers (Opus encoder downstream) know the sample format.
constexpr uint32_t kSampleRateHz = 48000;
constexpr uint8_t  kBitsPerSample = 32;     // I2S slot width
constexpr uint8_t  kChannelCount  = 2;      // XMOS test5 emits stereo
constexpr int      kI2sBclkPin    = 8;      // GPIO8
constexpr int      kI2sLrclkPin   = 7;      // GPIO7
constexpr int      kI2sDinPin     = 43;     // GPIO43
constexpr int      kI2sDoutPin    = 44;     // GPIO44 (unused by RX, reserved)

// One stereo frame = 2 samples of 32 bits each, captured back to back.
constexpr size_t kBytesPerFrame = (kBitsPerSample / 8) * kChannelCount;

// DMA sizing: 6 buffers x 1024 frames at 48 kHz stereo = ~128 ms total runway,
// roughly 21 ms per buffer. Comfortably above the >=60 ms target the brief
// asks for, and a clean multiple of typical Opus 20 ms frame work units.
constexpr int    kDmaBufCount       = 6;
constexpr int    kDmaBufLenFrames   = 1024;

struct AudioStats {
  uint64_t bytes_in;          // Total bytes pulled out of the I2S DMA.
  uint64_t frames_in;         // Total stereo frames pulled out.
  uint32_t underruns;         // Times i2s_read returned 0 bytes within timeout.
  uint32_t read_errors;       // Non-zero, non-OK return codes from i2s_read.
  float    rms_ch0_dbfs;      // Last-block RMS for channel 0 in dBFS.
  float    peak_ch0_dbfs;     // Last-block peak for channel 0 in dBFS.
  uint32_t blocks_processed;  // How many read_block calls succeeded.
};

// Initialize the I2S0 peripheral in slave RX mode and install DMA buffers.
// Returns true on success. Safe to call exactly once at boot. After this
// returns true, audio is already streaming into DMA; you only need to drain
// it via audio_io_read_block().
bool audio_io_init();

// Block until `frames` stereo frames are read from DMA into `buf` (sized
// frames * 2 int32 samples), or `timeout_ms` elapses. Returns the number of
// frames actually read. Updates internal stats (bytes_in, RMS, peak, etc).
// `buf` MUST hold frames * kChannelCount int32_t values.
size_t audio_io_read_block(int32_t* buf, size_t frames, uint32_t timeout_ms = 100);

// Snapshot the latest stats. Safe to call from any task; values are written
// atomically per-field so a torn read of one field is the only risk.
AudioStats audio_io_get_stats();

// Convert a 32-bit I2S sample (left-justified, real audio in upper 24 bits
// for typical I2S codecs) to a normalized float in [-1.0, 1.0]. Exposed for
// tests and for downstream Opus framing.
inline float sample_to_float(int32_t s) {
  // Treat the full int32 range as full scale. XMOS test5 emits 32-bit
  // left-justified with the meaningful audio in the upper bits, so this is
  // the correct scaling regardless of how many bits XMOS actually uses.
  return static_cast<float>(s) / 2147483648.0f;
}

}  // namespace audio
}  // namespace alfred
