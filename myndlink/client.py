"""High-level Actionslink client for the Raspberry Pi side of MYNDberry.

On the real MYND speaker, the STM32 main MCU talks to a Bluetooth/Actions
co-processor over UART1 (115200 8N1) using the Actionslink protocol. In
MYNDberry the Pi's GPIO UART takes the place of that co-processor, so this
client speaks the *peer* side of the protocol:

    - Frames the MCU transmits carry an ``ActionsLink.FromMcu`` payload.
    - Frames we transmit must carry an ``ActionsLink.ToMcu`` payload.

See reference/mynd-firmware/.../actionslink/src/{transport,api}/*.c for the
original (MCU-side) implementation this was reverse engineered from.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import serial

sys.path.insert(0, str(Path(__file__).parent / "pb"))
import message_pb2 as pb  # noqa: E402
import audio_pb2  # noqa: E402
import system_pb2  # noqa: E402
import device_pb2  # noqa: E402

from .hdlc import (  # noqa: E402
    Frame,
    FrameError,
    FrameParser,
    PacketType,
    encode_frame,
)

log = logging.getLogger("myndlink")

DEFAULT_PORT = "/dev/serial0"
DEFAULT_BAUDRATE = 115200  # from board_hw.h: BLUETOOTH_UART_BAUDRATE
ACK_TIMEOUT_S = 0.3  # matches MESSAGE_RESPONSE_TIMEOUT_MS
MAX_RETRIES = 2  # matches MAX_NUMBER_OF_TX_RETRIES


class ActionslinkError(Exception):
    """Raised when a request could not be delivered/acknowledged/answered."""


class ActionslinkTimeout(ActionslinkError):
    pass


@dataclass
class _PendingAck:
    transaction_id: int
    event: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    nack_reason: int = 0


class ActionslinkClient:
    """Speaks Actionslink over a serial port and dispatches events/requests.

    Usage::

        client = ActionslinkClient("/dev/serial0")
        client.on_event("notify_power_state", handle_power_state)
        client.on_request("set_audio_source", handle_set_audio_source)
        client.start()
        client.notify_system_ready()
    """

    def __init__(self, port: str = DEFAULT_PORT, baudrate: int = DEFAULT_BAUDRATE, timeout: float = 0.05):
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._parser = FrameParser()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._send_lock = threading.Lock()
        self._next_transaction_id = 0
        self._next_seq = 0
        self._pending_ack: Optional[_PendingAck] = None

        # Responses to our own outgoing ToMcuRequests, keyed by the request's
        # `which_Request` field name (e.g. "get_mcu_firmware_version").
        self._response_waiters: dict[str, "queue.Queue[pb.FromMcuResponse]"] = {}

        self._event_handlers: dict[str, list[Callable[[object], None]]] = {}
        self._request_handlers: dict[str, Callable[[object, int], object]] = {}

        # Events and requests are dispatched on a separate worker thread.
        # A request handler's response send blocks waiting for an ACK, and
        # that ACK is only ever observed by the reader thread - if dispatch
        # ran inline on the reader thread it would be waiting on itself.
        self._dispatch_thread: Optional[threading.Thread] = None
        self._dispatch_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, name="actionslink-dispatch", daemon=True)
        self._dispatch_thread.start()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="actionslink-reader", daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=1)
        self._dispatch_queue.put(None)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=1)
        self._serial.close()

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def on_event(self, name: str, handler: Callable[[object], None]) -> None:
        """Register a callback for a ``FromMcuEvent`` field, e.g. ``"notify_battery_level"``."""
        self._event_handlers.setdefault(name, []).append(handler)

    def on_request(self, name: str, handler: Callable[[object, int], object]) -> None:
        """Register a handler for a ``FromMcuRequest`` field, e.g. ``"set_audio_source"``.

        The handler receives ``(request_submessage, seq)`` and must return an
        ``actionslink_error_t``-style result: either ``None`` (success, no
        extra data), or a value to place in the matching ``ToMcuResponse``
        field. Raise an exception to send back a failure result.
        """
        self._request_handlers[name] = handler

    # ------------------------------------------------------------------ #
    # Reader loop
    # ------------------------------------------------------------------ #

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(256)
            except serial.SerialException:
                log.exception("serial read failed")
                time.sleep(0.5)
                continue
            if not chunk:
                continue
            for result in self._parser.feed(chunk):
                if isinstance(result, FrameError):
                    log.warning("bad frame: %s", result)
                    continue
                self._handle_frame(result)

    def _handle_frame(self, frame: Frame) -> None:
        if frame.packet_type == PacketType.ACK:
            self._handle_ack(frame)
            return

        # PROTOBUF frame: decode as FromMcu.
        message = pb.FromMcu()
        try:
            message.ParseFromString(frame.payload)
        except Exception:
            log.exception("failed to decode FromMcu payload")
            self._send_raw_ack(frame.transaction_id, PacketType.ACK, value=1)  # NACK_REASON_BAD_PACKET
            return

        # Always ack a successfully decoded protobuf frame first.
        self._send_raw_ack(frame.transaction_id, PacketType.ACK, value=0)

        which = message.WhichOneof("Payload")
        if which == "event":
            # Deferred to the dispatch thread: user handlers run here, and
            # must not block the reader thread that feeds their ACKs.
            self._dispatch_queue.put(("event", message.event))
        elif which == "request":
            self._dispatch_queue.put(("request", message.request))
        elif which == "response":
            # Cheap and non-blocking: safe to handle inline on the reader thread.
            self._dispatch_response(message.response)
        else:
            log.warning("received FromMcu with no payload set")

    def _handle_ack(self, frame: Frame) -> None:
        pending = self._pending_ack
        if pending is None or pending.transaction_id != frame.transaction_id:
            log.debug("received unexpected ack/nack (tx id %d)", frame.transaction_id)
            return
        pending.ok = frame.value == 0
        pending.nack_reason = frame.value
        pending.event.set()

    def _dispatch_loop(self) -> None:
        while True:
            item = self._dispatch_queue.get()
            if item is None:  # sentinel from stop()
                return
            kind, payload = item
            try:
                if kind == "event":
                    self._dispatch_event(payload)
                elif kind == "request":
                    self._dispatch_request(payload)
            except Exception:
                log.exception("dispatch of %s failed", kind)

    def _dispatch_event(self, event: "pb.FromMcuEvent") -> None:
        name = event.WhichOneof("Event")
        if name is None:
            return
        value = getattr(event, name)
        for handler in self._event_handlers.get(name, []):
            try:
                handler(value)
            except Exception:
                log.exception("event handler for %s failed", name)

    def _dispatch_request(self, request: "pb.FromMcuRequest") -> None:
        name = request.WhichOneof("Request")
        seq = request.seq
        if name is None:
            return
        handler = self._request_handlers.get(name)
        if handler is None:
            log.warning("no handler registered for request %s", name)
            return
        value = getattr(request, name)
        try:
            result = handler(value, seq)
        except Exception:
            log.exception("request handler for %s failed", name)
            return
        if result is not None:
            self._send_response(name, seq, result)

    def _dispatch_response(self, response: "pb.FromMcuResponse") -> None:
        name = response.WhichOneof("Response")
        if name is None:
            return
        q = self._response_waiters.get(name)
        if q is not None:
            q.put(response)
        else:
            log.debug("received unsolicited response %s", name)

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    def _send_raw_ack(self, transaction_id: int, packet_type: PacketType, value: int) -> None:
        frame = encode_frame(packet_type, value, transaction_id)
        self._serial.write(frame)

    def _next_tx_id(self) -> int:
        tx_id = self._next_transaction_id
        self._next_transaction_id = (self._next_transaction_id + 1) & 0xFF
        return tx_id

    def _transmit_with_ack(self, payload: bytes) -> None:
        """Send one PROTOBUF frame and block until it is ACKed, with retries.

        Callers must hold ``self._send_lock``: the protocol is half-duplex
        (mirroring the firmware's single-outstanding-transaction state
        machine), and ``self._pending_ack`` is shared, unguarded state.
        """
        last_nack_reason = None
        for _attempt in range(MAX_RETRIES + 1):
            tx_id = self._next_tx_id()
            pending = _PendingAck(transaction_id=tx_id)
            self._pending_ack = pending

            frame = encode_frame(PacketType.PROTOBUF, 0, tx_id, payload)
            self._serial.write(frame)

            if pending.event.wait(ACK_TIMEOUT_S) and pending.ok:
                self._pending_ack = None
                return
            last_nack_reason = pending.nack_reason
        self._pending_ack = None
        raise ActionslinkTimeout(f"no ack after {MAX_RETRIES + 1} attempts (last nack reason: {last_nack_reason})")

    @staticmethod
    def _set_oneof_field(container, field_name: str, value) -> None:
        """Set a field inside a protobuf ``oneof``, whether scalar or submessage.

        Message-typed fields can't be assigned via ``setattr`` in the
        protobuf Python API (that raises); they must be reached via
        ``getattr`` and mutated in place (``SetInParent``/``CopyFrom``).
        """
        if value is None:
            getattr(container, field_name).SetInParent()
        elif hasattr(value, "CopyFrom"):
            getattr(container, field_name).CopyFrom(value)
        else:
            setattr(container, field_name, value)

    def _send_event(self, event_field: str, value=None) -> None:
        message = pb.ToMcu()
        message.event.SetInParent()
        self._set_oneof_field(message.event, event_field, value)
        with self._send_lock:
            self._transmit_with_ack(message.SerializeToString())

    def _send_response(self, field_name: str, seq: int, value) -> None:
        message = pb.ToMcu()
        message.response.seq = seq
        self._set_oneof_field(message.response, field_name, value)
        with self._send_lock:
            self._transmit_with_ack(message.SerializeToString())

    def _send_request(self, field_name: str, value=None, expect_response: bool = True, timeout: float = ACK_TIMEOUT_S):
        message = pb.ToMcu()
        seq = self._next_seq
        self._next_seq = (self._next_seq + 1) & 0xFF
        message.request.seq = seq
        self._set_oneof_field(message.request, field_name, value)

        response_queue: "queue.Queue[pb.FromMcuResponse]" = queue.Queue(maxsize=1)
        if expect_response:
            self._response_waiters[field_name] = response_queue

        try:
            with self._send_lock:
                self._transmit_with_ack(message.SerializeToString())
                if not expect_response:
                    return None
                try:
                    response = response_queue.get(timeout=timeout)
                except queue.Empty as exc:
                    raise ActionslinkTimeout(f"no response to request {field_name!r}") from exc
                return getattr(response, field_name)
        finally:
            self._response_waiters.pop(field_name, None)

    # ------------------------------------------------------------------ #
    # Convenience wrappers (ToMcuEvent / ToMcuRequest)
    # ------------------------------------------------------------------ #

    def notify_system_ready(self) -> None:
        self._send_event("notify_system_ready")

    def notify_power_state(self, mode: "system_pb2.PowerState.SystemPowerMode") -> None:
        self._send_event("notify_power_state", system_pb2.PowerState(mode=mode))

    def notify_audio_source(self, source: "audio_pb2.AudioSourceType") -> None:
        self._send_event("notify_audio_source", audio_pb2.Source(source=source))

    def notify_volume_percent(self, percent: int, is_muted: bool = False) -> None:
        self._send_event("notify_volume", audio_pb2.Volume(percent=percent, is_muted=is_muted))

    def notify_stream_state(self, is_streaming: bool) -> None:
        self._send_event("notify_stream_state", is_streaming)

    def get_mcu_firmware_version(self, timeout: float = ACK_TIMEOUT_S) -> "system_pb2.FirmwareVersion":
        return self._send_request("get_mcu_firmware_version", timeout=timeout)

    def get_color(self, timeout: float = ACK_TIMEOUT_S) -> "device_pb2.Color":
        return self._send_request("get_color", timeout=timeout)

    def set_brightness(self, value: int, timeout: float = ACK_TIMEOUT_S):
        return self._send_request("set_brightness", value, timeout=timeout)

    def get_battery_capacity(self, timeout: float = ACK_TIMEOUT_S) -> int:
        return self._send_request("get_battery_capacity", timeout=timeout)
