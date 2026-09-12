# myndless

A from-scratch Pi-side replacement for moOde on a [MYNDberry](https://blog.teufelaudio.com/project-myndberry/)
mod (Teufel MYND speaker + Raspberry Pi Zero 2 W). Instead of moOde + the
closed-source "RpiLink" daemon, this talks the MYND's real, open-source
**Actionslink** protocol directly.

## Status

- [x] Actionslink protocol reverse engineered from the open-source MYND MCU
      firmware (framing, CRC, message schema) — see "Protocol notes" below.
- [x] `myndlink`: a Python implementation of the Pi-side ("BT chip" role) of
      Actionslink — HDLC framing, CRC-8, request/response/event dispatch.
      Verified end-to-end against a simulated MCU over a pty
      (`tests/test_client_loopback.py`).
- [ ] Orchestrator daemon tying `myndlink` events to audio backends (power,
      volume, source switching, LED/UI sync).
- [ ] Internet radio backend.
- [ ] Spotify Connect backend (librespot).
- [ ] Web UI.
- [ ] systemd units + Raspberry Pi OS setup script.

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
┌─────────────────────────┐        UART (115200 8N1)        ┌───────────────────────────┐
│   MYND main MCU          │◄───────────────────────────────►│  Raspberry Pi Zero 2 W     │
│   (STM32, mynd-firmware) │   Actionslink protocol           │  (this project)           │
│                          │   over what used to be the        │                           │
│  owns: amp, battery,      │   Bluetooth module's UART pins   │  myndlink/  - protocol lib │
│  buttons, LEDs, power     │                                  │  daemon/    - orchestrator │
└─────────────────────────┘                                  │  services/  - radio/spotify│
                                                               │  webui/     - control UI   │
                                                               └───────────────────────────┘
```

The Pi's role in this protocol is exactly the role the original Bluetooth/
Actions co-processor used to play: it receives commands from the MCU
(`set_audio_source`, `set_volume`, `set_power_state`, `play_sound_icon`, ...)
and must acknowledge/respond to them, and it can also push events/requests of
its own to the MCU (`notify_system_ready`, `notify_power_state`,
`notify_volume`, LED color/brightness control, battery status, ...).

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
can ignore, USB HID, LED color/brightness, battery, generic app passthrough).

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
tests/
  test_client_loopback.py   full protocol round-trip against a simulated MCU (no hardware needed)
reference/            vendored upstream repos for protocol/hardware reference
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

## Using `myndlink`

```python
from myndlink.client import ActionslinkClient
import system_pb2, audio_pb2  # from myndlink/pb, already on sys.path via client.py

client = ActionslinkClient("/dev/serial0")

def handle_set_audio_source(source, seq):
    print("MCU wants source", source.source)
    # ... actually switch ALSA routing/whatever here ...
    import common_pb2, error_pb2
    result = common_pb2.Result()
    result.status.code = error_pb2.Code.Success
    return result

client.on_request("set_audio_source", handle_set_audio_source)
client.start()
client.notify_system_ready()
client.notify_power_state(system_pb2.PowerState.ON)
```

Run the no-hardware-required protocol test with:

```bash
source .venv/bin/activate
python tests/test_client_loopback.py
```

## Regenerating the protobuf bindings

```bash
source .venv/bin/activate
bash scripts/gen_proto.sh
```

## Next steps

1. **Orchestrator daemon** (`daemon/`): wires `ActionslinkClient` events to
   real system state — power on/off (suspend/shutdown the Pi or just mute),
   volume (ALSA mixer), audio source switching, LED sync.
2. **Audio backends**: internet radio (MPD or a direct ffmpeg/gstreamer
   pipeline) and Spotify Connect (`librespot`), both routed to the same ALSA
   output feeding the MYND's amp over I2S.
3. **Web UI** (`webui/`): Flask app for source/station/volume control.
4. **Deployment**: Raspberry Pi OS Lite (not moOde) + systemd units +
   `raspi-config` UART setup (enable hardware UART, disable serial console)
   + setup script.

Before wiring up real hardware: verify the UART pinout against the physical
MYNDberry board/schematic (`reference/mynd-hardware/MYNDberry/MYNDberry.kicad_sch`)
rather than assuming — this project has not yet been run against a real MYND.
