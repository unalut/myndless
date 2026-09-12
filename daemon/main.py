"""myndless entrypoint: wires everything together and runs it.

    python -m daemon.main

Reads configuration from environment variables (see daemon/config.py) -
the systemd unit in systemd/myndless.service sets these for the real
deployment; defaults are reasonable for running by hand too.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from myndlink.client import ActionslinkClient

from .audio import AudioBackend
from .config import CONFIG
from .orchestrator import Orchestrator
from .radio import RadioPlayer, load_stations
from .spotify import SpotifyBackend, write_onevent_script

log = logging.getLogger("myndless.main")


def build_orchestrator() -> Orchestrator:
    client = ActionslinkClient(port=CONFIG.serial_port, baudrate=CONFIG.serial_baudrate)
    audio = AudioBackend(card=CONFIG.alsa_card, mixer_name=CONFIG.alsa_mixer, step_percent=CONFIG.volume_step_percent)
    radio = RadioPlayer(load_stations(CONFIG.radio_stations_file), socket_path=CONFIG.mpv_socket_path)

    onevent_script = "/tmp/myndless-librespot-onevent.sh"
    write_onevent_script(onevent_script, event_port=CONFIG.web_port)
    spotify = SpotifyBackend(
        device_name=CONFIG.librespot_device_name,
        librespot_bin=CONFIG.librespot_bin,
        alsa_device=CONFIG.alsa_pcm_device,
        onevent_script_path=onevent_script,
    )

    return Orchestrator(client, audio, radio, spotify, CONFIG)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    orch = build_orchestrator()
    orch.start()

    from webui.app import create_app

    app = create_app(orch)

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    server_thread = threading.Thread(
        target=lambda: app.run(host=CONFIG.web_host, port=CONFIG.web_port, use_reloader=False),
        name="webui",
        daemon=True,
    )
    server_thread.start()
    log.info("web UI listening on http://%s:%d", CONFIG.web_host, CONFIG.web_port)

    stop_event.wait()
    orch.stop()


if __name__ == "__main__":
    sys.exit(main())
