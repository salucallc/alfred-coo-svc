// Implementation of the I2S RX subsystem.
//
// On arduino-esp32 v2.0.16 we sit on top of ESP-IDF 4.4 and use the
// legacy `driver/i2s.h` API. The newer `i2s_std.h` only ships in IDF 5.x;
// when the project flips to the [env:idf] block (PlatformIO config) we can
// migrate to the new API in one pass. The legacy driver's slave-RX path
// is well-trodden and matches what every ESPHome i2s_audio component does
// under the hood, including the formatBCE integration that proves this
// exact carrier works.

#include "audio_io.h"

#include <Arduino.h>
#include <atomic>
#include <math.h>
#include <string.h>

#include "driver/i2s.h"
#include "esp_err.h"
#include "esp_log.h"

namespace alfred {
namespace audio {

namespace {

constexpr i2s_port_t kI2sPort = I2S_NUM_0;
constexpr const char* kTag    = "audio_io";

// Atomic stats. We only ever read individual fields, so std::atomic<float>
// for the level fields and std::atomic<uint64_t/uint32_t> for the counters
// is both portable and lock-free on Xtensa for these widths.
std::atomic<uint64_t> g_bytes_in{0};
std::atomic<uint64_t> g_frames_in{0};
std::atomic<uint32_t> g_underruns{0};
std::atomic<uint32_t> g_read_errors{0};
std::atomic<uint32_t> g_blocks_processed{0};
std::atomic<uint32_t> g_rms_dbfs_bits{0};   // Hold a float bit-pattern.
std::atomic<uint32_t> g_peak_dbfs_bits{0};

bool g_initialized = false;

// Helper to publish a float into an atomic<uint32_t> bag without UB.
void store_float(std::atomic<uint32_t>& slot, float v) {
  uint32_t bits;
  static_assert(sizeof(bits) == sizeof(v), "float must be 32 bits");
  memcpy(&bits, &v, sizeof(bits));
  slot.store(bits, std::memory_order_relaxed);
}

float load_float(const std::atomic<uint32_t>& slot) {
  uint32_t bits = slot.load(std::memory_order_relaxed);
  float v;
  memcpy(&v, &bits, sizeof(v));
  return v;
}

}  // namespace

bool audio_io_init() {
  if (g_initialized) {
    return true;
  }

  // Slave RX mode: XMOS test5 firmware drives BCK and LRCK; we are a passive
  // listener. RX-only since we do not yet drive the speaker (that lands in
  // SAL-4014 / P2.3). Standard I2S framing (Philips), 32-bit slot,
  // stereo (left+right). Channel 0 (left) is the post-AEC ASR stream per
  // the test5 firmware spec; channel 1 (right) carries the secondary mix
  // (typically AEC reference / second beamformed channel; documented at
  // https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/).
  i2s_config_t cfg = {};
  cfg.mode = static_cast<i2s_mode_t>(I2S_MODE_SLAVE | I2S_MODE_RX);
  cfg.sample_rate = kSampleRateHz;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;  // standard stereo, both slots
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = kDmaBufCount;
  cfg.dma_buf_len = kDmaBufLenFrames;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = false;
  cfg.fixed_mclk = 0;
  cfg.mclk_multiple = I2S_MCLK_MULTIPLE_DEFAULT;
  cfg.bits_per_chan = I2S_BITS_PER_CHAN_DEFAULT;

  esp_err_t err = i2s_driver_install(kI2sPort, &cfg, /*queue_size=*/0,
                                     /*event_queue=*/nullptr);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "i2s_driver_install failed: %s", esp_err_to_name(err));
    return false;
  }

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = kI2sBclkPin;
  pins.ws_io_num  = kI2sLrclkPin;
  pins.data_out_num = I2S_PIN_NO_CHANGE;  // RX-only for now.
  pins.data_in_num  = kI2sDinPin;

  err = i2s_set_pin(kI2sPort, &pins);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "i2s_set_pin failed: %s", esp_err_to_name(err));
    i2s_driver_uninstall(kI2sPort);
    return false;
  }

  // For slave RX we do NOT call i2s_set_clk; the master (XMOS) controls all
  // clocking. Calling it with our own desired rate would actually try to
  // reconfigure the local divider chain on a port that is supposed to be
  // a follower, which is harmless on slave but unnecessary.

  // Discard the first DMA load. After driver install the buffers contain
  // whatever was on the bus at boot, which is often a half-frame of garbage.
  // Allocate the scratch on the heap; the Arduino loopTask only has an
  // 8 KB stack and a buffer this big tips it over (verified on 2026-05-02).
  const size_t scratch_bytes = kDmaBufLenFrames * kBytesPerFrame;
  uint8_t* scratch = static_cast<uint8_t*>(malloc(scratch_bytes));
  if (scratch != nullptr) {
    size_t consumed = 0;
    i2s_read(kI2sPort, scratch, scratch_bytes, &consumed, pdMS_TO_TICKS(50));
    free(scratch);
  }

  g_initialized = true;
  ESP_LOGI(kTag,
           "I2S0 slave RX up: %lu Hz, %u-bit, %u ch, BCK=%d WS=%d DIN=%d, "
           "DMA %dx%d frames (~%lu ms runway)",
           static_cast<unsigned long>(kSampleRateHz),
           kBitsPerSample, kChannelCount,
           kI2sBclkPin, kI2sLrclkPin, kI2sDinPin,
           kDmaBufCount, kDmaBufLenFrames,
           static_cast<unsigned long>(
               (1000UL * kDmaBufCount * kDmaBufLenFrames) / kSampleRateHz));
  return true;
}

size_t audio_io_read_block(int32_t* buf, size_t frames, uint32_t timeout_ms) {
  if (!g_initialized || buf == nullptr || frames == 0) {
    return 0;
  }

  const size_t want_bytes = frames * kBytesPerFrame;
  size_t got_bytes = 0;
  esp_err_t err = i2s_read(kI2sPort, buf, want_bytes, &got_bytes,
                           pdMS_TO_TICKS(timeout_ms));
  if (err != ESP_OK) {
    g_read_errors.fetch_add(1, std::memory_order_relaxed);
    return 0;
  }
  if (got_bytes == 0) {
    g_underruns.fetch_add(1, std::memory_order_relaxed);
    return 0;
  }

  const size_t got_frames = got_bytes / kBytesPerFrame;
  g_bytes_in.fetch_add(got_bytes, std::memory_order_relaxed);
  g_frames_in.fetch_add(got_frames, std::memory_order_relaxed);
  g_blocks_processed.fetch_add(1, std::memory_order_relaxed);

  // Compute RMS + peak on channel 0 (left = AEC-clean ASR stream).
  // Use double for the sum-of-squares so we don't lose precision over
  // a 1024-frame block at 32-bit input.
  double sum_sq = 0.0;
  int32_t peak = 0;
  for (size_t i = 0; i < got_frames; ++i) {
    int32_t s0 = buf[i * kChannelCount + 0];   // left
    double f = static_cast<double>(s0) / 2147483648.0;
    sum_sq += f * f;
    int32_t a = (s0 < 0) ? -s0 : s0;
    if (a > peak) {
      peak = a;
    }
  }
  const double rms = (got_frames > 0) ? sqrt(sum_sq / got_frames) : 0.0;
  // Clamp the silence floor so log10(0) doesn't print -inf.
  // -120 dBFS is well below the noise floor of any real codec.
  const double rms_clamped = (rms < 1e-6) ? 1e-6 : rms;
  const double peak_norm = static_cast<double>(peak) / 2147483648.0;
  const double peak_clamped = (peak_norm < 1e-6) ? 1e-6 : peak_norm;
  store_float(g_rms_dbfs_bits,  static_cast<float>(20.0 * log10(rms_clamped)));
  store_float(g_peak_dbfs_bits, static_cast<float>(20.0 * log10(peak_clamped)));

  return got_frames;
}

AudioStats audio_io_get_stats() {
  AudioStats s = {};
  s.bytes_in         = g_bytes_in.load(std::memory_order_relaxed);
  s.frames_in        = g_frames_in.load(std::memory_order_relaxed);
  s.underruns        = g_underruns.load(std::memory_order_relaxed);
  s.read_errors      = g_read_errors.load(std::memory_order_relaxed);
  s.blocks_processed = g_blocks_processed.load(std::memory_order_relaxed);
  s.rms_ch0_dbfs     = load_float(g_rms_dbfs_bits);
  s.peak_ch0_dbfs    = load_float(g_peak_dbfs_bits);
  return s;
}

}  // namespace audio
}  // namespace alfred
