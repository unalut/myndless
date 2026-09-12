"""End-to-end loopback test for ActionslinkClient using a pty pair.

Simulates the MCU side by hand: reads/writes raw HDLC frames on one end of a
pty, while ActionslinkClient runs the real Pi-side stack on the other end.
No real hardware needed.
"""

import logging
import os
import pty
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "myndlink" / "pb"))

from myndlink.client import ActionslinkClient  # noqa: E402
from myndlink.hdlc import FrameParser, PacketType, encode_frame  # noqa: E402
import message_pb2 as pb  # noqa: E402
import system_pb2  # noqa: E402
import audio_pb2  # noqa: E402


def mcu_side(fd, results):
    """Very small stand-in for the MCU: acks everything, and answers
    get_mcu_firmware_version. Also proactively sends a set_audio_source
    request to the client and expects a response back."""
    parser = FrameParser()
    next_tx_id = [100]

    def send(packet_type, value, payload=b"", tx_id=None):
        if tx_id is None:
            tx_id = next_tx_id[0]
            next_tx_id[0] = (next_tx_id[0] + 1) & 0xFF
        os.write(fd, encode_frame(packet_type, value, tx_id, payload))

    def send_set_audio_source():
        msg = pb.FromMcu()
        msg.request.seq = 1
        msg.request.set_audio_source.source = audio_pb2.AudioSourceType.ANALOG
        send(PacketType.PROTOBUF, 0, msg.SerializeToString())

    deadline = time.time() + 2
    while time.time() < deadline:
        chunk = os.read(fd, 256)
        for frame in parser.feed(chunk):
            if isinstance(frame, Exception):
                continue
            if frame.packet_type == PacketType.ACK:
                continue  # ack for our own outgoing request
            decoded = pb.ToMcu()
            decoded.ParseFromString(frame.payload)
            which = decoded.WhichOneof("Payload")

            # We must ack every protobuf frame we receive, echoing its transaction id.
            send(PacketType.ACK, 0, tx_id=frame.transaction_id)

            if which == "event" and decoded.event.WhichOneof("Event") == "notify_system_ready":
                results["got_system_ready"] = True
                send_set_audio_source()
            elif which == "request" and decoded.request.WhichOneof("Request") == "get_mcu_firmware_version":
                resp = pb.FromMcu()
                resp.response.seq = decoded.request.seq
                resp.response.get_mcu_firmware_version.CopyFrom(
                    system_pb2.FirmwareVersion(major=1, minor=2, patch=3, build="test")
                )
                send(PacketType.PROTOBUF, 0, resp.SerializeToString())
            elif which == "response" and decoded.response.WhichOneof("Response") == "set_audio_source":
                results["audio_source_result"] = decoded.response.set_audio_source

            if results.get("got_system_ready") and "fw_version_sent" not in results:
                results["fw_version_sent"] = True

        if all(k in results for k in ("got_system_ready", "audio_source_result")):
            break


def main():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)

    results = {}
    mcu_thread = threading.Thread(target=mcu_side, args=(master_fd, results), daemon=True)
    mcu_thread.start()

    client = ActionslinkClient(port=slave_name)
    client.start()

    def handle_set_audio_source(value, seq):
        print(f"[client] MCU asked to set audio source to {value.source} (seq {seq})")
        import common_pb2
        import error_pb2

        result = common_pb2.Result()
        result.status.code = error_pb2.Code.Success
        return result

    client.on_request("set_audio_source", handle_set_audio_source)

    client.notify_system_ready()
    print("[client] sent notify_system_ready")

    version = client.get_mcu_firmware_version(timeout=1.0)
    print(f"[client] got firmware version: {version.major}.{version.minor}.{version.patch} ({version.build})")
    assert (version.major, version.minor, version.patch, version.build) == (1, 2, 3, "test")

    mcu_thread.join(timeout=3)
    assert results.get("got_system_ready"), "MCU never saw notify_system_ready"
    assert "audio_source_result" in results, "client never responded to set_audio_source request"
    print("[client] set_audio_source response reached MCU:", results["audio_source_result"])

    client.stop()
    print("ALL LOOPBACK CHECKS PASSED")


if __name__ == "__main__":
    main()
