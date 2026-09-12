#!/usr/bin/env python3
"""Standalone Actionslink UART diagnostic - no orchestrator, no audio, no
handlers. Just opens the serial port, logs every raw byte and every frame it
can parse, and (optionally) tries the boot handshake.

Run this FIRST on real hardware, before trusting daemon/main.py with actual
audio routing - it answers two questions:
  1. Are we even seeing bytes on this port? (wiring/UART-enable sanity check)
  2. Do those bytes parse as valid Actionslink frames? (protocol sanity check)

Usage:
    python3 scripts/uart_probe.py [port] [--handshake]

    port          defaults to /dev/serial0
    --handshake   also send notify_system_ready + notify_power_state(ON) and
                   try a get_mcu_firmware_version request, like the real
                   daemon does on startup. Only try this once raw frames are
                   confirmed flowing - it fully engages the protocol
                   including sending ACKs back to the MCU.
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


def handshake(port: str) -> None:
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

        version = client.get_mcu_firmware_version(timeout=1.0)
        print(f"MCU firmware version: {version.major}.{version.minor}.{version.patch} ({version.build})")
    except Exception as exc:
        print(f"handshake step failed: {exc}")
    finally:
        client.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    do_handshake = "--handshake" in args
    args = [a for a in args if not a.startswith("--")]
    port_arg = args[0] if args else "/dev/serial0"

    if do_handshake:
        handshake(port_arg)
    else:
        sniff(port_arg)
