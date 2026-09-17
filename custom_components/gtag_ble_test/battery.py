"""Infrequent battery reads, serialized with all other BLE operations."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
import logging

from bleak.exc import BleakCharacteristicNotFoundError
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import BATTERY_CHAR_UUID
from .transport import RETRYABLE, TransferResyncError, _disconnect, connect, get_operation_lock

_LOGGER = logging.getLogger(__name__)
POLL_INTERVAL = 300
BUSY_RETRY_INTERVAL = 30
READ_TIMEOUT = 30
STARTUP_RETRY_DELAYS = (5, 15, 30)


def parse_battery_voltage(raw: bytes | bytearray) -> float | None:
    """Two little-endian millivolt bytes; 0xFFFF means no valid measurement."""
    if len(raw) != 2:
        raise ValueError(f"Expected 2 battery bytes, got {len(raw)}")
    mv = int.from_bytes(raw, "little")
    if mv == 0xFFFF:
        return None
    if mv > 6000:
        raise ValueError(f"Invalid battery voltage: {mv} mV")
    return mv / 1000


class BatteryMonitor:
    def __init__(self, hass: HomeAssistant, address: str, notify: Callable[[], None]) -> None:
        self.hass = hass
        self.address = address
        self.voltage: float | None = None
        self.last_read: datetime | None = None
        self.last_error: str | None = None
        self.supported: bool | None = None
        self._notify = notify
        self._task: asyncio.Task | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._closed = False
        self._started = False
        self._startup_retries = 0
        self._received_voltage = False
        self._cache_refresh_attempted = False
        self._use_services_cache = True

    @callback
    def async_start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._start_poll(None)

    @callback
    def _start_poll(self, _now) -> None:
        self._cancel_timer = None
        if not self._closed:
            self._task = self.hass.async_create_background_task(
                self._async_poll(), f"GTag battery {self.address}",
            )

    async def _async_poll(self) -> None:
        delay = POLL_INTERVAL
        notify = False
        try:
            lock = get_operation_lock(self.hass, self.address)
            if lock.locked():
                # Let the active display/LED operation finish; do not queue
                # another connection immediately behind a long frame transfer.
                delay = BUSY_RETRY_INTERVAL
                return
            async with lock:
                client = None
                try:
                    async with asyncio.timeout(READ_TIMEOUT):
                        client = await connect(
                            self.hass, self.address, use_services_cache=self._use_services_cache,
                        )
                        try:
                            raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
                        except BleakCharacteristicNotFoundError:
                            if self._cache_refresh_attempted:
                                raise
                            # A firmware update can leave the old GATT table in
                            # HA's adapter/proxy cache. Refresh once under the
                            # operation lock and reconnect with fresh discovery.
                            self._cache_refresh_attempted = True
                            self._use_services_cache = False
                            delay = STARTUP_RETRY_DELAYS[0]
                            await client.clear_cache()
                            raise TransferResyncError("battery_services_refresh_pending") from None
                        self._use_services_cache = True
                        self.voltage = parse_battery_voltage(raw)
                        if self.voltage is not None:
                            self._received_voltage = True
                        self.supported = True
                        self.last_read = dt_util.utcnow()
                        self.last_error = None if self.voltage is not None else "measurement_unavailable"
                finally:
                    # Release the shared lock only after disconnecting.
                    await _disconnect(client)
            notify = True
        except BleakCharacteristicNotFoundError:
            # Still absent after fresh discovery: pre-0.7 firmware remains
            # usable for screens. Reload after upgrading the firmware.
            self.voltage = None
            self.supported = False
            self.last_error = "firmware_unsupported"
            notify = True
        except (*RETRYABLE, ValueError) as err:
            self.voltage = None
            self.last_error = str(err) or type(err).__name__
            _LOGGER.debug("%s: battery read failed: %s", self.address, err)
            notify = True
        finally:
            self._task = None
            if not self._closed:
                if self.supported is not False:
                    # HA may start while the board is still booting or before
                    # its first advertisement reaches the adapter. Do not make
                    # the first valid value wait a full five minutes, but bound
                    # retries so an absent device cannot cause constant scans.
                    if (notify and not self._received_voltage
                            and self._startup_retries < len(STARTUP_RETRY_DELAYS)):
                        delay = STARTUP_RETRY_DELAYS[self._startup_retries]
                        self._startup_retries += 1
                    self._cancel_timer = async_call_later(self.hass, delay, self._start_poll)
                if notify:
                    self._notify()

    async def async_close(self) -> None:
        self._closed = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
