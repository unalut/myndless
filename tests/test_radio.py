import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.radio import RadioPlayer, Station, load_stations, save_stations  # noqa: E402


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self._returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


class FakeSpawner:
    def __init__(self):
        self.spawned = []

    def __call__(self, args, **kwargs):
        proc = FakeProcess(args, **kwargs)
        self.spawned.append(proc)
        return proc


STATIONS = [
    Station(id="a", name="Station A", url="http://example.com/a"),
    Station(id="b", name="Station B", url="http://example.com/b"),
]


def test_play_spawns_mpv_with_station_url():
    spawner = FakeSpawner()
    player = RadioPlayer(STATIONS, socket_path="/tmp/test-mpv-a.sock", spawner=spawner)

    station = player.play("a")
    assert station.name == "Station A"
    assert player.is_playing()
    assert player.current_station.id == "a"
    assert "http://example.com/a" in spawner.spawned[0].args


def test_play_unknown_station_raises():
    player = RadioPlayer(STATIONS, spawner=FakeSpawner())
    try:
        player.play("nope")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_switching_stations_stops_previous_process():
    spawner = FakeSpawner()
    player = RadioPlayer(STATIONS, socket_path="/tmp/test-mpv-b.sock", spawner=spawner)

    player.play("a")
    first_proc = spawner.spawned[0]
    assert not first_proc.terminated

    player.play("b")
    assert first_proc.terminated
    assert player.current_station.id == "b"
    assert len(spawner.spawned) == 2


def test_stop_terminates_process_and_clears_state():
    spawner = FakeSpawner()
    player = RadioPlayer(STATIONS, socket_path="/tmp/test-mpv-c.sock", spawner=spawner)

    player.play("a")
    player.stop()
    assert not player.is_playing()
    assert player.current_station is None
    assert spawner.spawned[0].terminated


def test_load_stations_missing_file_returns_empty():
    assert load_stations("/nonexistent/path.json") == []


def test_load_stations_from_real_file():
    stations = load_stations(str(Path(__file__).parent.parent / "daemon" / "stations.json"))
    assert len(stations) >= 1
    assert all(isinstance(s, Station) for s in stations)


def test_add_station_makes_it_immediately_playable():
    player = RadioPlayer(STATIONS, spawner=FakeSpawner())
    player.add_station(Station(id="new-one", name="New One", url="http://example.com/new"))
    ids = [s.id for s in player.list_stations()]
    assert "new-one" in ids
    player.play("new-one")  # doesn't raise KeyError
    assert player.current_station.id == "new-one"


def test_save_and_reload_stations_round_trips(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/stations.json"
        save_stations(path, STATIONS)
        reloaded = load_stations(path)
        assert [s.id for s in reloaded] == [s.id for s in STATIONS]
        assert [s.url for s in reloaded] == [s.url for s in STATIONS]


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
