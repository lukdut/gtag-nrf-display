"""On-demand device checks, independent of image delivery and freshness."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from homeassistant.util import dt as dt_util


class ConnectionCheckError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class ConnectionCheck:
    def __init__(self, display) -> None:
        self.display = display
        self.status = "not_checked"
        self.checked_at = None
        self.seconds = None
        self.error_code = None
        self.detail = None
        self._task = None

    def attributes(self) -> dict:
        display = self.display
        return {
            "transport": display.transport,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "duration_seconds": self.seconds,
            "error_code": self.error_code,
            "error_detail": self.detail,
            "last_display_update": display.last_success.isoformat() if display.last_success else None,
            "last_confirmation": display.last_confirmation.isoformat() if display.last_confirmation else None,
            "display_status": display.status,
            "display_error": display.last_error,
            "display_stale": display.stale,
            **display.device_firmware_info,
        }

    async def async_run(self) -> dict:
        # Button, options flow and service share one check for this device.
        if self._task is None or self._task.done():
            self._task = self.display.hass.async_create_background_task(
                self._run(), f"GTag check connection {self.display.address}")
        return await asyncio.shield(self._task)

    async def _run(self) -> dict:
        from .transport import check_connection

        display = self.display
        self.status = "checking"
        self.error_code = self.detail = None
        self.checked_at = self.seconds = None
        display._notify()
        started = display.hass.loop.time()
        try:
            if display._closed:
                raise ConnectionCheckError("unloaded")
            if display.status == "sending":
                raise ConnectionCheckError("device_busy")
            async with asyncio.timeout(60):
                info = (await display.network.async_check_connection() if display.network is not None
                        else await check_connection(display.hass, display.address))
            if display.network is not None:
                display.network.firmware_info = info.attributes()
            else:
                display.firmware_info = info.attributes()
            try:
                info.validate_transfer(18 if display.network is None else 1)
            except ValueError as err:
                raise ConnectionCheckError("incompatible_firmware", str(err)) from err
            self.status = "ok"
        except asyncio.CancelledError:
            self.status = "error"
            self.error_code = "unloaded"
            raise
        except ConnectionCheckError as err:
            self.status = "error"
            self.error_code, self.detail = err.code, str(err)
        except TimeoutError:
            self.status = "error"
            self.error_code = "timeout"
        except Exception as err:
            self.status = "error"
            self.error_code, self.detail = "communication_error", str(err)
        finally:
            self.checked_at = dt_util.utcnow()
            self.seconds = round(display.hass.loop.time() - started, 2)
            display._notify()
        return {"status": self.status, **self.attributes()}

    async def async_close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
