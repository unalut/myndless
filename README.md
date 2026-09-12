<img src="webui/static/logo.svg" alt="myndless" height="56">

# myndless

A from-scratch Pi-side replacement for moOde on a [MYNDberry](https://blog.teufelaudio.com/project-myndberry/)
mod (Teufel MYND speaker + Raspberry Pi Zero 2 W). Instead of moOde + the
official "RpiLink" daemon, this talks the MYND's **Actionslink** protocol
directly with its own implementation, and runs its own internet radio +
Spotify Connect + web UI stack on top.

## Status

- [x] Actionslink protocol reverse engineered from the open-source MYND MCU
      firmware (framing, CRC, message schema) — see "Protocol notes" below.
- [x] `myndlink`: a Python implementation of the Pi-side ("BT chip" role) of
      Actionslink — HDLC framing, CRC-8, request/response/event dispatch.
- [x] `daemon/orchestrator.py`: wires Actionslink events/requests to real
      audio backends (power, volume, source switching, sound icons, LED
      queries) and stubs out the Bluetooth-management requests we don't
      implement so the MCU is never left hanging.
- [x] Internet radio backend (`daemon/radio.py`, via `mpv`).
- [x] Spotify Connect backend (`daemon/spotify.py`, via `librespot`).
- [x] Web UI (`webui/`, Flask) for source/station/volume control, plus
      searching [Radio Browser](https://www.radio-browser.info/) to add new
      internet radio stations on the fly (persisted to `stations.json`).
- [x] systemd unit + Raspberry Pi OS setup script (`scripts/setup_pi.sh`).
- [x] **Verified against real hardware** (a real MYND + MYNDberry board):
      the Actionslink UART link (framing, handshake, live button/event
      traffic), the I2S audio path (`dtoverlay=hifiberry-dac`, audible
      output), the full daemon + web UI running as a systemd service
      (orchestrator handshake with the MCU, station playback, software
      volume control all confirmed working end-to-end), and now **Spotify
      Connect itself**: `librespot` installed via the Raspotify project's
      prebuilt binary (see `scripts/setup_pi.sh`), phone shows "myndless" as
      a Connect target, audio plays through the MYND — see "First run on
      real hardware" for the exact steps and what's still open.

## Why not moOde?

MYNDberry's official stack is moOde OS plus an "RpiLink" daemon - it turns
out that *is* open source too (just easy to miss: it lives on the
`MYNDberry` branch of [teufelaudio/mynd-firmware](https://github.com/teufelaudio/mynd-firmware),
under `Projects/Mynd/src/tasks/rpi/daemon_install/`, not `main`). This
project doesn't use it, though - partly because that was discovered only
after already reimplementing the peer side from the protocol definitions
directly, and partly because the goal here was a custom Pi-side application
(different audio backends, own web UI) rather than moOde. The official
daemon is still a genuinely useful reference if something here disagrees
with it - see `reference/mynd-firmware` (branch `MYNDberry`) after running
`git fetch origin MYNDberry && git checkout MYNDberry` in that clone.

The protocol itself - **Actionslink** - is unambiguously open source either
way, as part of the same repo.

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
  radio_browser.py    Radio Browser API client (station search, used by the web UI)
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

# The official RpiLink daemon (a useful reference/cross-check - see "Why not
# moOde?") lives on the MYNDberry branch, not main:
git fetch origin MYNDberry --depth 1
git checkout -b MYNDberry FETCH_HEAD
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

Confirmed against a real MYND + MYNDberry board (Pi Zero 2 W), start to
finish: physical assembly, UART link, I2S audio path, and the full daemon +
web UI running as a systemd service (MCU handshake, station playback,
software volume control) all work as described below.

1. Follow the [MYNDberry blog post](https://blog.teufelaudio.com/project-myndberry/)
   (or the more detailed [official wiki guide](https://github.com/teufelaudio/mynd-firmware/wiki/MYNDberry_initial_setup))
   for the physical mod (adapter PCB install) - that part is unchanged, and
   works with plain **Raspberry Pi OS Lite** instead of moOde.
2. Enable the UART and disable the serial console. `raspi-config`'s
   `nonint` flags are inverted from what the names suggest - verified on a
   real Pi Zero 2 W:
   ```bash
   sudo raspi-config nonint do_serial_hw 0   # 0 = enable (not 1)
   sudo raspi-config nonint do_serial_cons 1 # 1 = disable (not 0)
   ```
3. Enable I2S audio out - the MCU owns I2C/DSP configuration of the amps
   (TAS5825P/TAS5805M) itself, so the Pi only needs to feed raw I2S PCM.
   The official image uses `dtoverlay=hifiberry-dac` for exactly this, and
   it's confirmed working (audible output) on real hardware:
   ```bash
   CONFIG_TXT=/boot/firmware/config.txt
   echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_TXT"
   echo "dtoverlay=hifiberry-dac" | sudo tee -a "$CONFIG_TXT"
   sudo reboot
   ```
   After rebooting, `aplay -l` should list `card 0: sndrpihifiberry
   [snd_rpi_hifiberry_dac]`. The DAC has no hardware volume control -
   `scripts/setup_pi.sh` sets up an ALSA softvol wrapper (`/etc/asound.conf`)
   so `amixer`/`daemon/audio.py` still has a "PCM" control to drive.
4. Before trusting the full daemon, verify the link in isolation with
   `scripts/uart_probe.py` (see its docstring) - `--handshake` sends the
   boot sequence and `--listen=N` then logs live MCU traffic (button
   presses, battery events, ...) for N seconds. The MCU stays silent until
   it sees the handshake, so plain passive sniffing sees nothing even on a
   working link.
5. Clone this repo onto the Pi and run `bash scripts/setup_pi.sh` - it
   redoes steps 2-3 (idempotently, safe to re-run) plus installs system
   packages and sets up the systemd service. Install `librespot` first (see
   the script's output for options) if you want Spotify Connect from boot.
6. `sudo systemctl start myndless`, then watch `journalctl -u myndless -f`
   while pressing physical buttons/knobs on the speaker to confirm
   Actionslink requests are arriving and being answered. Open
   `http://<pi-hostname-or-ip>:8080/` for the web UI - picking a station
   there and adjusting the volume slider both take effect immediately on a
   confirmed-working install.

### Gotchas hit during real bring-up (already fixed, worth knowing about)

- If `journalctl -u myndless` shows the service restarting every few
  seconds with a traceback, and `librespot` isn't installed: that's
  already handled (`daemon/spotify.py` logs a warning and skips Spotify
  Connect instead of crashing) as long as you're on a build that includes
  that fix - `git pull` and restart if you hit it.
- If volume control silently does nothing (`{"percent": 0}` from
  `/api/volume`, or a "no ALSA mixer control found" warning in the logs)
  right after a crash-loop like the one above: it was a transient
  side-effect of the rapid restart cycle in this project's own testing,
  not a real config problem - it resolved on its own once the crash loop
  stopped. If it persists on a clean boot, compare `sudo -u myndless
  amixer -c 0 scontrols` (should print `Simple mixer control 'PCM',0`)
  against what the service sees in its logs (now logs amixer's exit
  code/stdout/stderr on failure - see `daemon/audio.py`).

Still open:
- Spotify Connect play/pause/skip from the physical remote/buttons: vanilla
  `librespot` has no local control API for that (see `daemon/spotify.py`),
  only phone-initiated playback has been exercised.
- Whether `set_audio_source`/analog-source handling needs real behavior
  (right now it's acked as a no-op - see the docstring in
  `daemon/orchestrator.py`).
- Sound icon playback (`play_sound_icon`/`stop_sound_icon`) needs actual
  `.wav` files dropped into `assets/sound_icons/` - none are bundled.
- Physical button routing (play/pause/next/prev via `send_avrcp_action`/
  `send_usb_hid_action`) is implemented but not yet exercised on real
  hardware.
