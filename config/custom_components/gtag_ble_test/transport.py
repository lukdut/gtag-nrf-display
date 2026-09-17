"""Protocol-v1 BLE transport for gtag_display: RAW/WHITE_RLE and CRC32.

BEGIN/COMMIT use acknowledged writes; bounded data windows use Write Without
Response with STATUS-confirmed progress, retries, resume and ACK fallback.
Disconnect after verification lets the peripheral render and return to idle.
Frame transfer and the virtual LED share one operation lock per device.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time
from typing import Any, TypeVar

from bleak import BleakClient
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.exceptions import HomeAssistantError

from .const import CONTROL_CHAR_UUID, DOMAIN, FRAME_CHAR_UUID, LED_CHAR_UUID, STATUS_CHAR_UUID
from .frame_codec import RAW_FRAME_SIZE
from .frame_protocol import ERROR_NAMES, PreparedFrame

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
FRAME_SIZE = RAW_FRAME_SIZE
# Switch this to "acknowledged" for a conservative A/B test / rollback.
DATA_WRITE_MODE = "windowed"  # "windowed" or "acknowledged"

# Keep the same chunk boundaries in ALL modes and reconnects. This is important
# for accepting identical duplicates when old queued writes arrive late.
# 18 payload bytes + uint16 offset = 20 bytes; no MTU change is needed.
CHUNK_PAYLOAD = 18
WINDOW_CHUNKS = 8
WWR_INTER_CHUNK_DELAY_S = 0.002
WINDOW_SETTLE_S = 0.010
# STATUS can overtake queued writes on a proxy; give the queue time to drain.
STATUS_DRAIN_DELAYS = (0.040, 0.100)
MAX_SHORT_WINDOWS = 3
MAX_STALLED_WINDOWS = 2
# Only used in the acknowledged (fallback) mode. WWR uses the pacing above.
CHUNK_DELAY_S = 0.0
OPERATION_ATTEMPTS = 3  # total, including the first call
SESSION_ATTEMPTS = 5    # initial session plus up to four reconnections
OPERATION_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 180.0
PROGRESS_EVERY_CHUNKS = 16
RETRY_DELAYS = (0.2, 0.5)
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0)

STATE_NAMES = {0: "idle", 1: "receiving", 2: "complete", 3: "error"}

class TransferResyncError(Exception):
    """A new connection + idempotent BEGIN can recover the receiver position."""

class TransferProtocolError(Exception):
    """Do not hide a corrupt frame or incompatible protocol behind endless retries."""

RETRYABLE = (BleakError, TimeoutError, OSError, EOFError, TransferResyncError)

@dataclass(frozen=True)
class Status:
    state: int
    received: int
    crc: int
    error: int

    @classmethod
    def parse(cls, raw: bytes | bytearray) -> Status:
        if len(raw) != 8:
            raise TransferProtocolError(f"Expected 8 status bytes, got {len(raw)}")
        value = cls(raw[0], int.from_bytes(raw[1:3], "little"),
                    int.from_bytes(raw[3:7], "little"), raw[7])
        if value.state not in STATE_NAMES or value.received > FRAME_SIZE:
            raise TransferProtocolError(f"Invalid receiver status: {raw.hex()}")
        return value

    def describe(self, total: int = FRAME_SIZE) -> str:
        return (f"state={STATE_NAMES[self.state]} received={self.received}/{total} "
                f"error={self.error}({ERROR_NAMES.get(self.error, 'unknown')}) "
                f"raw_crc=0x{self.crc:08X}")

@dataclass
class Report:
    frame_id: int
    expected_crc: int
    codec: int
    codec_name: str
    encoded_size: int
    raw_size: int = FRAME_SIZE
    sessions: int = 0
    operation_errors: int = 0
    operation_retries: int = 0
    seconds: float = 0.0
    last_status: str = "not read"
    last_operation: str = "connect"
    requested_mode: str = "windowed"
    effective_mode: str = "windowed_wwr"
    wwr_writes: int = 0
    acknowledged_data_writes: int = 0
    status_reads: int = 0
    windows_sent: int = 0
    short_windows: int = 0
    window_resends: int = 0
    status_drain_reads: int = 0
    fallback_to_response: bool = False
    fallback_reason: str | None = None
    effective_window_chunks: int = WINDOW_CHUNKS
    backend_max_write_without_response: int | None = None
    connect_seconds: float = 0.0
    data_seconds: float = 0.0
    control_seconds: float = 0.0
    disconnect_seconds: float = 0.0

    def attributes(self) -> dict[str, Any]:
        return {
            "frame_id": f"{self.frame_id:08X}",
            "raw_crc32": f"{self.expected_crc:08X}",
            "codec": self.codec,
            "codec_name": self.codec_name,
            "raw_size": self.raw_size,
            "encoded_size": self.encoded_size,
            "bytes_saved": self.raw_size - self.encoded_size,
            "compression_ratio": round(self.encoded_size / self.raw_size, 4),
            "connection_sessions": self.sessions,
            "operation_errors": self.operation_errors,
            "operation_retries": self.operation_retries,
            "duration_seconds": round(self.seconds, 2),
            "receiver_status": self.last_status,
            "transport_version": "0.5-codec",
            "requested_mode": self.requested_mode,
            "transport_mode": self.effective_mode,
            "chunk_payload": CHUNK_PAYLOAD,
            "window_chunks": WINDOW_CHUNKS,
            "effective_window_chunks": self.effective_window_chunks,
            "wwr_writes": self.wwr_writes,
            "acknowledged_data_writes": self.acknowledged_data_writes,
            "status_reads": self.status_reads,
            "windows_sent": self.windows_sent,
            "short_windows": self.short_windows,
            "window_resends": self.window_resends,
            "status_drain_reads": self.status_drain_reads,
            "fallback_to_response": self.fallback_to_response,
            "fallback_reason": self.fallback_reason,
            "backend_max_write_without_response": self.backend_max_write_without_response,
            "connect_seconds": round(self.connect_seconds, 3),
            "data_seconds": round(self.data_seconds, 3),
            "control_seconds": round(self.control_seconds, 3),
            "disconnect_seconds": round(self.disconnect_seconds, 3),
        }

def get_operation_lock(hass: Any, address: str) -> asyncio.Lock:
    locks = hass.data.setdefault(DOMAIN, {}).setdefault("_ble_operation_locks", {})
    return locks.setdefault(address.upper(), asyncio.Lock())

async def _disconnect(client: Any) -> None:
    if client is None:
        return
    try:
        async with asyncio.timeout(5.0):
            await client.disconnect()
    except (BleakError, TimeoutError, OSError, EOFError) as err:
        # Cleanup failures must not replace the original transfer error/success.
        _LOGGER.debug("BLE disconnect cleanup failed: %r", err)

async def connect(hass: Any, address: str) -> BleakClient:
    device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise TransferResyncError(f"No connectable advertisement for {address}")
    return await establish_connection(BleakClient, device, address,
                                      max_attempts=2, timeout=20.0)

class FrameSender:
    """One press = one frame_id, retained across retries and reconnections."""
    def __init__(self, connect_client: Callable[[], Awaitable[Any]], address: str,
                 *, mode: str | None = None) -> None:
        self._connect = connect_client
        self._address = address
        self._mode = DATA_WRITE_MODE if mode is None else mode
        if self._mode not in ("windowed", "acknowledged"):
            raise ValueError("DATA_WRITE_MODE must be 'windowed' or 'acknowledged'")
        self.report: Report | None = None
        self._acknowledged = self._mode == "acknowledged"
        self._window_chunks = WINDOW_CHUNKS
        self._high_water = 0
        self._stalled_windows = 0

    async def _operation(self, client: Any, name: str,
                         operation: Callable[[], Awaitable[T]]) -> T:
        assert self.report is not None
        self.report.last_operation = name
        for attempt in range(OPERATION_ATTEMPTS):
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT_S):
                    return await operation()
            except BleakCharacteristicNotFoundError:
                raise  # Not a weak-link failure; never change UUIDs automatically.
            except RETRYABLE as err:
                self.report.operation_errors += 1
                _LOGGER.warning("%s %s: attempt %s/%s failed: %r",
                                self._address, name, attempt + 1, OPERATION_ATTEMPTS, err)
                # A timed-out ATT request can still be pending in the stack.
                # Reconnect rather than piling up writes on that connection.
                if (isinstance(err, TimeoutError) or not client.is_connected
                        or attempt + 1 == OPERATION_ATTEMPTS):
                    raise
                self.report.operation_retries += 1
                await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
        raise RuntimeError("Unreachable")

    async def _write(self, client: Any, uuid: str, data: bytes, name: str,
                     *, response: bool = True) -> None:
        async def write_once() -> None:
            assert self.report is not None
            if uuid == FRAME_CHAR_UUID:
                if response:
                    self.report.acknowledged_data_writes += 1
                else:
                    self.report.wwr_writes += 1
            await client.write_gatt_char(uuid, data, response=response)
        await self._operation(client, name, write_once)

    async def _status(self, client: Any) -> Status:
        async def read_once() -> Any:
            assert self.report is not None
            self.report.status_reads += 1
            return await client.read_gatt_char(STATUS_CHAR_UUID)
        raw = await self._operation(client, "STATUS", read_once)
        status = Status.parse(raw)
        assert self.report is not None
        self.report.last_status = status.describe(self.report.encoded_size)
        return status

    def _check_complete(self, status: Status) -> None:
        assert self.report is not None
        if (status.state != 2 or status.received != self.report.encoded_size
                or status.error != 0 or status.crc != self.report.expected_crc):
            raise TransferProtocolError(
                f"Final verification failed: "
                f"{status.describe(self.report.encoded_size)}; "
                f"expected raw CRC=0x{self.report.expected_crc:08X}")

    def _fall_back(self, reason: str) -> None:
        assert self.report is not None
        self._acknowledged = True
        self.report.fallback_to_response = True
        self.report.fallback_reason = reason
        self.report.effective_mode = (
            "windowed_wwr_then_acknowledged" if self.report.wwr_writes else "acknowledged"
        )
        _LOGGER.warning("%s falling back to acknowledged writes: %s", self._address, reason)

    def _check_wwr_support(self, client: Any) -> None:
        """Use public Bleak APIs; no assumptions about local vs proxy transport."""
        if self._acknowledged:
            return
        assert self.report is not None
        characteristic = client.services.get_characteristic(FRAME_CHAR_UUID)
        if characteristic is None:
            raise BleakCharacteristicNotFoundError(FRAME_CHAR_UUID)
        if "write-without-response" not in characteristic.properties:
            self._fall_back("FRAME does not advertise Write Without Response support")
            return
        limit = int(characteristic.max_write_without_response_size)
        self.report.backend_max_write_without_response = limit
        if limit < CHUNK_PAYLOAD + 2:
            self._fall_back(f"Backend WWR limit is {limit}, need {CHUNK_PAYLOAD + 2}")

    def _validate_prefix(self, status: Status, minimum: int) -> None:
        if status.state == 3:
            raise TransferProtocolError(status.describe())
        if status.state != 1:
            raise TransferResyncError(f"Receiver is no longer receiving: {status.describe()}")
        if status.error not in (0, 2, 4):
            raise TransferProtocolError(f"Unexpected receiver error: {status.describe()}")
        if status.received < minimum:
            # A reset or a different session invalidated the prefix. Only a new
            # BEGIN/RESUME after reconnect may establish a fresh session safely.
            raise TransferResyncError(f"Receiver prefix went backwards: {status.describe()}")
        if status.received > self._high_water:
            raise TransferProtocolError(f"Receiver claims unsent bytes: {status.describe()}")
        if status.received != self.report.encoded_size and status.received % CHUNK_PAYLOAD:
            raise TransferProtocolError(f"Receiver prefix is not a chunk boundary: {status.describe()}")

    async def _window_status(self, client: Any, start: int, target: int) -> Status:
        """Never treat return from write_gatt_char(response=False) as delivery."""
        assert self.report is not None
        if WINDOW_SETTLE_S:
            await asyncio.sleep(WINDOW_SETTLE_S)
        status = await self._status(client)
        self._validate_prefix(status, start)
        for wait in STATUS_DRAIN_DELAYS:
            if status.received >= target:
                break
            # A read may have overtaken writes still queued in the proxy or OS.
            # Recheck briefly before retransmitting the unconfirmed suffix.
            if wait:
                await asyncio.sleep(wait)
            self.report.status_drain_reads += 1
            status = await self._status(client)
            self._validate_prefix(status, start)
        return status

    async def _send_acknowledged(self, client: Any, frame: bytes, offset: int) -> int:
        writes = 0
        while offset < len(frame):
            data = frame[offset:offset + CHUNK_PAYLOAD]
            self._high_water = max(self._high_water, offset + len(data))
            await self._write(client, FRAME_CHAR_UUID, offset.to_bytes(2, "little") + data,
                              f"FRAME/ACK offset={offset} len={len(data)}")
            offset += len(data)
            writes += 1
            if writes % PROGRESS_EVERY_CHUNKS == 0:
                status = await self._status(client)
                self._validate_prefix(status, offset)
                offset = status.received
            if CHUNK_DELAY_S:
                await asyncio.sleep(CHUNK_DELAY_S)
        return offset

    async def _send_data(self, client: Any, frame: bytes, offset: int) -> int:
        assert self.report is not None
        # BEGIN/STATUS established the transfer ID/CRC, NOT authentication.
        # Reset bounds to this connection's confirmed prefix.
        self._high_water = offset
        self._validate_prefix(Status(1, offset, 0, 0), offset)
        while offset < len(frame):
            if self._acknowledged:
                return await self._send_acknowledged(client, frame, offset)
            start = offset
            target = min(len(frame), start + CHUNK_PAYLOAD * self._window_chunks)
            self.report.windows_sent += 1
            for pos in range(start, target, CHUNK_PAYLOAD):
                data = frame[pos:pos + CHUNK_PAYLOAD]
                self._high_water = max(self._high_water, pos + len(data))
                await self._write(client, FRAME_CHAR_UUID, pos.to_bytes(2, "little") + data,
                                  f"FRAME/WWR offset={pos} len={len(data)}", response=False)
                # Yield between commands, never launch unbounded concurrent writes.
                await asyncio.sleep(WWR_INTER_CHUNK_DELAY_S)
            status = await self._window_status(client, start, target)
            offset = status.received  # ONLY receiver-confirmed bytes count as progress.
            if offset >= target:
                self._stalled_windows = 0
                continue
            self.report.short_windows += 1
            self.report.window_resends += 1
            self._stalled_windows = self._stalled_windows + 1 if offset == start else 0
            _LOGGER.warning("%s short window %s..%s, receiver=%s; retry suffix",
                            self._address, start, target, offset)
            # Shrink the next batch on congestion; do not change chunk boundaries.
            self._window_chunks = max(1, self._window_chunks // 2)
            self.report.effective_window_chunks = self._window_chunks
            if (self.report.short_windows >= MAX_SHORT_WINDOWS or
                    self._stalled_windows >= MAX_STALLED_WINDOWS):
                self._fall_back("Repeated incomplete WWR windows; continue from confirmed prefix")
        return offset

    async def send(self, frame: bytes) -> Report:
        if len(frame) != FRAME_SIZE:
            raise ValueError(f"Frame must have exactly {FRAME_SIZE} bytes")

        prepared = PreparedFrame.prepare(frame)
        desc = prepared.descriptor
        payload = prepared.payload

        start = time.monotonic()
        self._acknowledged = self._mode == "acknowledged"
        self._window_chunks = WINDOW_CHUNKS
        self._high_water = 0
        self._stalled_windows = 0

        self.report = Report(
            desc.frame_id,
            desc.raw_crc32,
            desc.codec,
            prepared.codec_name,
            desc.encoded_size,
            raw_size=prepared.raw_size,
            requested_mode=self._mode,
            effective_mode="acknowledged" if self._acknowledged else "windowed_wwr",
        )

        # BLE adapter for the transport-neutral BEGIN descriptor:
        # cmd | proto | codec | encoded_size:u16 | frame_id:u32 | raw_crc:u32
        begin = (
            b"\x01"
            + bytes([desc.version, desc.codec])
            + desc.encoded_size.to_bytes(2, "little")
            + desc.frame_id.to_bytes(4, "little")
            + desc.raw_crc32.to_bytes(4, "little")
        )
        last_error: BaseException | None = None
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_S):
                for session in range(SESSION_ATTEMPTS):
                    client = None
                    self.report.sessions = session + 1
                    try:
                        self.report.last_operation = "CONNECT"
                        stage_start = time.monotonic()
                        try:
                            client = await self._connect()
                        finally:
                            self.report.connect_seconds += time.monotonic() - stage_start
                        self._check_wwr_support(client)
                        stage_start = time.monotonic()
                        try:
                            await self._write(client, CONTROL_CHAR_UUID, begin, "BEGIN/RESUME")
                            status = await self._status(client)
                        finally:
                            self.report.control_seconds += time.monotonic() - stage_start
                        if status.state == 2:
                            # COMMIT succeeded before a lost response/disconnect.
                            self._check_complete(status)
                            return self.report
                        if status.state != 1:
                            raise TransferResyncError(f"Cannot resume: {status.describe()}")
                        offset = status.received
                        if session:
                            _LOGGER.warning("%s resume at receiver-confirmed %s/%s",
                                            self._address, offset, self.report.encoded_size)
                        stage_start = time.monotonic()
                        try:
                            await self._send_data(client, payload, offset)
                        finally:
                            self.report.data_seconds += time.monotonic() - stage_start
                        stage_start = time.monotonic()
                        try:
                            await self._write(client, CONTROL_CHAR_UUID, b"\x02", "COMMIT")
                            self._check_complete(await self._status(client))
                        finally:
                            self.report.control_seconds += time.monotonic() - stage_start
                        return self.report
                    except BleakCharacteristicNotFoundError as err:
                        raise HomeAssistantError(
                            f"GATT characteristic missing: {err}. "
                            "Check that the nRF firmware and existing const.py use 0011..0015.") from err
                    except RETRYABLE as err:
                        last_error = err
                        failed_operation = self.report.last_operation
                        # Preserve the *original* operation in the diagnostic.
                        if (client is not None and client.is_connected
                                and not isinstance(err, TimeoutError)):
                            try:
                                async with asyncio.timeout(3.0):
                                    raw = await client.read_gatt_char(STATUS_CHAR_UUID)
                                    self.report.last_status = Status.parse(raw).describe(self.report.encoded_size)
                            except (BleakError, TimeoutError, OSError, EOFError,
                                    TransferProtocolError):
                                pass
                        self.report.last_operation = failed_operation
                        _LOGGER.warning(
                            "%s session %s/%s failed at %s: %r; receiver: %s",
                            self._address, session + 1, SESSION_ATTEMPTS,
                            failed_operation, err, self.report.last_status)
                    finally:
                        stage_start = time.monotonic()
                        await _disconnect(client)
                        self.report.disconnect_seconds += time.monotonic() - stage_start
                    if session + 1 < SESSION_ATTEMPTS:
                        await asyncio.sleep(RECONNECT_DELAYS[min(session, len(RECONNECT_DELAYS)-1)])
                raise HomeAssistantError(
                    f"Transfer failed after {SESSION_ATTEMPTS} sessions. "
                    f"Last operation: {self.report.last_operation}. "
                    f"Receiver: {self.report.last_status}. Cause: {last_error!r}") from last_error
        except TimeoutError as err:
            raise HomeAssistantError(
                f"Transfer exceeded {TOTAL_TIMEOUT_S:.0f}s; "
                f"operation={self.report.last_operation}; {self.report.last_status}") from err
        except TransferProtocolError as err:
            raise HomeAssistantError(f"Frame verification/protocol error: {err}") from err
        finally:
            self.report.seconds = time.monotonic() - start

async def write_led(hass: Any, address: str, state: bool) -> None:
    lock = get_operation_lock(hass, address)
    if lock.locked():
        raise HomeAssistantError("BLE operation is in progress; wait for the frame transfer")
    async with lock:
        last_error: BaseException | None = None
        try:
            async with asyncio.timeout(90.0):
                for attempt in range(3):
                    client = None
                    try:
                        client = await connect(hass, address)
                        async with asyncio.timeout(OPERATION_TIMEOUT_S):
                            await client.write_gatt_char(LED_CHAR_UUID, bytes([int(state)]),
                                                         response=True)
                        return
                    except BleakCharacteristicNotFoundError as err:
                        raise HomeAssistantError(f"LED characteristic missing: {err}") from err
                    except RETRYABLE as err:
                        last_error = err
                        _LOGGER.warning("%s LED attempt %s failed: %r", address, attempt + 1, err)
                    finally:
                        await _disconnect(client)
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
        except TimeoutError as err:
            raise HomeAssistantError("LED operation timed out") from err
        raise HomeAssistantError(f"LED write failed: {last_error!r}") from last_error
