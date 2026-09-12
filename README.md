# myndless

A from-scratch Pi-side replacement for moOde on a [MYNDberry](https://blog.teufelaudio.com/project-myndberry/)
mod (Teufel MYND speaker + Raspberry Pi Zero 2 W). Instead of moOde + the
closed-source "RpiLink" daemon, this talks the MYND's real, open-source
**Actionslink** protocol directly, and runs its own internet radio + Spotify
Connect + web UI stack on top.

## Status

- [x] Actionslink protocol reverse engineered from the open-source MYND MCU
      firmware (framing, CRC, message schema) — see "Protocol notes" below.
- [x] `myndlink`: a Python implementation of the Pi-side ("BT chip" role) of
      Actionslink — HDLC framing, CRC-8, request/response/event dispatch.
      Verified end-to-end against a simulated MCU over a pty.
- [x] `daemon/orchestrator.py`: wires Actionslink events/requests to real
      audio backends (power, volume, source switching, sound icons, LED
      queries) and stubs out the Bluetooth-management requests we don't
      implement so the MCU is never left hanging.
- [x] Internet radio backend (`daemon/radio.py`, via `mpv`).
- [x] Spotify Connect backend (`daemon/spotify.py`, via `librespot`).
- [x] Web UI (`webui/`, Flask) for source/station/volume control.
- [x] systemd unit + Raspberry Pi OS setup script (`scripts/setup_pi.sh`).
- [ ] **Run against real hardware.** Everything above is built and tested
      against a simulated MCU (pty) and mocked subprocesses/ALSA - it has not
      yet been wired up to an actual MYND + MYNDberry board. Expect to spend
      time here: confirming the UART pinout against the physical adapter PCB,
      finding the real ALSA mixer control name for whatever DAC the board
      uses, etc. See "First run on real hardware" below.

## Why not moOde?

MYNDberry's official stack is moOde OS plus a closed-source "RpiLink" daemon
that isn't published anywhere. But the protocol RpiLink speaks to the MYND's
main MCU — **Actionslink** — *is* open source, as part of
[teufelaudio/mynd-firmware](https://github.com/teufelaudio/mynd-firmware). So
instead of depending on the closed daemon, this project re-implements the
peer side of that protocol directly and builds a custom Pi-side application
on top of it.

## Architecture

```
┌──────────────────────────┐        UART (115200 8N1)         ┌────────────────────────────┐
│   MYND main MCU           │◄────────────────────────────────►│  Raspberry Pi Zero 2 W      │
│   (STM32, mynd-firmware)  │      Actionslink protocol         │  (this project)            │
│                           │   over what used to be the        │                            │
│   owns: amp, battery,     │   Bluetooth module's UART pins    │  myndlink/ - protocol lib   │
│   buttons, LEDs, power    │                                   │  daemon/   - orchestrator + │
└──────────────────────────┘                                   │             audio backends  │
                                                                 │  webui/    - Flask control  │
        ALSA (shared output) ◄───────────────┬────────────────►│             UI + API        │
                     ▲                        │                 └────────────────────────────┘
                     │                        │
              mpv (internet radio)     librespot (Spotify Connect)
```

The Pi's role in the Actionslink protocol is exactly the role the original
Bluetooth/Actions co-processor used to play: it receives commands from the
MCU (`set_audio_source`, `set_volume`, `set_power_state`, `play_sound_icon`,
transport controls, ...) and must acknowledge/respond to them, and it can
also push events/requests of its own to the MCU (`notify_system_ready`,
`notify_power_state`, `notify_volume`, `notify_stream_state`, LED
color/brightness queries, battery status, ...). `daemon/orchestrator.py` is
where that's implemented, on top of `myndlink`'s protocol library.

Only one of {radio, spotify} is ever meant to be actually producing sound at
a time: starting radio force-stops `librespot` (there's no local way to
"pause" a remote Spotify Connect session), and Spotify becoming active (via
its `--onevent` hook) stops radio.

## Protocol notes (Actionslink)

Reverse engineered from
`reference/mynd-firmware/Projects/Mynd/external/teufel/libs/actionslink/`.

**Transport** — `src/bsp/bluetooth_uart/bsp_bluetooth_uart.c` +
`src/bsp/board_hw.h`:
- `USART1`, **115200 baud, 8N1, no flow control**. Direct hardware UART (not
  USB-serial) — on the MYNDberry adapter PCB this is wired straight to the
  Pi's GPIO UART pins (`/dev/serial0`).

**Framing** — `src/transport/actionslink_bt_ll.c`, HDLC-style:
- Frame = `0x7E` + byte-stuffed(header + payload) + `0x7E`
- Escape char `0x7D`, escaped bytes are XORed with `0x20`
- 8-byte header: magic (`0x55`) · packet-type+value · transaction id ·
  payload length (u16 LE) · payload CRC-8 · reserved · header CRC-8
- CRC-8: poly `0x07`, init `0x00`, no reflection, xorout `0x00`
- Every frame with a payload (`PROTOBUF` type) must be met with an
  `ACK`/`NACK` frame (same transaction id) before the sender considers it
  delivered. 2 retries, 300ms timeout per attempt.
- Requests additionally expect a matching response message after the ACK.

**Messages** — `proto/eco/message.proto` (Protocol Buffers, vendored into
`myndlink/proto/`): bytes the MCU sends are always an `ActionsLink.FromMcu`
message; bytes we send are always `ActionsLink.ToMcu`. See that file for the
full message catalogue (power, audio source/volume, BT-emulation fields we
stub out, USB HID, LED color/brightness, battery, generic app passthrough).

**Hardware** — `reference/mynd-hardware/MYNDberry/` (KiCad): the MYNDberry
adapter PCB breaks out the Pi's 40-pin header to the MYND's original
Bluetooth-module connector, with a CH340N USB-serial chip on board as well
(most likely for a separate debug console, not the Actionslink link itself —
unconfirmed, verify against the physical board before relying on it).

## Repo layout

```
myndlink/            Actionslink protocol library
  proto/              vendored .proto sources (from mynd-firmware + nanopb)
  pb/                 generated *_pb2.py (regenerate with scripts/gen_proto.sh)
  crc8.py             CRC-8 implementation
  hdlc.py             HDLC framing (encode + incremental parser)
  client.py           ActionslinkClient: transport + dispatch + convenience API
daemon/
  config.py           env-var-driven configuration
  audio.py            ALSA volume control (shells out to amixer)
  radio.py            internet radio playback via mpv + its JSON IPC socket
  spotify.py          Spotify Connect via librespot (process mgmt + onevent hook)
  orchestrator.py     wires myndlink <-> audio backends; the real protocol handlers
  main.py             process entrypoint (python -m daemon.main)
  stations.json       default internet radio station list
webui/
  app.py              Flask app: status/radio/volume/spotify API + onevent receiver
  templates/index.html  control page
systemd/
  myndless.service    systemd unit for the setup script
scripts/
  setup_pi.sh          Raspberry Pi OS setup (UART, packages, venv, service)
  gen_proto.sh         regenerate myndlink/pb/*_pb2.py from myndlink/proto/*.proto
tests/                unit + integration tests, all runnable without real hardware
reference/            vendored upstream repos for protocol/hardware reference (gitignored)
  mynd-firmware/       github.com/teufelaudio/mynd-firmware
  mynd-hardware/        github.com/teufelaudio/mynd-hardware
```

## Setting up `reference/`

`reference/` is gitignored (it's upstream Teufel repos, not ours to version)
but the protocol/hardware notes above point into it. Recreate it with:

```bash
mkdir -p reference && cd reference
git clone --depth 1 https://github.com/teufelaudio/mynd-firmware.git
git clone --depth 1 https://github.com/teufelaudio/mynd-hardware.git
cd mynd-firmware
git submodule update --init --depth 1 Projects/Mynd/external/thirdparty/nanopb
```

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # adds grpcio-tools (proto codegen) + pyflakes
```

Run the tests (no hardware, no mpv/librespot/ALSA required - everything's
mocked or run against a simulated MCU over a pty):

```bash
source .venv/bin/activate
for f in tests/test_*.py; do python "$f" || break; done
```

Regenerate the protobuf bindings after touching `myndlink/proto/`:

```bash
bash scripts/gen_proto.sh
```

## Using `myndlink` directly

```python
from myndlink.client import ActionslinkClient
import system_pb2  # from myndlink/pb, already on sys.path via client.py

client = ActionslinkClient("/dev/serial0")

def handle_set_audio_source(source, seq):
    print("MCU wants source", source.source)
    import common_pb2, error_pb2
    result = common_pb2.Result()
    result.status.code = error_pb2.Code.Success
    return result

client.on_request("set_audio_source", handle_set_audio_source)
client.start()
client.notify_system_ready()
client.notify_power_state(system_pb2.PowerState.ON)
```

`daemon/orchestrator.py` is the fuller, real implementation of this pattern -
start there if you're extending protocol handling.

## Running it (dev machine, no MYND attached)

You can run the whole stack without hardware to poke at the web UI - the
Actionslink client will just fail to open `/dev/serial0` unless you point it
at a pty (see `tests/test_orchestrator.py` for how the tests fake one up).
For a real dry run you need at least a Pi (or any Linux box) with a serial
port, `mpv`, and optionally `librespot` installed:

```bash
pip install -r requirements.txt
export MYNDLESS_SERIAL_PORT=/dev/ttyUSB0   # or wherever
python -m daemon.main
```

Then open `http://localhost:8080/`.

## First run on real hardware

1. Follow the [MYNDberry blog post](https://blog.teufelaudio.com/project-myndberry/)
   for the physical mod (adapter PCB install) - that part is unchanged.
2. Flash **Raspberry Pi OS Lite** (not moOde) to the SD card.
3. Clone this repo onto the Pi and run `bash scripts/setup_pi.sh` - it
   enables the hardware UART, disables the serial console, installs system
   packages, and sets up the systemd service. Install `librespot` first (see
   the script's output for options) if you want Spotify Connect from boot.
4. `sudo reboot`, then `sudo systemctl start myndless`.
5. Find the real ALSA mixer control name for your DAC (`amixer scontrols`)
   and set `MYNDLESS_ALSA_MIXER` in `systemd/myndless.service` if
   auto-detection (`daemon/audio.py`) doesn't pick the right one.
6. Watch `journalctl -u myndless -f` while triggering physical buttons/knobs
   on the speaker to confirm Actionslink requests are arriving and being
   answered.

Things worth double-checking against the physical board rather than
assuming, since none of this has touched real hardware yet:
- The UART pinout (`reference/hardware/MYNDberry/MYNDberry.kicad_sch`) -
  confirm it's wired to the Pi's primary UART and not, say, the
  Bluetooth-shared mini-UART.
- Whether `set_audio_source`/analog-source handling needs real behavior
  (right now it's acked as a no-op - see the docstring in
  `daemon/orchestrator.py`).
- Sound icon playback (`play_sound_icon`/`stop_sound_icon`) needs actual
  `.wav` files dropped into `assets/sound_icons/` - none are bundled.
