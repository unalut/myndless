#!/usr/bin/env python3
"""Standalone Actionslink UART diagnostic - no orchestrator, no audio, no
handlers. Just opens the serial port, logs every raw byte and every frame it
can parse, and (optionally) tries the boot handshake.

Run this FIRST on real hardware, before trusting daemon/main.py with actual
audio routing - it answers two questions:
  1. Are we even seeing bytes on this port? (wiring/UART-enable sanity check)
  2. Do those bytes parse as valid Actionslink frames? (protocol sanity check)

Usage:
    python3 scripts/uart_probe.py [port]
    python3 scripts/uart_probe.py [port] --handshake [--listen=SECONDS]

    port           defaults to /dev/serial0
    --handshake    send notify_system_ready + notify_power_state(ON) and try
                   a get_mcu_firmware_version request, like the real daemon
                   does on startup. The MCU stays silent until it sees this -
                   plain sniffing (no --handshake) will see nothing even on
                   a perfectly working link.
    --listen=N     after a successful handshake, stay up for N seconds
                   logging every event/request the MCU sends (e.g. from
                   pressing buttons on the speaker) instead of exiting
                   immediately.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import serial  # noqa: E402

from myndlink.hdlc import FrameParser, PacketType  # noqa: E402


def sniff(port: str, seconds: float = 15.0) -> None:
    print(f"Opening {port} at 115200 8N1, watching for {seconds:.0f}s ...")
    print("(power-cycle the speaker or press a button on it now)\n")

    ser = serial.Serial(port, baudrate=115200, timeout=0.1)
    parser = FrameParser()
    deadline = time.time() + seconds
    saw_anything = False
    saw_valid_frame = False

    while time.time() < deadline:
        chunk = ser.read(max(1, ser.in_waiting))
        if not chunk:
            continue
        saw_anything = True
        print(f"RX {len(chunk):3d} bytes: {chunk.hex()}")
        for result in parser.feed(chunk):
            if isinstance(result, Exception):
                print(f"    -> frame error: {result}")
                continue
            saw_valid_frame = True
            kind = "ACK/NACK" if result.packet_type == PacketType.ACK else "PROTOBUF"
            print(f"    -> valid frame: type={kind} tx_id={result.transaction_id} "
                  f"value={result.value} payload={result.payload.hex()}")
            if result.packet_type == PacketType.PROTOBUF:
                _describe_protobuf(result.payload)

    ser.close()
    print()
    if not saw_anything:
        print("Nothing received at all. Check:")
        print("  - UART enabled + serial console disabled (raspi-config, see README)")
        print("  - MYNDberry PCB seated correctly / not on the wrong header pins")
        print("  - The speaker is actually powered on")
    elif not saw_valid_frame:
        print("Received bytes, but nothing parsed as a valid Actionslink frame.")
        print("Check baud rate (should be 115200) and that TX/RX aren't swapped.")
    else:
        print("Looks healthy: raw bytes are arriving and parsing as valid frames.")


def _describe_protobuf(payload: bytes) -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent / "myndlink" / "pb"))
    import message_pb2 as pb

    msg = pb.FromMcu()
    try:
        msg.ParseFromString(payload)
    except Exception as exc:
        print(f"       (protobuf decode failed: {exc})")
        return
    which = msg.WhichOneof("Payload")
    if which == "request":
        print(f"       FromMcuRequest.{msg.request.WhichOneof('Request')} (seq={msg.request.seq})")
    elif which == "event":
        print(f"       FromMcuEvent.{msg.event.WhichOneof('Event')}")
    elif which == "response":
        print(f"       FromMcuResponse.{msg.response.WhichOneof('Response')} (seq={msg.response.seq})")


def handshake(port: str, listen_seconds: float = 0.0) -> None:
    from myndlink.client import ActionslinkClient

    print(f"Opening {port}, running the boot handshake ...\n")
    client = ActionslinkClient(port)
    client.start()

    try:
        client.notify_system_ready()
        print("notify_system_ready: acked")

        import system_pb2

        client.notify_power_state(system_pb2.PowerState.ON)
        print("notify_power_state(ON): acked")

        import audio_pb2

        client.notify_audio_source(audio_pb2.AudioSourceType.A2DP1)
        print("notify_audio_source(A2DP1): acked")

        version = client.get_mcu_firmware_version(timeout=1.0)
        print(f"MCU firmware version: {version.major}.{version.minor}.{version.patch} ({version.build})")

        if listen_seconds > 0:
            _listen(client, listen_seconds)
    except Exception as exc:
        print(f"handshake step failed: {exc}")
    finally:
        client.stop()


def _listen(client, seconds: float) -> None:
    """Register a catch-all logger for every request/event field and stay
    up for a while - use this once the handshake succeeds, to see live
    traffic from physical button/knob presses on the speaker."""
    import common_pb2
    import error_pb2

    def log_event(name):
        def handler(value):
            print(f"EVENT  {name}: {value}")
        return handler

    def log_request(name):
        def handler(value, seq):
            print(f"REQUEST {name} (seq={seq}): {value}")
            result = common_pb2.Result()
            result.status.code = error_pb2.Code.Success
            return result
        return handler

    event_names = [
        "notify_aux_connected", "notify_usb_connected", "notify_battery_level",
        "notify_charger_status", "notify_color", "notify_battery_friendly_charging",
        "notify_eco_mode",
    ]
    request_names = [
        "soft_reset", "get_firmware_version", "set_power_state", "enter_dfu_mode",
        "set_audio_source", "set_volume", "play_sound_icon", "stop_sound_icon",
        "get_a2dp_data", "send_avrcp_action", "set_absolute_avrcp_volume",
        "get_paired_device_list", "get_device_name", "disconnect_all_bt_devices",
        "enable_bt_reconnection", "clear_bt_paired_device_list", "set_bt_pairing_state",
        "get_bt_pairing_state", "get_bt_connection_state", "get_csb_state",
        "exit_csb_mode", "get_bt_mac_address", "get_ble_mac_address",
        "get_bt_rssi_value", "get_this_device_name", "send_usb_hid_action",
        "send_app_packet",
    ]
    for name in event_names:
        client.on_event(name, log_event(name))
    for name in request_names:
        # Note: a couple of these (get_firmware_version, get_this_device_name,
        # get_a2dp_data, get_bt_mac_address, ...) really want a differently
        # typed response than plain Common.Result - fine for a quick listen
        # session, but this generic handler will log a "dispatch failed"
        # for those rather than crash anything.
        client.on_request(name, log_request(name))

    print(f"\nListening for {seconds:.0f}s - press buttons / turn knobs on the speaker now ...\n")
    time.sleep(seconds)


if __name__ == "__main__":
    args = sys.argv[1:]
    do_handshake = "--handshake" in args
    listen_for = 0.0
    for a in args:
        if a.startswith("--listen="):
            listen_for = float(a.split("=", 1)[1])
    args = [a for a in args if not a.startswith("--")]
    port_arg = args[0] if args else "/dev/serial0"

    if do_handshake:
        handshake(port_arg, listen_seconds=listen_for)
    else:
        sniff(port_arg)
