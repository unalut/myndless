"""ALSA volume control, shelling out to ``amixer`` rather than depending on
a C extension (pyalsaaudio) — one less thing to compile on a Pi Zero.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Callable

log = logging.getLogger("myndless.audio")

_PERCENT_RE = re.compile(r"\[(\d{1,3})%\]")
_MUTED_RE = re.compile(r"\[(on|off)\]")

# Reasonably common mixer control names across Pi HATs/DACs, in preference order.
_COMMON_MIXER_NAMES = ["PCM", "Digital", "Master", "Speaker", "Headphone"]

# Observed on real hardware: `amixer -c <card> scontrols` occasionally exits 0
# with no controls listed even though the same command run a moment later (or
# by hand) finds them fine - some transient condition around ALSA/softvol
# state right after a process starts, root cause not pinned down. A few
# retries with a short delay reliably works around it.
_AUTODETECT_ATTEMPTS = 4
_AUTODETECT_RETRY_DELAY_S = 0.3

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class AudioBackend:
    def __init__(self, card: str = "default", mixer_name: str = "", step_percent: int = 5, runner: Runner = subprocess.run):
        self._card = card
        self._step = step_percent
        self._run = runner
        self._mixer_name = mixer_name or self._autodetect_mixer()
        if self._mixer_name:
            log.info("using ALSA mixer control %r on card %r", self._mixer_name, card)
        else:
            log.warning("no ALSA mixer control found on card %r - volume control disabled", card)

    # ------------------------------------------------------------------ #

    def _autodetect_mixer(self) -> str:
        for attempt in range(1, _AUTODETECT_ATTEMPTS + 1):
            mixer = self._autodetect_mixer_once(attempt)
            if mixer:
                if attempt > 1:
                    log.info("found ALSA mixer control %r on attempt %d", mixer, attempt)
                return mixer
            if attempt < _AUTODETECT_ATTEMPTS:
                time.sleep(_AUTODETECT_RETRY_DELAY_S)
        return ""

    def _autodetect_mixer_once(self, attempt: int) -> str:
        try:
            result = self._run(
                ["amixer", "-c", self._card, "scontrols"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            log.exception("failed to list ALSA mixer controls (attempt %d)", attempt)
            return ""

        if result.returncode != 0:
            log.warning(
                "amixer -c %s scontrols exited %d on attempt %d - stdout=%r stderr=%r",
                self._card, result.returncode, attempt, result.stdout, result.stderr,
            )

        available = re.findall(r"'([^']+)'", result.stdout)
        for candidate in _COMMON_MIXER_NAMES:
            if candidate in available:
                return candidate
        return available[0] if available else ""

    def _amixer(self, *args: str) -> str:
        result = self._run(
            ["amixer", "-c", self._card, *args],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            log.warning("amixer %s failed: %s", args, result.stderr.strip())
        return result.stdout

    # ------------------------------------------------------------------ #

    def get_volume_percent(self) -> int:
        if not self._mixer_name:
            return 0
        output = self._amixer("sget", self._mixer_name)
        match = _PERCENT_RE.search(output)
        return int(match.group(1)) if match else 0

    def is_muted(self) -> bool:
        if not self._mixer_name:
            return False
        output = self._amixer("sget", self._mixer_name)
        match = _MUTED_RE.search(output)
        return match is not None and match.group(1) == "off"

    def set_volume_percent(self, percent: int) -> int:
        percent = max(0, min(100, percent))
        if not self._mixer_name:
            return 0
        self._amixer("sset", self._mixer_name, f"{percent}%")
        return percent

    def step_up(self) -> int:
        return self.set_volume_percent(self.get_volume_percent() + self._step)

    def step_down(self) -> int:
        return self.set_volume_percent(self.get_volume_percent() - self._step)

    def set_muted(self, muted: bool) -> None:
        if not self._mixer_name:
            return
        self._amixer("sset", self._mixer_name, "mute" if muted else "unmute")
