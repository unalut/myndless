"""Central configuration for the myndless daemon, sourced from environment
variables so the same code runs unmodified in dev (pty loopback) and on the
real Pi (systemd unit sets the env vars).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    # Actionslink / UART
    serial_port: str = os.environ.get("MYNDLESS_SERIAL_PORT", "/dev/serial0")
    serial_baudrate: int = _env_int("MYNDLESS_SERIAL_BAUDRATE", 115200)

    # ALSA
    # "0" is a card *index*, not the "default" PCM/ctl alias - amixer's -c
    # flag wants a number (or exact card short-id); "default" doesn't
    # reliably resolve there. Confirmed against real MYNDberry hardware:
    # dtoverlay=hifiberry-dac always enumerates as card 0.
    alsa_card: str = os.environ.get("MYNDLESS_ALSA_CARD", "0")
    alsa_mixer: str = os.environ.get("MYNDLESS_ALSA_MIXER", "")  # "" = auto-detect
    # Separate from alsa_card on purpose: this is the PCM *device name* mpv/
    # librespot output to (routed through the softvol wrapper in
    # /etc/asound.conf - see scripts/setup_pi.sh), not the amixer card index.
    alsa_pcm_device: str = os.environ.get("MYNDLESS_ALSA_PCM_DEVICE", "default")
    volume_step_percent: int = _env_int("MYNDLESS_VOLUME_STEP", 5)

    # Radio
    radio_stations_file: str = os.environ.get("MYNDLESS_RADIO_STATIONS", "daemon/stations.json")
    mpv_socket_path: str = os.environ.get("MYNDLESS_MPV_SOCKET", "/tmp/myndless-mpv.sock")

    # Spotify (librespot)
    librespot_bin: str = os.environ.get("MYNDLESS_LIBRESPOT_BIN", "librespot")
    librespot_device_name: str = os.environ.get("MYNDLESS_LIBRESPOT_NAME", "myndless")
    spotify_event_port: int = _env_int("MYNDLESS_SPOTIFY_EVENT_PORT", 5901)

    # Sound icons (short local chimes played on MCU request)
    sound_icons_dir: str = os.environ.get("MYNDLESS_SOUND_ICONS_DIR", "assets/sound_icons")

    # Web UI
    web_host: str = os.environ.get("MYNDLESS_WEB_HOST", "0.0.0.0")
    web_port: int = _env_int("MYNDLESS_WEB_PORT", 8080)

    # Misc
    device_name: str = os.environ.get("MYNDLESS_DEVICE_NAME", "myndless")
    firmware_version: tuple[int, int, int] = (0, 1, 0)


CONFIG = Config()
