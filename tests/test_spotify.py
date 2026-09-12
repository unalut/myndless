import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.spotify import SpotifyBackend, write_onevent_script  # noqa: E402


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def kill(self):
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


def test_start_spawns_librespot_with_device_name():
    spawner = FakeSpawner()
    backend = SpotifyBackend(device_name="myndless-test", spawner=spawner)
    backend.start()
    assert backend.is_running()
    args = spawner.spawned[0].args
    assert "--name" in args and "myndless-test" in args


def test_start_is_idempotent_while_running():
    spawner = FakeSpawner()
    backend = SpotifyBackend(spawner=spawner)
    backend.start()
    backend.start()
    assert len(spawner.spawned) == 1


def test_stop_clears_running_state():
    spawner = FakeSpawner()
    backend = SpotifyBackend(spawner=spawner)
    backend.start()
    backend.stop()
    assert not backend.is_running()


def test_events_track_active_state():
    backend = SpotifyBackend(spawner=FakeSpawner())
    assert not backend.is_active
    backend.handle_event("playing")
    assert backend.is_active
    backend.handle_event("paused")
    assert not backend.is_active


def test_onevent_flag_passed_when_configured():
    spawner = FakeSpawner()
    backend = SpotifyBackend(spawner=spawner, onevent_script_path="/tmp/onevent.sh")
    backend.start()
    args = spawner.spawned[0].args
    assert "--onevent" in args
    assert "/tmp/onevent.sh" in args


def test_write_onevent_script_embeds_port_and_is_executable(tmp_path=None):
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "onevent.sh")
        write_onevent_script(path, event_port=5901)
        content = open(path).read()
        assert "5901" in content
        assert os.access(path, os.X_OK)


def test_playback_control_is_a_documented_no_op():
    backend = SpotifyBackend(spawner=FakeSpawner())
    assert backend.play_pause() is False
    assert backend.next_track() is False
    assert backend.previous_track() is False


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
