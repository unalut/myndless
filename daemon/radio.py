"""Internet radio playback via ``mpv``, controlled over its JSON IPC socket.

mpv is used instead of e.g. MPD because it's a single process with no
separate server/db to manage, handles flaky stream reconnects well, and its
``--input-ipc-server`` gives us play/stop/status for free.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("myndless.radio")

Spawner = Callable[..., "subprocess.Popen[bytes]"]


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    url: str
    favicon: str = ""


def load_stations(path: str) -> list[Station]:
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        log.warning("stations file %s not found - no radio stations available", path)
        return []
    return [
        Station(id=item["id"], name=item["name"], url=item["url"], favicon=item.get("favicon", ""))
        for item in raw
    ]


def save_stations(path: str, stations: list[Station]) -> None:
    """Persist the station list back to disk (same shape load_stations reads),
    so stations added at runtime (e.g. via the web UI's Radio Browser search)
    survive a restart."""
    data = [{"id": s.id, "name": s.name, "url": s.url, "favicon": s.favicon} for s in stations]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class RadioPlayer:
    def __init__(
        self,
        stations: list[Station],
        socket_path: str = "/tmp/myndless-mpv.sock",
        mpv_bin: str = "mpv",
        spawner: Spawner = subprocess.Popen,
    ):
        self._stations = {s.id: s for s in stations}
        self._socket_path = socket_path
        self._mpv_bin = mpv_bin
        self._spawn = spawner
        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[Station] = None

    # ------------------------------------------------------------------ #

    def list_stations(self) -> list[Station]:
        return list(self._stations.values())

    def add_station(self, station: Station) -> None:
        self._stations[station.id] = station

    def remove_station(self, station_id: str) -> bool:
        """Remove a station. Returns True if it existed. Stops playback
        first if it's the one currently playing."""
        if station_id not in self._stations:
            return False
        if self._current is not None and self._current.id == station_id:
            self.stop()
        del self._stations[station_id]
        return True

    @property
    def current_station(self) -> Optional[Station]:
        return self._current if self.is_playing() else None

    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ #

    def play(self, station_id: str) -> Station:
        station = self._stations.get(station_id)
        if station is None:
            raise KeyError(f"unknown station id: {station_id!r}")

        self.stop()

        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

        log.info("starting mpv for station %s (%s)", station.name, station.url)
        self._proc = self._spawn(
            [
                self._mpv_bin,
                "--no-video",
                "--idle=no",
                f"--input-ipc-server={self._socket_path}",
                "--volume=100",  # actual level is controlled via the ALSA mixer, not mpv
                station.url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._current = station
        return station

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            log.info("stopping mpv (was playing %s)", self._current.name if self._current else "?")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._current = None

    # ------------------------------------------------------------------ #
    # mpv JSON IPC (best-effort: play() succeeding doesn't depend on this)
    # ------------------------------------------------------------------ #

    def _ipc_command(self, command: list, retries: int = 5, retry_delay: float = 0.1) -> Optional[dict]:
        """Send a command over mpv's JSON IPC socket. Returns the reply, or
        None if the socket isn't up yet / the command failed."""
        for attempt in range(retries):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    sock.connect(self._socket_path)
                    sock.sendall((json.dumps({"command": command}) + "\n").encode())
                    response = sock.recv(4096)
                    return json.loads(response.decode())
            except (OSError, json.JSONDecodeError):
                time.sleep(retry_delay)
        log.warning("mpv IPC command %s failed after %d attempts", command, retries)
        return None

    def toggle_pause(self) -> None:
        self._ipc_command(["cycle", "pause"])

    def play_pause(self) -> None:
        # Internet radio has no real "pause" (the stream keeps flowing) -
        # cycling pause in mpv is close enough for a play/pause button.
        self.toggle_pause()
