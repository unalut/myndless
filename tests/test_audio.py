import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.audio import AudioBackend  # noqa: E402


class FakeRunner:
    """Records amixer invocations and returns canned output."""

    def __init__(self):
        self.calls = []
        self.volume = 42
        self.muted = False

    def __call__(self, args, capture_output=True, text=True, timeout=2):
        self.calls.append(args)
        if args[1:3] == ["-c", "default"] and args[3] == "scontrols":
            stdout = "Simple mixer control 'PCM',0\nSimple mixer control 'Capture',0\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if "sget" in args:
            state = "off" if self.muted else "on"
            stdout = f"  Front Left: Playback {self.volume * 3}  [{self.volume}%] [{state}]"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if "sset" in args:
            value = args[-1]
            if value == "mute":
                self.muted = True
            elif value == "unmute":
                self.muted = False
            elif value.endswith("%"):
                self.volume = int(value[:-1])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_autodetect_prefers_pcm():
    runner = FakeRunner()
    backend = AudioBackend(runner=runner)
    assert backend._mixer_name == "PCM"


def test_get_and_set_volume():
    runner = FakeRunner()
    backend = AudioBackend(runner=runner)
    assert backend.get_volume_percent() == 42

    result = backend.set_volume_percent(70)
    assert result == 70
    assert backend.get_volume_percent() == 70


def test_set_volume_clamps_range():
    runner = FakeRunner()
    backend = AudioBackend(runner=runner)
    assert backend.set_volume_percent(150) == 100
    assert backend.set_volume_percent(-10) == 0


def test_step_up_and_down():
    runner = FakeRunner()
    backend = AudioBackend(runner=runner, step_percent=5)
    runner.volume = 50
    assert backend.step_up() == 55
    assert backend.step_down() == 50
    assert backend.step_down() == 45


def test_mute_unmute():
    runner = FakeRunner()
    backend = AudioBackend(runner=runner)
    assert backend.is_muted() is False
    backend.set_muted(True)
    assert backend.is_muted() is True
    backend.set_muted(False)
    assert backend.is_muted() is False


def test_no_mixer_found_disables_volume_control():
    class EmptyRunner(FakeRunner):
        def __call__(self, args, capture_output=True, text=True, timeout=2):
            if "scontrols" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return super().__call__(args, capture_output, text, timeout)

    backend = AudioBackend(runner=EmptyRunner())
    assert backend._mixer_name == ""
    assert backend.get_volume_percent() == 0
    assert backend.set_volume_percent(80) == 0


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
