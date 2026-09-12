"""Integration test: real ActionslinkClient + real Orchestrator wiring,
against a simulated MCU over a pty, with fake audio/radio/spotify backends
(subprocess-based backends are already covered on their own in
test_audio.py / test_radio.py / test_spotify.py)."""

import os
import pty
import select
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "myndlink" / "pb"))

from myndlink.client import ActionslinkClient  # noqa: E402
from myndlink.hdlc import FrameParser, PacketType, encode_frame  # noqa: E402
from daemon.config import Config  # noqa: E402
from daemon.orchestrator import Orchestrator  # noqa: E402
from daemon.radio import Station  # noqa: E402
import message_pb2 as pb  # noqa: E402
import bluetooth_pb2  # noqa: E402
import system_pb2  # noqa: E402


class FakeAudio:
    def __init__(self):
        self.percent = 50
        self.muted = False

    def get_volume_percent(self):
        return self.percent

    def set_volume_percent(self, percent):
        self.percent = max(0, min(100, percent))
        return self.percent

    def step_up(self):
        return self.set_volume_percent(self.percent + 5)

    def step_down(self):
        return self.set_volume_percent(self.percent - 5)

    def is_muted(self):
        return self.muted

    def set_muted(self, muted):
        self.muted = muted


class FakeRadio:
    def __init__(self):
        self.playing_station = None
        self.pause_toggled = 0
        self._stations = {
            "somafm-groovesalad": Station(id="somafm-groovesalad", name="Groove Salad", url="http://example.com/gs"),
        }

    def list_stations(self):
        return list(self._stations.values())

    def play(self, station_id):
        station = self._stations[station_id]
        self.playing_station = station_id
        return station

    def stop(self):
        self.playing_station = None

    def is_playing(self):
        return self.playing_station is not None

    @property
    def current_station(self):
        return self._stations.get(self.playing_station) if self.is_playing() else None

    def play_pause(self):
        self.pause_toggled += 1


class FakeSpotify:
    def __init__(self):
        self.started = False
        self.active = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False
        self.active = False

    def is_running(self):
        return self.started

    @property
    def is_active(self):
        return self.active

    def handle_event(self, event, track_id="", volume=""):
        self.active = event in ("playing", "active", "started")

    def play_pause(self):
        pass

    def next_track(self):
        pass

    def previous_track(self):
        pass


class MockMcu:
    """Drives one end of the pty in a background thread: continuously reads
    frames, auto-acks protobuf ones (echoing their transaction id), and
    records decoded ToMcu messages for the test to assert on."""

    def __init__(self, fd):
        self.fd = fd
        self.parser = FrameParser()
        self._next_tx_id = 200
        self.received: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1)
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _tx_id(self):
        tx_id = self._next_tx_id
        self._next_tx_id = (self._next_tx_id + 1) & 0xFF
        return tx_id

    def send(self, packet_type, value, payload=b"", tx_id=None):
        os.write(self.fd, encode_frame(packet_type, value, tx_id if tx_id is not None else self._tx_id(), payload))

    def send_from_mcu_request(self, field_name, submessage=None, scalar=None, seq=1):
        msg = pb.FromMcu()
        msg.request.seq = seq
        target = getattr(msg.request, field_name)
        if submessage is not None:
            target.CopyFrom(submessage)
        elif scalar is not None:
            setattr(msg.request, field_name, scalar)
        else:
            target.SetInParent()
        self.send(PacketType.PROTOBUF, 0, msg.SerializeToString())

    def _run(self) -> None:
        # select() + a blocking read reacts to new bytes immediately, unlike
        # a poll-with-sleep loop - matters here because the protocol's ACK
        # timeout (300ms, sized for real UART hardware) leaves little room
        # for a busy-poll's extra latency under thread/GIL contention.
        os.set_blocking(self.fd, False)
        while not self._stop.is_set():
            readable, _, _ = select.select([self.fd], [], [], 0.05)
            if not readable:
                continue
            try:
                chunk = os.read(self.fd, 256)
            except (BlockingIOError, OSError):
                continue
            if not chunk:
                continue
            for frame in self.parser.feed(chunk):
                if isinstance(frame, Exception):
                    continue
                if frame.packet_type == PacketType.ACK:
                    continue
                decoded = pb.ToMcu()
                decoded.ParseFromString(frame.payload)
                self.send(PacketType.ACK, 0, tx_id=frame.transaction_id)
                with self._lock:
                    self.received.append(decoded)

    def clear(self) -> None:
        with self._lock:
            self.received.clear()

    def pump_until(self, predicate, timeout=2.0):
        """Poll (already-being-collected) received messages until predicate(self) is true."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if predicate(self):
                    return True
            time.sleep(0.002)
        with self._lock:
            return predicate(self)


def make_orchestrator():
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    # ActionslinkClient opens its own fd on slave_path via pyserial; don't
    # leave this original one dangling open and unread alongside it.
    os.close(slave_fd)
    client = ActionslinkClient(port=slave_path)
    audio, radio, spotify = FakeAudio(), FakeRadio(), FakeSpotify()
    orch = Orchestrator(client, audio, radio, spotify, Config())
    mcu = MockMcu(master_fd)
    return orch, mcu, audio, radio, spotify


def test_startup_sends_system_ready_and_starts_spotify():
    orch, mcu, audio, radio, spotify = make_orchestrator()
    orch.start()
    try:
        got_it = mcu.pump_until(
            lambda m: any(d.WhichOneof("Payload") == "event" and d.event.WhichOneof("Event") == "notify_system_ready" for d in m.received)
        )
        assert got_it, "orchestrator never sent notify_system_ready"
        assert spotify.started, "spotify backend should be started on orchestrator startup"
    finally:
        orch.stop()
        mcu.close()


def test_mcu_get_this_device_name_is_answered():
    orch, mcu, *_ = make_orchestrator()
    orch.start()
    try:
        mcu.send_from_mcu_request("get_this_device_name")

        response_holder = {}

        def got_response(m):
            for d in m.received:
                if d.WhichOneof("Payload") == "response" and d.response.WhichOneof("Response") == "get_this_device_name":
                    response_holder["name"] = d.response.get_this_device_name.name
                    return True
            return False

        assert mcu.pump_until(got_response), "no response to get_this_device_name"
        assert response_holder["name"] == "myndless"
    finally:
        orch.stop()
        mcu.close()


def test_volume_up_button_adjusts_audio_and_notifies_mcu():
    orch, mcu, audio, radio, spotify = make_orchestrator()
    orch.start()
    try:
        mcu.pump_until(lambda m: any(d.WhichOneof("Payload") == "event" for d in m.received))
        mcu.clear()

        import audio_pb2

        vc = audio_pb2.VolumeControl(action=audio_pb2.VolumeControl.VOLUME_UP)
        mcu.send_from_mcu_request("set_volume", submessage=vc)

        def got_volume_notify(m):
            return any(
                d.WhichOneof("Payload") == "event" and d.event.WhichOneof("Event") == "notify_volume"
                for d in m.received
            )

        assert mcu.pump_until(got_volume_notify), "orchestrator never notified MCU of new volume"
        assert audio.percent == 55

        # The handler also owes the MCU a response to the original request
        # (sent right after the notify_volume event above); wait for it
        # before tearing down, or its still-in-flight retry races the
        # teardown and logs a harmless but noisy spurious timeout.
        def got_response(m):
            return any(d.WhichOneof("Payload") == "response" for d in m.received)

        assert mcu.pump_until(got_response), "orchestrator never responded to set_volume"
    finally:
        orch.stop()
        mcu.close()


def test_avrcp_play_pause_routes_to_active_radio_backend():
    orch, mcu, audio, radio, spotify = make_orchestrator()
    orch.start()
    try:
        mcu.pump_until(lambda m: any(d.WhichOneof("Payload") == "event" for d in m.received))
        orch.start_radio("somafm-groovesalad")
        assert orch.active_source == "radio"
        assert radio.playing_station == "somafm-groovesalad"
        assert spotify.started is False, "starting radio should force-stop spotify"

        mcu.clear()
        bluetooth_action = bluetooth_pb2.AvrcpAction(action=bluetooth_pb2.AvrcpAction.TOGGLE_PLAY_PAUSE)
        mcu.send_from_mcu_request("send_avrcp_action", submessage=bluetooth_action)

        def got_response(m):
            return any(d.WhichOneof("Payload") == "response" for d in m.received)

        assert mcu.pump_until(got_response)
        assert radio.pause_toggled == 1
    finally:
        orch.stop()
        mcu.close()


def test_set_power_state_standby_stops_radio_and_notifies():
    orch, mcu, audio, radio, spotify = make_orchestrator()
    orch.start()
    try:
        mcu.pump_until(lambda m: any(d.WhichOneof("Payload") == "event" for d in m.received))
        orch.start_radio("somafm-groovesalad")
        mcu.clear()

        power_state = system_pb2.PowerState(mode=system_pb2.PowerState.STANDBY)
        mcu.send_from_mcu_request("set_power_state", submessage=power_state)

        def got_stream_state_false(m):
            for d in m.received:
                if d.WhichOneof("Payload") == "event" and d.event.WhichOneof("Event") == "notify_stream_state":
                    return d.event.notify_stream_state is False
            return False

        assert mcu.pump_until(got_stream_state_false)
        assert radio.playing_station is None
        assert orch.active_source is None

        # Same as above: let the still-owed response to set_power_state
        # land before tearing down, so it doesn't race the teardown.
        def got_response(m):
            return any(d.WhichOneof("Payload") == "response" for d in m.received)

        assert mcu.pump_until(got_response), "orchestrator never responded to set_power_state"
    finally:
        orch.stop()
        mcu.close()


def test_spotify_event_stops_radio_and_becomes_active_source():
    orch, mcu, audio, radio, spotify = make_orchestrator()
    orch.start()
    try:
        mcu.pump_until(lambda m: any(d.WhichOneof("Payload") == "event" for d in m.received))
        orch.start_radio("somafm-groovesalad")
        assert orch.active_source == "radio"

        orch.handle_spotify_event("playing")
        assert orch.active_source == "spotify"
        assert radio.playing_station is None
    finally:
        orch.stop()
        mcu.close()


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
