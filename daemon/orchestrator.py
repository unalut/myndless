"""Wires ActionslinkClient (the MYND MCU link) to the audio backends.

This is where the "peer" (BT-chip-shaped) side of the Actionslink protocol
gets a real implementation: every FromMcuRequest field gets a handler that
returns *something* so the MCU's transaction never hangs, and every
FromMcuEvent we care about updates local state.

Design choices worth calling out:

  - Only one of {radio, spotify} is ever meant to be actually making sound.
    Starting radio force-stops librespot (so a phone can't keep streaming
    into the same ALSA device); Spotify becoming active (via its onevent
    hook) stops radio. See `_set_active_source`.
  - `set_audio_source` (A2DP1/A2DP2/USB/ANALOG) is vestigial here - it made
    sense when the peer was a real multi-source BT chip. We ack it but it
    doesn't change anything; our own source switching goes through
    start_radio()/switch_to_spotify() instead, driven by the web UI.
  - Bluetooth-management requests (pairing, paired device list, RSSI, ...)
    are stubbed out with harmless static/empty responses: we don't
    implement a real Bluetooth stack, but the MCU must never be left
    waiting for a reply.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from myndlink.client import ActionslinkClient
import app_pb2
import audio_pb2
import bluetooth_pb2
import common_pb2
import error_pb2
import system_pb2
import usb_pb2

from .audio import AudioBackend
from .config import Config
from . import radio_browser
from .radio import RadioPlayer, Station, save_stations
from .spotify import SpotifyBackend

log = logging.getLogger("myndless.orchestrator")


def _result(code=error_pb2.Code.Success) -> common_pb2.Result:
    return common_pb2.Result(status=error_pb2.Error(code=code))


class Orchestrator:
    def __init__(
        self,
        client: ActionslinkClient,
        audio: AudioBackend,
        radio: RadioPlayer,
        spotify: SpotifyBackend,
        config: Config,
    ):
        self.client = client
        self.audio = audio
        self.radio = radio
        self.spotify = spotify
        self.config = config

        self.active_source: Optional[str] = None  # "radio" | "spotify" | None
        self.device_state: dict = {
            "aux_connected": False,
            "usb_connected": False,
            "battery_level": None,
            "charger_status": None,
            "color": None,
            "battery_friendly_charging": None,
            "eco_mode": None,
        }

        self._register_handlers()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self.client.start()
        self.client.notify_system_ready()
        self.spotify.start()
        self.client.notify_power_state(system_pb2.PowerState.ON)
        log.info("orchestrator ready")

    def stop(self) -> None:
        self.radio.stop()
        self.spotify.stop()
        self.client.stop()

    # ------------------------------------------------------------------ #
    # Source control (called from the web UI / API, not the MCU)
    # ------------------------------------------------------------------ #

    def start_radio(self, station_id: str):
        self.spotify.stop()  # force-disconnect so it can't keep streaming into the same output
        station = self.radio.play(station_id)
        self._set_active_source("radio")
        return station

    def stop_radio(self) -> None:
        self.radio.stop()
        if self.active_source == "radio":
            self._set_active_source(None)

    def search_radio_stations(self, query: str) -> list[dict]:
        return radio_browser.search_stations(query)

    def add_radio_station(self, name: str, url: str, station_id: Optional[str] = None) -> Station:
        """Add a station (from a Radio Browser search result, or typed in by
        hand) and persist it to disk so it survives a restart."""
        station = Station(id=station_id or self._unique_station_id(name), name=name, url=url)
        self.radio.add_station(station)
        try:
            save_stations(self.config.radio_stations_file, self.radio.list_stations())
        except OSError:
            log.exception("failed to persist stations to %s - station added for this run only", self.config.radio_stations_file)
        return station

    def _unique_station_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "station"
        existing = {s.id for s in self.radio.list_stations()}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def switch_to_spotify(self) -> None:
        self.radio.stop()
        self.spotify.start()
        # Actual activity (and notify_stream_state) is driven by librespot's
        # own events once a phone actually picks this device - see
        # handle_spotify_event().

    def set_volume(self, percent: int) -> int:
        actual = self.audio.set_volume_percent(percent)
        self.client.notify_volume_percent(actual, is_muted=self.audio.is_muted())
        return actual

    def _set_active_source(self, source: Optional[str]) -> None:
        if source == self.active_source:
            return
        self.active_source = source
        log.info("active source -> %s", source)
        self.client.notify_stream_state(source is not None)

    # ------------------------------------------------------------------ #
    # Spotify onevent hook (see webui/app.py POST /internal/spotify-event)
    # ------------------------------------------------------------------ #

    def handle_spotify_event(self, event: str, track_id: str = "", volume: str = "") -> None:
        self.spotify.handle_event(event, track_id, volume)
        if self.spotify.is_active:
            self.radio.stop()
            self._set_active_source("spotify")
        elif self.active_source == "spotify":
            self._set_active_source(None)

    # ------------------------------------------------------------------ #
    # Actionslink handler registration
    # ------------------------------------------------------------------ #

    def _register_handlers(self) -> None:
        c = self.client

        c.on_request("set_power_state", self._on_set_power_state)
        c.on_request("set_audio_source", self._on_set_audio_source)
        c.on_request("set_volume", self._on_mcu_set_volume)
        c.on_request("send_avrcp_action", self._on_avrcp_action)
        c.on_request("send_usb_hid_action", self._on_usb_hid_action)
        c.on_request("get_a2dp_data", self._on_get_a2dp_data)
        c.on_request("get_firmware_version", self._on_get_firmware_version)
        c.on_request("get_this_device_name", self._on_get_this_device_name)
        c.on_request("play_sound_icon", self._on_play_sound_icon)
        c.on_request("stop_sound_icon", self._on_stop_sound_icon)
        c.on_request("soft_reset", self._on_soft_reset)
        c.on_request("enter_dfu_mode", self._on_enter_dfu_mode)
        c.on_request("send_app_packet", self._on_app_packet)

        # Bluetooth stack we don't implement: always answer, never hang the MCU.
        c.on_request("get_device_name", self._on_get_device_name)
        c.on_request("get_bt_mac_address", lambda v, seq: bluetooth_pb2.Device(address=0))
        c.on_request("get_ble_mac_address", lambda v, seq: bluetooth_pb2.Device(address=0))
        c.on_request("get_bt_rssi_value", lambda v, seq: bluetooth_pb2.RSSI(rssi=0))
        c.on_request(
            "get_paired_device_list",
            lambda v, seq: bluetooth_pb2.ResponsePairedDeviceList(list=bluetooth_pb2.PairedDeviceList(devices=[])),
        )
        c.on_request("disconnect_all_bt_devices", lambda v, seq: _result())
        c.on_request("enable_bt_reconnection", lambda v, seq: _result())
        c.on_request("clear_bt_paired_device_list", lambda v, seq: _result())
        c.on_request("set_bt_pairing_state", lambda v, seq: _result())
        c.on_request(
            "get_bt_pairing_state",
            lambda v, seq: bluetooth_pb2.ResponsePairingState(
                state=bluetooth_pb2.PairingState(state=bluetooth_pb2.PairingState.IDLE)
            ),
        )
        c.on_request(
            "get_bt_connection_state",
            lambda v, seq: bluetooth_pb2.ResponseConnectionState(
                state=bluetooth_pb2.ConnectionState(state=bluetooth_pb2.ConnectionState.DISCONNECTED)
            ),
        )
        c.on_request(
            "get_csb_state",
            lambda v, seq: bluetooth_pb2.ResponseCsbState(
                state=bluetooth_pb2.CsbState(state=bluetooth_pb2.CsbState.DISABLED)
            ),
        )
        c.on_request("exit_csb_mode", lambda v, seq: _result())

        c.on_event("notify_aux_connected", lambda v: self._store("aux_connected", v))
        c.on_event("notify_usb_connected", lambda v: self._store("usb_connected", v))
        c.on_event("notify_battery_level", lambda v: self._store("battery_level", v))
        c.on_event("notify_charger_status", lambda v: self._store("charger_status", v))
        c.on_event("notify_color", lambda v: self._store("color", v))
        c.on_event("notify_battery_friendly_charging", lambda v: self._store("battery_friendly_charging", v))
        c.on_event("notify_eco_mode", lambda v: self._store("eco_mode", v))

    def _store(self, key: str, value) -> None:
        self.device_state[key] = value
        log.debug("device state: %s = %s", key, value)

    # ------------------------------------------------------------------ #
    # Request handlers
    # ------------------------------------------------------------------ #

    def _on_set_power_state(self, value: "system_pb2.PowerState", seq: int):
        log.info("MCU set power state: %s", system_pb2.PowerState.SystemPowerMode.Name(value.mode))
        if value.mode in (system_pb2.PowerState.OFF, system_pb2.PowerState.STANDBY):
            self.radio.stop()
            self._set_active_source(None)
        return _result()

    def _on_set_audio_source(self, value: "audio_pb2.Source", seq: int):
        log.info("MCU set audio source: %s (no-op - see module docstring)", audio_pb2.AudioSourceType.Name(value.source))
        return _result()

    def _on_mcu_set_volume(self, value: "audio_pb2.VolumeControl", seq: int):
        if value.action == audio_pb2.VolumeControl.VOLUME_UP:
            self.audio.step_up()
        else:
            self.audio.step_down()
        self.client.notify_volume_percent(self.audio.get_volume_percent(), is_muted=self.audio.is_muted())
        return _result()

    def _on_avrcp_action(self, value: "bluetooth_pb2.AvrcpAction", seq: int):
        self._route_transport_action(value.action, bluetooth_pb2.AvrcpAction.Action)
        return _result()

    def _on_usb_hid_action(self, value: "usb_pb2.HidAction", seq: int):
        # HidAction has no TOGGLE_PLAY_PAUSE/PAUSE - just PLAY_PAUSE/NEXT/PREVIOUS.
        if value.action == usb_pb2.HidAction.PLAY_PAUSE:
            self._active_backend_play_pause()
        elif value.action == usb_pb2.HidAction.NEXT_TRACK:
            self._active_backend_next()
        elif value.action == usb_pb2.HidAction.PREVIOUS_TRACK:
            self._active_backend_previous()
        return _result()

    def _route_transport_action(self, action, action_enum) -> None:
        if action in (action_enum.PLAY, action_enum.PAUSE, action_enum.TOGGLE_PLAY_PAUSE):
            self._active_backend_play_pause()
        elif action == action_enum.NEXT_TRACK:
            self._active_backend_next()
        elif action == action_enum.PREVIOUS_TRACK:
            self._active_backend_previous()

    def _active_backend_play_pause(self) -> None:
        if self.active_source == "radio":
            self.radio.play_pause()
        elif self.active_source == "spotify":
            self.spotify.play_pause()

    def _active_backend_next(self) -> None:
        if self.active_source == "spotify":
            self.spotify.next_track()

    def _active_backend_previous(self) -> None:
        if self.active_source == "spotify":
            self.spotify.previous_track()

    def _on_get_a2dp_data(self, value, seq: int):
        if self.active_source is None:
            return bluetooth_pb2.ResponseA2dpData(error=error_pb2.Error(code=error_pb2.Code.ResourceUnavailable))
        return bluetooth_pb2.ResponseA2dpData(
            data=bluetooth_pb2.A2dpData(
                channel_mode=bluetooth_pb2.Stereo,
                codec=bluetooth_pb2.AAC,
                sample_rate=44100,
            )
        )

    def _on_get_firmware_version(self, value, seq: int):
        major, minor, patch = self.config.firmware_version
        return system_pb2.FirmwareVersion(major=major, minor=minor, patch=patch, build="myndless")

    def _on_get_this_device_name(self, value, seq: int):
        return bluetooth_pb2.ResponseDeviceName(name=self.config.device_name)

    def _on_get_device_name(self, value, seq: int):
        # We don't track paired peer devices - there aren't any.
        return bluetooth_pb2.ResponseDeviceName(error=error_pb2.Error(code=error_pb2.Code.ResourceUnavailable))

    def _on_play_sound_icon(self, value: "audio_pb2.PlaySoundIcon", seq: int):
        name = audio_pb2.SoundIcon.Name(value.sound_icon).lower()
        path = Path(self.config.sound_icons_dir) / f"{name}.wav"
        if path.exists():
            subprocess.Popen(["aplay", "-q", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            log.debug("sound icon %s requested but %s does not exist", name, path)
        return _result()

    def _on_stop_sound_icon(self, value, seq: int):
        # aplay instances started above are fire-and-forget and short; nothing to stop.
        return _result()

    def _on_soft_reset(self, value, seq: int):
        log.warning("MCU requested a soft reset - not implemented, acking only")
        return _result()

    def _on_enter_dfu_mode(self, value, seq: int):
        return _result(error_pb2.Code.OperationNotSupported)

    def _on_app_packet(self, value: "app_pb2.Packet", seq: int):
        log.debug("received app packet (%d bytes) - no handler registered", len(value.payload))
        return _result()
