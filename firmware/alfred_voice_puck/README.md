# alfred_voice_puck firmware

Firmware for the Alfred voice puck: a Seeed XIAO ESP32-S3 mounted on a Seeed
ReSpeaker XVF3800 4-mic array carrier with a soldered 10 W speaker. Talks to
the existing Alfred backend (`alfred_coo.fleet_voice` gateway, then `soul-svc`)
over a WebSocket carrying Opus audio + JSON control messages.

This subtree lives inside `alfred-coo-svc-hawkman` so device firmware,
gateway, and protocol can evolve in lockstep on a single PR.

## Status

* Ticket SAL-3999 (P1.1, project skeleton): boot banner, I2C smoke, heartbeat.
* Ticket SAL-4000 (P1.2, I2S slave RX): live capture from XVF3800,
  channel-0 RMS log on UART, dedicated FreeRTOS task on core 1.
* Ticket SAL-4001 (P1.3, Opus voice encode): 16 kHz mono / 20 ms / 24 kbps
  VBR; outbound EncodedFrame queue.
* Ticket SAL-4006 (P1.6, end-to-end voice-in): Wi-Fi STA + WebSocket
  client to fleet_voice gateway. Encoded Opus frames now ship out as
  binary WS messages; inbound JSON control + binary TTS frames are
  logged. TLS + per-device JWT land in later phases.
* Opus decode + I2S TX (SAL-4007) and music downlink (SAL-4014+) land
  in Phase 2 / Phase 3.

## Hardware

| Item | Value |
|------|-------|
| MCU | Seeed XIAO ESP32-S3 (ESP32-S3R8, 8 MB flash, 8 MB Octal PSRAM) |
| Carrier | Seeed ReSpeaker XVF3800 4-mic array |
| Codec | TLV320AIC3104 at I2C 0x18 |
| XMOS DSP | XVF3800 control servicer at I2C 0x2C (requires test5 firmware) |
| I2C pins on XIAO | SDA = GPIO5, SCL = GPIO6 |
| USB-CDC | Native, on the XIAO's own Type-C port (NOT the carrier's XMOS Type-C) |

## Framework choice and rationale

Decision: **PlatformIO + arduino-esp32 framework** for Phase 1, with a
preconfigured `[env:idf]` environment that swaps to pure ESP-IDF when needed.

Trade-off considered:

1. Pure ESP-IDF: cleanest path to esp-adf later, but installation is multi-GB,
   slow on Windows, and adds friction to every contributor environment.
2. arduino-cli with arduino-esp32: already on the bench, but the project
   layout is opinionated and harder to swap to pure IDF without a rewrite.
3. PlatformIO: thin Python install, supports both arduino-esp32 and pure IDF
   from the same project tree, manages toolchain pinning automatically,
   and the PSRAM + custom partition + monitor_filters knobs are first-class.

PlatformIO wins because the framework switch later costs one
`default_envs = idf` change rather than a project restructure. arduino-esp32
sits on top of ESP-IDF, so anything we depend on (I2S driver, esp_log, NVS,
Wi-Fi, mbedTLS) keeps working when we flip.

## Layout

```
firmware/alfred_voice_puck/
  platformio.ini          # build config; default env = arduino, alt env = idf
  partitions/huge_app.csv # custom partition table, 3 MB app slot
  src/main.cpp            # boot banner, I2C smoke, heartbeat, audio task spawn
  include/                # public headers (empty for now)
  lib/audio_io/           # I2S slave RX + RMS metering (SAL-4000)
  lib/                    # vendored libraries (libopus lands at SAL-4001)
  test/                   # PlatformIO unit tests (empty for now)
```

When future tickets land, they should drop into `src/` for application code
and `lib/<component_name>/` for self-contained subsystems (audio_io, codec,
transport).

## Prerequisites

* Python 3.10+
* PlatformIO Core 6.x (`python -m pip install --user platformio`); `pio.exe`
  ends up under `%APPDATA%\Python\Python3xx\Scripts\` on Windows.
* USB driver for the XIAO ESP32-S3 (Windows binds it automatically as a CDC
  serial device on VID 303A PID 1001).

PlatformIO downloads the espressif32 platform and the XTensa toolchain on the
first build; expect roughly 5..15 minutes the first time.

## Build

```powershell
cd Z:\alfred-coo-svc-hawkman\firmware\alfred_voice_puck
python -m platformio run -e arduino
```

## Flash

The XIAO appears as COM5 on the bench minipc. Adjust `--upload-port` if your
host enumerates it differently.

```powershell
python -m platformio run -e arduino -t upload --upload-port COM5
```

If the chip is wedged in a flash-failure loop, hold `BOOT`, tap `RESET`,
release `BOOT` to enter ROM bootloader mode, then re-run upload.

Note: the XIAO ESP32-S3 uses native USB-Serial/JTAG rather than a USB-UART
bridge. The default PlatformIO board definition tries to enter the bootloader
via RTS/DTR pulses (the right move on dev boards with a CP210x), which fails
on the XIAO and produces `A fatal error occurred: No serial data received`.
The `upload_flags = --before usb-reset --after hard-reset` block in
`platformio.ini` switches esptool to USB reset, which is what works here.
If you ever rebase this onto a vanilla `seeed_xiao_esp32s3` config, that
block must come back.

## Monitor

```powershell
python -m platformio device monitor -e arduino --port COM5
```

Exit with `Ctrl-T Ctrl-X` (PlatformIO's miniterm shortcut).

For headless capture (used during CI and the SAL-3999 acceptance run):

```powershell
python -m platformio device monitor -e arduino --port COM5 `
  --quiet --filter time `
  > Z:\_planning\respeaker-xvf3800-alfred\p1_1_skeleton_boot.log
```

## What the boot banner proves

* Native USB-CDC works (you see anything at all).
* PSRAM is enabled (`free_psram` is non-zero, usually ~8 MB minus overhead).
* The custom huge_app partition is honored (`sketch_size` reports the larger
  partition, not the default 1.5 MB).
* The I2C bus is healthy and the XVF3800 control surface still ACKs at 0x2C,
  matching the bench result captured in `p0_i2c.log`.
* The 5 second heartbeat confirms the loop is scheduled and the USB-CDC
  buffer is draining.

## I2S audio capture (SAL-4000)

The XVF3800 carrier ships with the
`respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.7_48k_test5.bin` firmware
flashed via DFU (see top-level handoff). In that firmware the **XMOS DSP is
the I2S master** and emits a 48 kHz / 32-bit / stereo (2-channel) post-AEC
stream. Channel 0 (left) is the AEC-clean ASR signal; channel 1 (right) is
the secondary mix. The XIAO ESP32-S3 sits on the bus as **I2S slave RX**.

### Pin map

| Signal | XIAO GPIO | Notes |
|--------|-----------|-------|
| BCLK   | GPIO8     | clock from XMOS |
| LRCLK / WS | GPIO7 | frame sync from XMOS |
| DIN    | GPIO43    | mic data into XIAO |
| DOUT   | GPIO44    | RESERVED for SAL-4014 (I2S TX to codec) |
| MCLK   | not connected | XMOS does not need an MCLK from the XIAO |

Source of truth for this map is the
[Seeed wiki](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/) and
the
[`formatBCE/Respeaker-XVF3800-ESPHome-integration`](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration)
satellite YAML, both of which agree on the pinout for this exact carrier
SKU. Earlier internal notes that listed `WS=9, DIN=7` were wrong; do not
revert to those.

### I2S config

* port: `I2S_NUM_0`
* mode: `I2S_MODE_SLAVE | I2S_MODE_RX`
* sample rate: 48 000 Hz
* bits per sample: 32 (slot width)
* channel format: `I2S_CHANNEL_FMT_RIGHT_LEFT` (standard stereo)
* communication format: `I2S_COMM_FORMAT_STAND_I2S` (Philips)
* DMA: 6 buffers x 1024 frames = 128 ms total runway, ~21 ms per buffer

The audio capture task is pinned to **core 1** at priority 5, leaving
core 0 free for the Arduino main loop and (later) the Wi-Fi + WebSocket
client. The task reads 20 ms blocks (960 frames) so SAL-4001's Opus encoder
can hook straight into the same block size.

### Reading the RMS log

While the firmware is monitoring, expect lines of the form:

```
[audio rms_ch0=-72.4 dBFS peak_ch0=-58.1 dBFS blocks=25/500ms frames=24576 under=0]
[heartbeat seq=3 uptime=15s heap=294060 psram=8388416 audio_frames=120384 underruns=0 errors=0]
```

* `rms_ch0` — RMS of channel 0 over the 500 ms window in dBFS.
* `peak_ch0` — peak absolute sample over the same window in dBFS.
* `blocks=N/Tms` — N audio blocks consumed in T ms (expect roughly 25 blocks
  for 500 ms at 20 ms per block).
* `under` — i2s_read timeouts since boot (should stay at 0).

Reference values measured on the bench:

| Acoustic state | rms_ch0 | peak_ch0 |
|----------------|---------|----------|
| Quiet room (no one talking) | -70 to -80 dBFS | -55 to -65 dBFS |
| Normal speaking voice at ~30 cm | -45 to -30 dBFS | -25 to -10 dBFS |
| Hand clap near the array | n/a (transient) | 0 to -3 dBFS peak |

A >12 dB delta between the silent and speaking RMS values proves the audio
path is alive and channel-0 selection is correct.

Note: as of SAL-4001 the per-500ms `[audio ...]` line is no longer printed
(the standalone audio task was replaced by the encoder task which embeds
the same DMA pull). The `[heartbeat ...]` and `[opus ...]` lines below
carry the same information plus the codec stats.

## Opus voice encode (SAL-4001)

The encoder pipeline turns audio_io's 48 kHz / 32-bit / stereo blocks into
16 kHz / mono / 20 ms Opus voice frames at ~24 kbps VBR. Per PLAN.md
decision 11 this is the locked codec spec for the uplink (mic to gateway)
voice path. Music downlink stays at native 48 kHz stereo and gets a
separate codec instance once SAL-4014 lands.

### Pipeline per 20 ms block

1. `audio_io_read_block` pulls 960 stereo frames (7680 bytes int32 stereo).
2. Channel 0 (left, AEC-clean) is selected and run through an 11-tap
   Hamming-windowed FIR low-pass (fc ~7 kHz at 48 kHz). FIR state carries
   between calls so the filter is continuous.
3. Decimate by 3, scale int32 to int16 with saturating cast. Result is
   320 int16 samples (20 ms at 16 kHz mono).
4. `opus_encode` with `OPUS_APPLICATION_VOIP`, `OPUS_SET_BITRATE(24000)`,
   `OPUS_SET_VBR(1)`, `OPUS_SET_COMPLEXITY(3)`,
   `OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE)`,
   `OPUS_SET_BANDWIDTH(OPUS_BANDWIDTH_WIDEBAND)`, `OPUS_SET_DTX(1)`,
   `OPUS_SET_INBAND_FEC(1)`, `OPUS_SET_PACKET_LOSS_PERC(5)`.
   Complexity is held at 3 to keep the warm-up encode call under the
   10 ms budget on the XIAO ESP32-S3 at 240 MHz with FIXED_POINT;
   complexity 5 measured 12.4 ms on the warm-up frame which is
   technically still under the 20 ms hard ceiling but exceeds the
   acceptance budget. Quality difference at 24 kbps voice is
   imperceptible per upstream Opus docs.
5. Resulting payload (~30..100 bytes typical, up to 200 reserved) is
   packed into an `EncodedFrame{seq, timestamp_ms, length, data[200]}`
   and pushed into a 100-deep FreeRTOS queue. On overflow the oldest
   frame is dropped (drop-oldest is the right policy for live voice).

### Library choice and licensing note

We pull the codec via `lib_deps = sh123/esp32_opus@^1.0.3` in
`platformio.ini`. The wrapper LICENSE file says GPL-3 but every source
file under its `src/` carries the upstream xiph BSD-3 header (verified by
inspection of `opus.c`, `silk_encoder.c`, `celt_encoder.c`, etc). The
wrapper just defines `FIXED_POINT` and `OPUS_BUILD` and packages the
sources as an Arduino library. If a future licensing review demands
fully owning the source tree, vendor `opus-1.4` under `lib/opus/src/`
and drop the `lib_deps` entry; the application code does not change.

The float build of Opus is explicitly NOT what we want on this target
(per the SAL-4001 brief). `FIXED_POINT` is set in the wrapper's
`config.h` and is the build flavor we're using.

### Task topology

| Task | Core | Priority | Stack | Job |
|------|------|----------|-------|-----|
| loopTask (Arduino) | 0 | 1 | 8 KB | heartbeat every 5 s |
| voice_enc | 1 | 5 | 16 KB | I2S DMA pull + downsample + opus_encode + queue push |
| voice_drain | 0 | 4 | 4 KB | queue pop + per-second log line; SAL-4006 will swap this for the WS sender |

Why the encoder task does both reading and encoding in one loop: keeping
the I2S read and the Opus encode on the same task and same core lets
audio_io's DMA back-pressure us directly when the encoder falls behind
(i2s_read times out -> audio underruns are visible). Splitting them
across tasks would just hide the back-pressure in another queue.

### Reading the Opus log

Expect one `[opus ...]` line per second from the drain task plus the
existing `[heartbeat ...]` every 5 s with the codec stats appended:

```
[opus seq=49 fps=50.0 avg=58.4 min=42 max=74 bps=23360 qd=0/100 tot_frames=50 tot_bytes=2920 enc_us_max=4870 enc_overruns=0 enc_errors=0]
[heartbeat seq=1 uptime=5s heap=270440 psram=8385536 audio_frames=240000 under=0 opus_frames=250 opus_bytes=14600 max_enc_us=4870 over=0]
```

Field meaning:
* `seq` — sequence of the most recent frame popped from the queue.
* `fps` — frames/sec over the 1 s window. Should be ~50 in steady state.
* `avg`, `min`, `max` — bytes per frame across the window.
* `bps` — bits/sec over the wire (avg * fps * 8). Target ~24000 for
  speech, lower for silence (DTX), higher for transients.
* `qd=N/M` — outbound queue depth N out of capacity M. Should stay
  near 0 in normal operation; non-zero means the consumer (currently the
  drain task, eventually the WS sender) is falling behind.
* `enc_us_max` — slowest encode call since boot, in microseconds.
  Hard ceiling is 20000 (20 ms wallclock budget per frame).
* `enc_overruns` — count of encode calls that exceeded the 20 ms
  budget. MUST stay at 0; if non-zero we will starve I2S.
* `enc_errors` — count of `opus_encode` negative returns.

Reference values measured on the bench (SAL-4001 acceptance log
`Z:/_planning/respeaker-xvf3800-alfred/p1_3_opus_encode.log`, COM5,
complexity 3):

| Acoustic state | avg bytes/frame | bps |
|----------------|-----------------|-----|
| Silence (DTX 1-byte frames mixed with ambient) | min=1, avg 30..40 | ~12000..16000 |
| Ambient room noise / mic self-noise | avg 40..50 | ~16000..20000 |
| Speech / tone burst | avg 50..70 | ~20000..28000 |

`enc_us_max` on the XIAO ESP32-S3 at 240 MHz with complexity 3 is
typically 3..5 ms per 20 ms frame in steady state; the very first
encode call after `voice_codec_init()` is the warmup outlier and
measured 8.6 ms in the acceptance run. Both are comfortably under the
10 ms acceptance budget and the 20 ms hard ceiling.

Acceptance run also showed: free heap 297624 B / 320 KB internal,
free PSRAM 8326071 B / 8 MB Octal, both stable over 60+ s with
zero `audio_io.underruns`, `enc_overruns`, or `enc_errors`.

## Wi-Fi + WebSocket transport (SAL-4006)

The encoder pipeline above (audio_io -> voice_codec -> outbound queue)
is now drained by `lib/transport/transport.cpp` instead of the SAL-4001
log-and-drop drain task. Transport responsibilities:

1. Bring up Wi-Fi STA using SSID + password from `secrets.ini`.
   Retries with exponential backoff on disconnect.
2. Open a WebSocket to `ws://<ALFRED_GATEWAY_HOST>:<PORT><PATH>` with
   `Authorization: Bearer <ALFRED_GATEWAY_BEARER>` header.
3. On WS open: send the `hello` JSON control frame so the gateway
   registers the session.
4. Pump: pop `EncodedFrame` from the outbound queue and ship
   `frame.data[0..length]` as a binary WS message. While the WS is
   down, frames are dropped (drop-oldest in the encoder + drop-on-
   offline in the pump keeps audio fresh on reconnect; we never block
   audio capture).
5. Inbound: log every server text frame (welcome, ping, transcript,
   assistant_text), auto-pong, and log the byte length of binary TTS
   frames. Full Opus decode + I2S TX is SAL-4007.
6. Reconnect with exponential backoff if WS drops; ditto for Wi-Fi.

### Library choice

`links2004/WebSockets` (a.k.a. `arduinoWebSockets`) pinned to `^2.4.1`
in `platformio.ini`. This is the maintained arduino-esp32 standard
that ships ws + wss in one client. We use plain `ws://` for the
SAL-4006 smoke; TLS lands when the gateway gets a real cert.

### Threading

The transport task `voice_ws` runs on core 0 at priority 3 (below the
encoder's 5 and the FreeRTOS Wi-Fi stack's 23). It is the SOLE owner
of the WebSocket client, so all socket access is single-threaded; the
links2004 library is not thread-safe. Encoder runs on core 1 and
keeps producing into the queue regardless of network state.

### Heartbeat fields

The 5 s heartbeat now includes transport stats so ops can see Wi-Fi +
WS health from the same one-line view:

```
[heartbeat seq=12 uptime=60s heap=294060 psram=8326071
 audio_frames=288000 under=0 opus_frames=3000 opus_bytes=180000
 max_enc_us=8626 over=0 wifi=1 rssi=42 ws=1
 tx_frames=2998 tx_bytes=179840 rx_text=10 rx_bin=0 drop_off=2]
```

* `wifi=0|1` Wi-Fi STA connected
* `rssi=N`   absolute value of last measured RSSI (dBm)
* `ws=0|1`   WebSocket open
* `tx_frames` / `tx_bytes` outbound binary (Opus) accounting
* `rx_text`  inbound JSON control frame count
* `rx_bin`   inbound binary frame count (TTS Opus, SAL-4007)
* `drop_off` frames dropped because WS was offline at pump time

### secrets.ini schema

Local `secrets.ini` in this directory (gitignored) populates Wi-Fi +
gateway settings via build_flags. Schema is in `secrets.ini.example`;
copy to `secrets.ini` and fill:

| Key | Meaning |
|-----|---------|
| `ALFRED_WIFI_SSID` | Wi-Fi SSID the puck should join |
| `ALFRED_WIFI_PASSWORD` | Wi-Fi password for that SSID |
| `ALFRED_GATEWAY_HOST` | minipc LAN IP for the SAL-4006 smoke (e.g. `192.168.12.134`) |
| `ALFRED_GATEWAY_PORT` | TCP port (default `8091`) |
| `ALFRED_GATEWAY_PATH` | WS path (default `/v1/fleet/voice`) |
| `ALFRED_GATEWAY_BEARER` | bearer token, must match gateway's `FLEET_VOICE_KEY` env |
| `ALFRED_GATEWAY_TLS` | `0` for `ws://`, `1` for `wss://` |

### Expected log lines on a healthy boot

Successful Wi-Fi + WS bring-up looks like:

```
[wifi] connecting to ssid="<your-ssid>"...
[wifi] connected ip=192.168.12.180 rssi=-44 dBm channel=6
[ws] bring-up target=ws://192.168.12.134:8091/v1/fleet/voice
[ws] connected to ws://192.168.12.134:8091/v1/fleet/voice
[ws] hello sent: {"type":"hello","seq":0,"device_id":"alfred-puck-...
[ws] rx text len=... : {"type":"hello","seq":0,...}
[ws] rx text len=... : {"type":"welcome","seq":0,...}
```

When you speak, the gateway buffers frames and (after `start_utterance`
or natural silence) emits:

```
[ws] rx text len=... : {"type":"transcript","seq":1,"text":"hello alfred",...}
[ws] rx text len=... : {"type":"assistant_text","seq":2,"text":"...reply..."}
[ws] rx text len=... : {"type":"audio_start","seq":1,...}
[ws] rx binary len=...   <-- TTS Opus frames; logged but not yet decoded
[ws] rx text len=... : {"type":"audio_end","seq":1,"frames":...,"bytes":...}
```

## Future hooks

When SAL-4007 (Opus decode + I2S TX) lands, the transport's
`WStype_BIN` log line is replaced with a push into an inbound jitter
buffer, which feeds an Opus decoder on core 1, which writes decoded
PCM into audio_io's TX path on GPIO44 sharing the same BCK/WS pins
(full duplex on I2S0). The `audio_start` / `audio_end` JSON envelopes
the gateway already sends are the prep + flush hooks for the decoder.

The downlink music path (SAL-4014+) keeps native 48 kHz stereo and
gets a separate codec instance.
