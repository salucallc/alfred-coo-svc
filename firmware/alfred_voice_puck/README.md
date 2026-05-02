# alfred_voice_puck firmware

Firmware for the Alfred voice puck: a Seeed XIAO ESP32-S3 mounted on a Seeed
ReSpeaker XVF3800 4-mic array carrier with a soldered 10 W speaker. Talks to
the existing Alfred backend (`alfred_coo.fleet_voice` gateway, then `soul-svc`)
over a WebSocket carrying Opus audio + JSON control messages.

This subtree lives inside `alfred-coo-svc-hawkman` so device firmware,
gateway, and protocol can evolve in lockstep on a single PR.

## Status

* Ticket SAL-3999 (P1.1, project skeleton): this commit.
* I2S RX, Opus encode, WebSocket client, TLS, and Wi-Fi provisioning land in
  later Phase 1 tickets (SAL-4000..SAL-4002+).

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
  src/main.cpp            # boot banner, I2C smoke test, heartbeat
  include/                # public headers (empty for now)
  lib/                    # vendored libraries (empty for now; libopus lands at SAL-4001)
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

## Future hooks

When SAL-4000 (I2S RX) lands, replace the `runI2cSmokeTest` body with a
proper codec init that also configures the TLV320 for 4-channel I2S slave
mode at 16 kHz. The smoke probe stays as a pre-flight assertion.

When SAL-4001 (Opus encode) lands, vendor libopus under `lib/opus/` and add
`lib_deps = file://lib/opus` to the `[env:arduino]` block.

When SAL-4002 (WebSocket transport) lands, add `lib_deps = WebSockets` and
keep TLS roots in `data/` so they ship in the SPIFFS partition.
