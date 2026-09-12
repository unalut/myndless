import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from daemon.config import Config  # noqa: E402
from daemon.radio import Station  # noqa: E402
from webui.app import create_app  # noqa: E402
from test_orchestrator import FakeAudio, FakeRadio, FakeSpotify  # noqa: E402


class FakeClient:
    """Stands in for ActionslinkClient - the web UI never talks to it directly."""

    def start(self): pass
    def stop(self): pass
    def on_request(self, *a, **k): pass
    def on_event(self, *a, **k): pass
    def notify_system_ready(self): pass
    def notify_power_state(self, *a, **k): pass
    def notify_stream_state(self, *a, **k): pass
    def notify_volume_percent(self, *a, **k): pass


def make_client():
    import tempfile

    from daemon.orchestrator import Orchestrator

    audio, radio_backend, spotify = FakeAudio(), FakeRadio(), FakeSpotify()
    radio_backend._stations = {
        "a": Station(id="a", name="Station A", url="http://example.com/a"),
    }
    # A scratch stations file, not the real daemon/stations.json - add_radio_station
    # persists to disk, and tests must not clobber the repo's real station list.
    stations_path = f"{tempfile.mkdtemp()}/stations.json"
    config = Config(radio_stations_file=stations_path)
    orch = Orchestrator(FakeClient(), audio, radio_backend, spotify, config)
    app = create_app(orch)
    app.testing = True
    return app.test_client(), orch


def test_status_endpoint_reports_idle_by_default():
    client, orch = make_client()
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["active_source"] is None
    assert body["current_station"] is None


def test_list_stations():
    client, orch = make_client()
    resp = client.get("/api/radio/stations")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.get_json()]
    assert ids == ["a"]


def test_play_station_updates_status():
    client, orch = make_client()
    resp = client.post("/api/radio/play/a")
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Station A"

    status = client.get("/api/status").get_json()
    assert status["active_source"] == "radio"
    assert status["current_station"]["id"] == "a"


def test_play_unknown_station_returns_404():
    client, orch = make_client()
    resp = client.post("/api/radio/play/does-not-exist")
    assert resp.status_code == 404


def test_stop_radio():
    client, orch = make_client()
    client.post("/api/radio/play/a")
    resp = client.post("/api/radio/stop")
    assert resp.status_code == 200
    assert client.get("/api/status").get_json()["active_source"] is None


def test_set_volume():
    client, orch = make_client()
    resp = client.post("/api/volume", json={"percent": 42})
    assert resp.status_code == 200
    assert resp.get_json()["percent"] == 42
    assert client.get("/api/status").get_json()["volume_percent"] == 42


def test_set_volume_rejects_bad_body():
    client, orch = make_client()
    resp = client.post("/api/volume", json={"nope": 1})
    assert resp.status_code == 400


def test_spotify_activate():
    client, orch = make_client()
    resp = client.post("/api/spotify/activate")
    assert resp.status_code == 200
    assert orch.spotify.started is True


def test_spotify_event_hook_updates_orchestrator():
    client, orch = make_client()
    client.post("/api/radio/play/a")
    assert orch.active_source == "radio"

    resp = client.post("/internal/spotify-event", data={"event": "playing", "track_id": "abc", "volume": "50"})
    assert resp.status_code == 204
    assert orch.active_source == "spotify"


def test_index_page_renders():
    client, orch = make_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"myndless" in resp.data


def test_search_stations_endpoint():
    import daemon.orchestrator as orchestrator_module

    original = orchestrator_module.radio_browser.search_stations
    orchestrator_module.radio_browser.search_stations = lambda q: [{"name": f"Result for {q}", "url": "http://x", "stationuuid": "u1"}]
    try:
        client, orch = make_client()
        resp = client.get("/api/radio/search?q=jazz")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body[0]["name"] == "Result for jazz"
    finally:
        orchestrator_module.radio_browser.search_stations = original


def test_add_station_endpoint_then_playable():
    client, orch = make_client()
    resp = client.post("/api/radio/stations", json={"name": "New Station", "url": "http://example.com/new"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["id"] == "new-station"

    stations = client.get("/api/radio/stations").get_json()
    assert any(s["id"] == "new-station" for s in stations)

    play_resp = client.post("/api/radio/play/new-station")
    assert play_resp.status_code == 200


def test_add_station_endpoint_rejects_missing_fields():
    client, orch = make_client()
    resp = client.post("/api/radio/stations", json={"name": "No URL"})
    assert resp.status_code == 400


def test_remove_station_endpoint():
    client, orch = make_client()
    resp = client.delete("/api/radio/stations/a")
    assert resp.status_code == 204

    stations = client.get("/api/radio/stations").get_json()
    assert not any(s["id"] == "a" for s in stations)


def test_remove_station_endpoint_unknown_id_returns_404():
    client, orch = make_client()
    resp = client.delete("/api/radio/stations/does-not-exist")
    assert resp.status_code == 404


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
