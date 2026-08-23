// Alfred voice puck — I2S TX subsystem.
//
// Ticket: SAL-4007 (P1.7). Mirror of audio_io's RX path: pushes 48 kHz /
// 32-bit / stereo PCM out DOUT (GPIO44) so the soldered speaker on the
// ReSpeaker XVF3800 carrier (driven by the TLV320AIC3104 codec on the
// shared I2S bus) plays back what Alfred says.
//
// The XIAO ESP32-S3 stays I2S slave — XMOS test5 firmware drives BCK +
// LRCK on the same bus we already RX from. Because the legacy
// driver/i2s.h API on arduino-esp32 v2.x can only own one direction per
// port, we co-own I2S_NUM_0 with audio_io: i2s_tx_init() reconfigures
// the port for I2S_MODE_RX | I2S_MODE_TX (duplex slave) and registers
// data_out_num = GPIO44 in addition to the existing data_in_num.
//
// audio_io_init() must be called first; i2s_tx_init() relies on its
// driver_install + pin_config and only updates the pin map + DMA TX
// buffers. If we ever migrate to esp_idf 5.x i2s_std this collapses to
// two channel handles on the same controller.
//
// Sample format on the wire matches what XMOS expects on the codec's
// other half of the I2S frame (codec is master from the puck's side
// because XMOS drives BCK/LRCK; the codec just samples whichever data
// line is wired into its DIN — the carrier wires GPIO44 -> codec DIN).
// 48 kHz, 32-bit slot, stereo. We left-shift int16 by 16 to fill the
// upper bits of the 32-bit slot; lower 16 stay zero (volume scales
// linearly with how many bits we actually use, and 48 dB of headroom is
// plenty for voice).

#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace alfred {
namespace audio {

// Hardware-level constants for the TX path. Most are pulled from
// audio_io.h via duplex sharing of the same I2S0 port; we only own the
// DOUT pin and the TX-side DMA sizing.
constexpr int kI2sDoutPinTx = 44;       // GPIO44 -> TLV320AIC3104 DIN

// DMA TX sizing: 6 buffers x 240 frames at 48 kHz stereo = ~30 ms total
// runway, ~5 ms per buffer. Latency-vs-underrun trade-off; voice
// playback wants low latency so we keep the buffers small. If we
// observe i2s_write timeouts in the field, bump kDmaTxBufLenFrames to
// 480 (60 ms total) at the cost of doubled latency.
constexpr int kDmaTxBufCount      = 6;
constexpr int kDmaTxBufLenFrames  = 240;

struct I2sTxStats {
  uint64_t bytes_out;        // Total bytes pushed to i2s_write.
  uint64_t frames_out;       // Total stereo frames pushed.
  uint32_t write_errors;     // Non-OK return codes from i2s_write.
  uint32_t write_short;      // Times i2s_write wrote less than requested.
  uint32_t write_calls;      // Successful (non-zero, non-error) write calls.
  uint32_t max_write_us;     // Slowest i2s_write since boot.
  uint32_t last_write_us;    // Most recent i2s_write wall time.
};

// Reconfigure I2S0 (already initialized by audio_io_init() in slave RX
// mode) into duplex slave RX+TX, register GPIO44 as DOUT, and install
// the TX-side DMA buffers. Returns true on success. Idempotent;
// repeated calls are no-ops.
//
// MUST be called AFTER audio_io_init() — the latter installs the
// driver and the pin map; we patch on top.
bool i2s_tx_init();

// Push `frames` stereo frames (frames * 2 int32_t samples) out the I2S
// TX path. Blocks up to `timeout_ms` waiting for DMA buffer space.
// Returns the number of frames actually written. Updates stats.
//
// Caller must have converted any int16 PCM to int32 via the
// upsample_and_promote helper below (or its own equivalent).
size_t i2s_tx_write_block(const int32_t* buf, size_t frames,
                          uint32_t timeout_ms = 50);

// Snapshot stats. Safe to call from any task.
I2sTxStats i2s_tx_get_stats();

// ---------------------------------------------------------------------------
// PCM helpers: 16 kHz mono int16 → 48 kHz stereo int32 in one shot.
// ---------------------------------------------------------------------------

// Linear-interpolation upsample by 3x: 16 kHz mono int16 → 48 kHz mono
// int16. `in_samples` int16 in `in`; writes `in_samples * 3` int16
// into `out`. `last_in` is the previous block's last sample so the
// interpolation is continuous across block boundaries (caller must
// keep state); pass 0 on the first call. Returns the new last_in.
//
// Linear interpolation is sufficient for v1 voice; the FIR low-pass on
// the codec output filters most of the imaging artifacts, and Opus VOIP
// at 16 kHz already band-limits the source to ~7 kHz so the spectral
// images at multiples of 16 kHz fall into already-attenuated regions.
int16_t upsample_3x_linear_mono(const int16_t* in, size_t in_samples,
                                int16_t last_in, int16_t* out);

// Stereo-duplicate + int16→int32 promote in one pass. Reads
// `mono_samples` int16 from `mono_48k`, writes `mono_samples * 2`
// int32 (stereo, left=right) into `stereo_48k_int32`. The int32 slot
// is filled by left-shifting the int16 by 16, matching the upper-bits
// convention XMOS / codec expects.
void mono_int16_to_stereo_int32(const int16_t* mono_48k,
                                size_t mono_samples,
                                int32_t* stereo_48k_int32);

}  // namespace audio
}  // namespace alfred
