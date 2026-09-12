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
#   2. Installs system packages: mpv (radio playback), alsa-utils
#      (volume control), curl (librespot's onevent hook).
#   3. Sets up a Python venv with this repo's dependencies.
#   4. Installs and enables the myndless systemd service.
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
sudo raspi-config nonint do_serial_hw 1   # enable UART hardware
sudo raspi-config nonint do_serial_cons 0 # disable login shell on serial
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

python3 -m venv "$INSTALL_DIR/.venv"
sudo "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
sudo chown -R myndless:myndless "$INSTALL_DIR"

echo "==> Installing systemd service"
sudo cp "$INSTALL_DIR/systemd/myndless.service" /etc/systemd/system/myndless.service
sudo systemctl daemon-reload
sudo systemctl enable myndless.service

cat <<EOF

Done. A reboot is needed for the UART/Bluetooth changes to take effect:

    sudo reboot

After rebooting:

    sudo systemctl start myndless
    sudo systemctl status myndless
    journalctl -u myndless -f

The web UI will be at http://<pi-hostname-or-ip>:8080/
EOF
