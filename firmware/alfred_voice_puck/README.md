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
* Opus encode (SAL-4001), WebSocket client (SAL-4003), TLS, and Wi-Fi
  provisioning land in later Phase 1 tickets.

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

## Future hooks

When SAL-4001 (Opus encode) lands, vendor libopus under `lib/opus/` and add
`lib_deps = file://lib/opus` to the `[env:arduino]` block. Tap into
`audio_io_read_block()` directly; the 20 ms block size already matches
Opus framing.

When SAL-4003 (WebSocket transport) lands, add `lib_deps = WebSockets` and
keep TLS roots in `data/` so they ship in the SPIFFS partition.

When SAL-4014 (I2S TX to codec) lands, extend `audio_io` with a TX path
on GPIO44 sharing the same BCK/WS pins (full duplex on I2S0).
