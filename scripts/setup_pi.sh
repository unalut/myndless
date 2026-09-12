#!/usr/bin/env bash
# One-time setup for a Raspberry Pi OS Lite install running myndless
# (instead of moOde) on a MYNDberry-modified MYND speaker.
#
# Run this ON THE PI, as a user with sudo, from the myndless repo root:
#   bash scripts/setup_pi.sh
#
# What it does:
#   1. Frees up the Pi's hardware UART for Actionslink (disables the
#      serial console, moves Bluetooth off the PL011 UART).
#   2. Enables I2S audio out to the MYND's amps (dtoverlay=hifiberry-dac -
#      confirmed against real hardware: the MCU owns I2C/DSP config of the
#      TAS5825P/TAS5805M amps, the Pi just needs to feed raw I2S PCM, same
#      overlay the official MYNDberry moOde image uses) and sets up an ALSA
#      softvol control (the DAC has no hardware mixer of its own).
#   3. Installs system packages: mpv (radio playback), alsa-utils
#      (volume control), curl (librespot's onevent hook).
#   4. Sets up a Python venv with this repo's dependencies.
#   5. Installs and enables the myndless systemd service.
#
# What it does NOT do: install librespot (packaging varies too much across
# Pi OS versions/architectures to script reliably) - see the librespot
# section below and finish that step by hand first.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run this as your normal user (it uses sudo where needed), not as root." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/myndless"

echo "==> Enabling the hardware UART for Actionslink"
# raspi-config's nonint booleans are inverted from what you'd guess (0 = yes/enable,
# 1 = no/disable) - verified against a real Pi Zero 2 W: do_serial_hw 0 is the one
# that actually produces enable_uart=1, and do_serial_cons 1 is the one that
# actually strips console=serial0,... from cmdline.txt.
sudo raspi-config nonint do_serial_hw 0   # enable UART hardware
sudo raspi-config nonint do_serial_cons 1 # disable login shell on serial
if ! grep -q "^dtoverlay=disable-bt" /boot/firmware/config.txt 2>/dev/null && \
   ! grep -q "^dtoverlay=disable-bt" /boot/config.txt 2>/dev/null; then
    CONFIG_TXT="/boot/firmware/config.txt"
    [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"
    echo "dtoverlay=disable-bt" | sudo tee -a "$CONFIG_TXT" >/dev/null
    sudo systemctl disable hciuart.service 2>/dev/null || true
    echo "    Added 'dtoverlay=disable-bt' to $CONFIG_TXT (frees the full PL011"
    echo "    UART from Bluetooth - the Pi's own Bluetooth radio is unused here"
    echo "    anyway, since MYNDberry replaces the speaker's BT module with the Pi)."
fi

echo "==> Enabling I2S audio out (dtoverlay=hifiberry-dac)"
CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"
grep -q "^dtparam=i2c_arm=on" "$CONFIG_TXT" || echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_TXT" >/dev/null
grep -q "^dtoverlay=hifiberry-dac" "$CONFIG_TXT" || echo "dtoverlay=hifiberry-dac" | sudo tee -a "$CONFIG_TXT" >/dev/null

echo "==> Setting up software volume control (the DAC has no hardware mixer)"
if [[ ! -f /etc/asound.conf ]]; then
    sudo tee /etc/asound.conf >/dev/null <<'ASOUNDCONF'
# ALSA softvol wrapper: dtoverlay=hifiberry-dac has no hardware volume
# control, so this creates a software "PCM" mixer control on card 0 that
# amixer/daemon/audio.py can drive, and routes the system default device
# through it so mpv/librespot don't need to know about it.
pcm.hifiberry {
    type hw
    card 0
}
pcm.softvol {
    type softvol
    slave.pcm "hifiberry"
    control {
        name "PCM"
        card 0
    }
    min_dB -51.0
    max_dB 0.0
}
ctl.softvol {
    type hw
    card 0
}
pcm.!default {
    type plug
    slave.pcm "softvol"
}
ASOUNDCONF
fi

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3-venv python3-pip mpv alsa-utils curl git

echo "==> librespot"
if command -v librespot >/dev/null 2>&1; then
    echo "    found: $(command -v librespot)"
else
    cat <<'EOF'
    Not found. librespot isn't packaged consistently enough across Pi OS
    versions/architectures to install here automatically. Pick one:

      - Prebuilt binary: https://github.com/librespot-org/librespot/releases
        (grab the aarch64-unknown-linux-gnu or armv7 build matching your OS,
        drop it somewhere on PATH, e.g. /usr/local/bin/librespot)
      - Build from source (needs Rust): https://github.com/librespot-org/librespot#compiling

    Re-run this script (or just start the systemd service) once it's on PATH.
EOF
fi

echo "==> Setting up $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo rsync -a --delete --exclude .venv --exclude .git --exclude reference \
    "$REPO_DIR"/ "$INSTALL_DIR"/
sudo useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin myndless 2>/dev/null || true
sudo usermod -aG dialout,audio myndless

sudo python3 -m venv "$INSTALL_DIR/.venv"
sudo "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
sudo chown -R myndless:myndless "$INSTALL_DIR"

echo "==> Installing systemd service"
sudo cp "$INSTALL_DIR/systemd/myndless.service" /etc/systemd/system/myndless.service
sudo systemctl daemon-reload
sudo systemctl enable myndless.service

cat <<EOF

Done. A reboot is needed for the UART/Bluetooth/audio changes to take effect:

    sudo reboot

After rebooting:

    sudo systemctl start myndless
    sudo systemctl status myndless
    journalctl -u myndless -f

The web UI will be at http://<pi-hostname-or-ip>:8080/
EOF
