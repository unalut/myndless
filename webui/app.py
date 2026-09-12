"""Flask control UI + API for myndless.

Runs in the same process as the Orchestrator (see daemon/main.py) so it can
call straight into it - no IPC needed for a single-Pi deployment. Also hosts
the endpoint librespot's --onevent hook posts to (see daemon/spotify.py).
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from daemon.orchestrator import Orchestrator


def create_app(orch: Orchestrator) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(
            {
                "active_source": orch.active_source,
                "volume_percent": orch.audio.get_volume_percent(),
                "muted": orch.audio.is_muted(),
                "current_station": (
                    {"id": orch.radio.current_station.id, "name": orch.radio.current_station.name}
                    if orch.radio.current_station
                    else None
                ),
                "spotify_running": orch.spotify.is_running(),
                "spotify_active": orch.spotify.is_active,
                "device_state": orch.device_state,
            }
        )

    @app.get("/api/radio/stations")
    def list_stations():
        return jsonify([{"id": s.id, "name": s.name} for s in orch.radio.list_stations()])

    @app.get("/api/radio/search")
    def search_stations():
        query = request.args.get("q", "")
        return jsonify(orch.search_radio_stations(query))

    @app.post("/api/radio/stations")
    def add_station():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        if not name or not url:
            return jsonify({"error": "expected JSON body {'name': ..., 'url': ...}"}), 400
        station = orch.add_radio_station(name, url, station_id=data.get("id") or None)
        return jsonify({"id": station.id, "name": station.name}), 201

    @app.post("/api/radio/play/<station_id>")
    def play_station(station_id: str):
        try:
            station = orch.start_radio(station_id)
        except KeyError:
            return jsonify({"error": f"unknown station {station_id!r}"}), 404
        return jsonify({"id": station.id, "name": station.name})

    @app.post("/api/radio/stop")
    def stop_radio():
        orch.stop_radio()
        return jsonify({"ok": True})

    @app.post("/api/spotify/activate")
    def activate_spotify():
        orch.switch_to_spotify()
        return jsonify({"ok": True})

    @app.post("/api/volume")
    def set_volume():
        data = request.get_json(force=True, silent=True) or {}
        try:
            percent = int(data["percent"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "expected JSON body {'percent': 0-100}"}), 400
        actual = orch.set_volume(percent)
        return jsonify({"percent": actual})

    @app.post("/internal/spotify-event")
    def spotify_event():
        # Posted by the shell script librespot invokes via --onevent
        # (see daemon/spotify.write_onevent_script) - trusted, loopback-only.
        event = request.form.get("event", "")
        track_id = request.form.get("track_id", "")
        volume = request.form.get("volume", "")
        orch.handle_spotify_event(event, track_id, volume)
        return "", 204

    return app
