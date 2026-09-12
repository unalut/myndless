"""Spotify Connect backend, wrapping the ``librespot`` binary.

librespot turns the Pi into a Spotify Connect receiver: it shows up as a
device in any Spotify app, and playback is initiated/controlled *remotely*
from there (Spotify's own protocol), not by us. What we do locally is:

  - keep the process running and route its audio to the shared ALSA output
  - get notified of state changes via ``--onevent`` so we can tell the MCU
    (``notify_stream_state``, ``notify_audio_source``)

Caveat: vanilla librespot has no local play/pause/skip control API (that's
intentionally the Connect protocol's job, driven from the controlling app).
Physical play/pause/skip button presses on the speaker therefore can't
control Spotify playback through this backend - see ``play_pause`` etc.
below. If that matters to you, switch this backend to ``spotifyd`` (has an
MPRIS/D-Bus control interface) or a librespot fork with a local control API.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("myndless.spotify")

Spawner = Callable[..., "subprocess.Popen[bytes]"]

# Written to disk and handed to librespot's --onevent: librespot invokes this
# with event details in environment variables (PLAYER_EVENT, TRACK_ID, ...).
# We just forward that as a form-encoded POST to our own event endpoint.
_ONEVENT_SCRIPT = """#!/bin/sh
curl -s -m 2 -X POST "http://127.0.0.1:{port}/internal/spotify-event" \\
    --data-urlencode "event=${{PLAYER_EVENT}}" \\
    --data-urlencode "track_id=${{TRACK_ID}}" \\
    --data-urlencode "volume=${{VOLUME}}" \\
    >/dev/null 2>&1 &
"""


def write_onevent_script(path: str, event_port: int) -> None:
    script_path = Path(path)
    script_path.write_text(_ONEVENT_SCRIPT.format(port=event_port))
    script_path.chmod(0o755)


class SpotifyBackend:
    def __init__(
        self,
        device_name: str = "myndless",
        librespot_bin: str = "librespot",
        alsa_device: str = "default",
        onevent_script_path: Optional[str] = None,
        spawner: Spawner = subprocess.Popen,
    ):
        self._device_name = device_name
        self._librespot_bin = librespot_bin
        self._alsa_device = alsa_device
        self._onevent_script_path = onevent_script_path
        self._spawn = spawner
        self._proc: Optional[subprocess.Popen] = None
        self._is_active = False  # True between "playing" / "active" events and "stopped" / "inactive"

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self.is_running():
            return
        args = [
            self._librespot_bin,
            "--name", self._device_name,
            "--backend", "alsa",
            "--device", self._alsa_device,
            "--bitrate", "320",
            "--initial-volume", "100",  # actual level is controlled via the ALSA mixer, not librespot
        ]
        if self._onevent_script_path:
            args += ["--onevent", self._onevent_script_path]
        log.info("starting librespot as %r", self._device_name)
        try:
            self._proc = self._spawn(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            # Most commonly FileNotFoundError - librespot isn't installed/on
            # PATH yet. Not fatal: everything else (radio, Actionslink, web
            # UI) should keep working: just no Spotify Connect until it is.
            log.warning(
                "could not start %r (not installed / not on PATH?) - Spotify Connect unavailable, "
                "everything else still works. See README for install options.",
                self._librespot_bin,
            )
            self._proc = None

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            log.info("stopping librespot")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._is_active = False

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_active(self) -> bool:
        """True while Spotify Connect is actually the thing making sound
        (as opposed to just running in the background waiting to be picked
        as a Connect target)."""
        return self._is_active

    # ------------------------------------------------------------------ #
    # Called from the onevent HTTP hook (see write_onevent_script)
    # ------------------------------------------------------------------ #

    def handle_event(self, event: str, track_id: str = "", volume: str = "") -> None:
        log.debug("librespot event: %s (track=%s volume=%s)", event, track_id, volume)
        if event in ("playing", "active", "started"):
            self._is_active = True
        elif event in ("paused", "stopped", "inactive", "session_disconnected"):
            self._is_active = False

    # ------------------------------------------------------------------ #
    # Playback control - see module docstring caveat.
    # ------------------------------------------------------------------ #

    def play_pause(self) -> bool:
        log.warning("play/pause requested but vanilla librespot has no local control API - ignored")
        return False

    def next_track(self) -> bool:
        log.warning("next-track requested but vanilla librespot has no local control API - ignored")
        return False

    def previous_track(self) -> bool:
        log.warning("previous-track requested but vanilla librespot has no local control API - ignored")
        return False
