// Implementation of the I2S TX subsystem.
//
// SAL-4007 (P1.7). Co-owns I2S_NUM_0 with audio_io's RX path. The legacy
// driver/i2s.h API on arduino-esp32 v2.x can flip a port from RX-only
// to RX+TX after install via i2s_set_pin (registering data_out_num) +
// a DMA buffer reinstall via i2s_driver_uninstall + i2s_driver_install
// with the new mode. We do exactly that here so audio_io's existing
// state stays intact (same DMA RX sizing, same slave-mode framing) and
// we just light up the TX side on top.
//
// Why not run i2s_tx_init() before audio_io_init(): if we did, audio_io
// would re-uninstall+reinstall the driver on its own first call,
// blowing away our TX DMA buffers. The "audio_io first, then we
// upgrade to duplex" ordering keeps the API surface minimal — audio_io
// stays unaware of TX, and the only state it cares about (g_initialized
// + the RX read path) survives our reinstall because i2s_read just
// pulls from whatever DMA the driver currently owns.

#include "i2s_tx.h"

#include "audio_io.h"

#include <Arduino.h>
#include <atomic>
#include <string.h>

#include "driver/i2s.h"
#include "esp_err.h"
#include "esp_log.h"

namespace alfred {
namespace audio {

namespace {

constexpr i2s_port_t kI2sPort = I2S_NUM_0;
constexpr const char* kTag    = "i2s_tx";

bool g_initialized = false;

std::atomic<uint64_t> g_bytes_out{0};
std::atomic<uint64_t> g_frames_out{0};
std::atomic<uint32_t> g_write_errors{0};
std::atomic<uint32_t> g_write_short{0};
std::atomic<uint32_t> g_write_calls{0};
std::atomic<uint32_t> g_max_write_us{0};
std::atomic<uint32_t> g_last_write_us{0};

}  // namespace

bool i2s_tx_init() {
  if (g_initialized) {
    return true;
  }

  // Reinstall driver in duplex slave RX+TX mode. Using the same DMA RX
  // sizing as audio_io so the RX side is byte-for-byte identical to its
  // RX-only configuration; the TX side gets its own DMA pool sized for
  // low latency (kDmaTxBufLenFrames).
  //
  // The legacy driver doesn't allow asymmetric DMA pool sizes between
  // RX and TX in a single install — both halves share dma_buf_count and
  // dma_buf_len. Empirically, audio_io ships with 6 x 1024 frames; we
  // keep that for RX since the encoder happily consumes 20 ms blocks
  // (960 frames) from the larger buffers, and use the SAME sizing on
  // TX so DMA doesn't get fragmented. 6 x 1024 = ~128 ms playback
  // jitter cushion which is more than enough to ride out a momentary
  // WS hiccup.
  //
  // If you want tighter playback latency later, the right answer is to
  // migrate to the IDF 5.x i2s_std API which lets each direction have
  // its own DMA pool.
  i2s_driver_uninstall(kI2sPort);

  i2s_config_t cfg = {};
  cfg.mode = static_cast<i2s_mode_t>(I2S_MODE_SLAVE | I2S_MODE_RX | I2S_MODE_TX);
  cfg.sample_rate = kSampleRateHz;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = kDmaBufCount;       // shared with RX (audio_io)
  cfg.dma_buf_len = kDmaBufLenFrames;     // shared with RX (audio_io)
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;          // auto-clear TX DMA on underrun so a stall
                                          // doesn't loop the last sample (audible click)
  cfg.fixed_mclk = 0;
  cfg.mclk_multiple = I2S_MCLK_MULTIPLE_DEFAULT;
  cfg.bits_per_chan = I2S_BITS_PER_CHAN_DEFAULT;

  esp_err_t err = i2s_driver_install(kI2sPort, &cfg, /*queue_size=*/0,
                                     /*event_queue=*/nullptr);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "duplex i2s_driver_install failed: %s",
             esp_err_to_name(err));
    return false;
  }

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = kI2sBclkPin;       // GPIO8, clocked by XMOS
  pins.ws_io_num  = kI2sLrclkPin;      // GPIO7, clocked by XMOS
  pins.data_out_num = kI2sDoutPinTx;   // GPIO44 -> codec DIN
  pins.data_in_num  = kI2sDinPin;      // GPIO43, from codec DOUT (post-AEC)

  err = i2s_set_pin(kI2sPort, &pins);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "duplex i2s_set_pin failed: %s", esp_err_to_name(err));
    i2s_driver_uninstall(kI2sPort);
    return false;
  }

  // Zero the TX DMA buffers so the speaker starts from silence rather
  // than whatever bus garbage was pre-loaded. i2s_zero_dma_buffer is the
  // documented way; it walks the DMA descriptors and writes zero across
  // all buffers in the TX pool.
  err = i2s_zero_dma_buffer(kI2sPort);
  if (err != ESP_OK) {
    ESP_LOGW(kTag, "i2s_zero_dma_buffer warning: %s", esp_err_to_name(err));
    // Non-fatal; first sample of audio will overwrite the silence anyway.
  }

  g_initialized = true;
  ESP_LOGI(kTag,
           "I2S0 duplex up: RX %d, TX %d (slave); %lu Hz %u-bit %u ch; "
           "DMA %dx%d frames per direction",
           kI2sDinPin, kI2sDoutPinTx,
           static_cast<unsigned long>(kSampleRateHz),
           kBitsPerSample, kChannelCount,
           kDmaBufCount, kDmaBufLenFrames);
  return true;
}

size_t i2s_tx_write_block(const int32_t* buf, size_t frames,
                          uint32_t timeout_ms) {
  if (!g_initialized || buf == nullptr || frames == 0) {
    return 0;
  }
  const size_t want_bytes = frames * kBytesPerFrame;
  size_t got_bytes = 0;
  const uint32_t t0 = micros();
  esp_err_t err = i2s_write(kI2sPort, buf, want_bytes, &got_bytes,
                            pdMS_TO_TICKS(timeout_ms));
  const uint32_t t1 = micros();
  const uint32_t dt = t1 - t0;
  g_last_write_us.store(dt, std::memory_order_relaxed);
  uint32_t prev_max = g_max_write_us.load(std::memory_order_relaxed);
  while (dt > prev_max && !g_max_write_us.compare_exchange_weak(prev_max, dt)) {}

  if (err != ESP_OK) {
    g_write_errors.fetch_add(1, std::memory_order_relaxed);
    return 0;
  }
  if (got_bytes == 0) {
    return 0;
  }
  if (got_bytes < want_bytes) {
    g_write_short.fetch_add(1, std::memory_order_relaxed);
  }
  const size_t got_frames = got_bytes / kBytesPerFrame;
  g_bytes_out.fetch_add(got_bytes, std::memory_order_relaxed);
  g_frames_out.fetch_add(got_frames, std::memory_order_relaxed);
  g_write_calls.fetch_add(1, std::memory_order_relaxed);
  return got_frames;
}

I2sTxStats i2s_tx_get_stats() {
  I2sTxStats s = {};
  s.bytes_out     = g_bytes_out.load(std::memory_order_relaxed);
  s.frames_out    = g_frames_out.load(std::memory_order_relaxed);
  s.write_errors  = g_write_errors.load(std::memory_order_relaxed);
  s.write_short   = g_write_short.load(std::memory_order_relaxed);
  s.write_calls   = g_write_calls.load(std::memory_order_relaxed);
  s.max_write_us  = g_max_write_us.load(std::memory_order_relaxed);
  s.last_write_us = g_last_write_us.load(std::memory_order_relaxed);
  return s;
}

// ---------------------------------------------------------------------------
// PCM helpers
// ---------------------------------------------------------------------------

int16_t upsample_3x_linear_mono(const int16_t* in, size_t in_samples,
                                int16_t last_in, int16_t* out) {
  // For each input sample y[n] given previous y[n-1] (= last_in for n=0,
  // = in[n-1] otherwise), emit three output samples:
  //   out[3n+0] = y[n-1] + (y[n] - y[n-1]) * 0/3   = y[n-1]
  //   out[3n+1] = y[n-1] + (y[n] - y[n-1]) * 1/3
  //   out[3n+2] = y[n-1] + (y[n] - y[n-1]) * 2/3
  // We use 32-bit intermediate math to avoid overflow on the diff.
  int32_t prev = static_cast<int32_t>(last_in);
  for (size_t n = 0; n < in_samples; ++n) {
    const int32_t cur = static_cast<int32_t>(in[n]);
    const int32_t diff = cur - prev;
    out[3 * n + 0] = static_cast<int16_t>(prev);
    out[3 * n + 1] = static_cast<int16_t>(prev + (diff * 1) / 3);
    out[3 * n + 2] = static_cast<int16_t>(prev + (diff * 2) / 3);
    prev = cur;
  }
  return static_cast<int16_t>(prev);
}

void mono_int16_to_stereo_int32(const int16_t* mono_48k,
                                size_t mono_samples,
                                int32_t* stereo_48k_int32) {
  // Promote int16 → int32 by left-shifting 16 so the meaningful audio
  // sits in the upper bits of the slot. Duplicate to L and R.
  for (size_t i = 0; i < mono_samples; ++i) {
    const int32_t s = static_cast<int32_t>(mono_48k[i]) << 16;
    stereo_48k_int32[2 * i + 0] = s;  // left
    stereo_48k_int32[2 * i + 1] = s;  // right
  }
}

}  // namespace audio
}  // namespace alfred
