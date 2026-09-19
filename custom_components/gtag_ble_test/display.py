"""Per-display queue, rendering, clock updates and last successful preview."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import TrackTemplate, async_track_template_result, async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from .const import CONF_TRANSPORT, DOMAIN, TRANSPORT_BLE, TRANSPORT_ZIGBEE
from .battery import BatteryMonitor
from .connection import ConnectionCheck
from .frame_protocol import PreparedFrame
from .layouts import async_render_layout, normalize_saved_timing, preset_layout, upgrade_saved_preset, validate_settings
from .render import DYNAMIC_TYPES, LAYOUT_SCHEMA, RenderedFrame, clock_layout, from_raw
from .transport import FrameSender, connect, get_operation_lock, renew_freshness

_LOGGER = logging.getLogger(__name__)
MIN_UPDATE_INTERVAL = 5.0
COALESCE_SECONDS = 0.25
# A rebooted peripheral cannot advertise its lost framebuffer with protocol v1.
# Bound local duplicate suppression; Refresh and force always bypass it.
UNCHANGED_MAX_AGE = 900.0


@dataclass
class DrawRequest:
    mode: str
    content: dict[str, Any] | bytes | None
    force: bool
    result: asyncio.Future
    check_only: bool = False


class Display:
    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry_id = entry.entry_id
        self.address = entry.data[CONF_ADDRESS]
        self.transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_BLE)
        self.identity = f"zigbee:{self.address}" if self.transport == TRANSPORT_ZIGBEE else self.address
        self.name = entry.title
        self.clock_enabled = False
        self.auto_update = True
        self.last_layout: dict[str, Any] | None = None
        self.preview: bytes | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.status = "idle"
        self.report: dict[str, Any] = {}
        self.firmware_info: dict[str, Any] = {}
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._listeners: set[Callable[[], None]] = set()
        self._clock_unsubscribe: Callable[[], None] | None = None
        self._template_tracker = None
        self._pending: DrawRequest | None = None
        self._pending_since = 0.0
        self._worker: asyncio.Task | None = None
        self._queue_changed = asyncio.Event()
        self._intent_lock = asyncio.Lock()
        self._closed = False
        self._last_frame: bytes | None = None
        self._last_success_time = float("-inf")
        self._last_attempt_end = float("-inf")
        self._entry_options = dict(entry.options)
        if "screen" in self._entry_options:
            self._entry_options["screen"] = normalize_saved_timing(self._entry_options["screen"])
        self._options_revision = None
        self._configured_interval = self._entry_options.get("screen", {}).get("update_interval")
        self.freshness_timeout = int(self._entry_options.get("screen", {}).get("stale_after", 15)) * 60
        self.last_confirmation: datetime | None = None
        self._confirmed_at: float | None = None
        self.confirmed_timeout: int | None = None
        self._freshness_sequence = 0
        self._freshness_unsubscribe = None
        self._last_check = float("-inf")
        self.zigbee = None
        if self.transport == TRANSPORT_ZIGBEE:
            from .zigbee import ZigbeeTransport

            self.zigbee = ZigbeeTransport(hass, entry, self._notify, self._on_link_restored)
        self.battery = self.zigbee or BatteryMonitor(hass, self.address, self._notify)
        self.connection_check = ConnectionCheck(self)

    @property
    def device_firmware_info(self) -> dict:
        return self.zigbee.firmware_info if self.zigbee is not None else self.firmware_info

    @property
    def update_interval(self) -> float:
        return (max(MIN_UPDATE_INTERVAL, float(self._configured_interval))
                if self._configured_interval is not None else MIN_UPDATE_INTERVAL)

    def _select_settings(self, settings: dict[str, Any], revision: str) -> None:
        settings = validate_settings(settings)
        self._stop_clock()
        self._stop_watching()
        self._configured_interval = settings["update_interval"]
        self.freshness_timeout = settings["stale_after"] * 60
        self._options_revision = revision
        self.clock_enabled = settings["preset"] == "clock"
        self.auto_update = settings.get("auto_update", True)
        self.last_layout = None if self.clock_enabled else preset_layout(settings)

    async def async_apply_settings(self, settings: dict[str, Any], revision: str) -> None:
        async with self._intent_lock:
            if self._closed or revision == self._options_revision:
                return
            self._select_settings(settings, revision)
            await self._save()
            if self.clock_enabled:
                self._start_clock()
                result = self._enqueue("clock", None, True)
            else:
                self._watch_layout()
                result = self._enqueue("layout", deepcopy(self.last_layout), True)
        await asyncio.shield(result)

    async def async_load(self) -> None:
        if self.zigbee is not None:
            await self.zigbee.async_setup()
        saved = await self._store.async_load() or {}
        if not isinstance(saved, dict):
            _LOGGER.warning("%s: ignoring invalid saved display settings", self.address)
            saved = {}
        self._options_revision = saved.get("options_revision")
        if isinstance(saved.get("last_success"), str):
            self.last_success = dt_util.parse_datetime(saved["last_success"])
        self.clock_enabled = saved.get("clock_enabled", False) is True
        self.auto_update = saved.get("auto_update", True) is True
        if saved.get("layout") is not None:
            try:
                self.last_layout = self._validate_layout(saved["layout"])
            except (vol.Invalid, HomeAssistantError, TypeError) as err:
                _LOGGER.warning("%s: ignoring invalid saved layout: %s", self.address, err)
        # Recover an options save even if HA stopped before its update listener
        # could apply it. A later draw action remains active across restarts.
        if (settings := self._entry_options.get("screen")) and (
            (revision := self._entry_options.get("screen_revision")) != self._options_revision
        ):
            self._select_settings(settings, revision)
            await self._save()
        elif settings and self.last_layout is not None and not self.clock_enabled:
            updated = upgrade_saved_preset(self.last_layout, settings)
            if updated != self.last_layout:
                self.last_layout = updated
                await self._save()

    def _validate_layout(self, layout: dict[str, Any]) -> dict[str, Any]:
        layout = LAYOUT_SCHEMA(layout)
        for element in layout["elements"]:
            if element["type"] == "text":
                Template(element["text"], self.hass).ensure_valid()
        return layout

    def _saved_data(self) -> dict:
        return {
            "clock_enabled": self.clock_enabled, "layout": self.last_layout,
            "auto_update": self.auto_update,
            "options_revision": self._options_revision,
            "last_success": self.last_success.isoformat() if self.last_success else None,
        }

    async def _save(self) -> None:
        await self._store.async_save(self._saved_data())

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _on_link_restored(self) -> None:
        if self._closed or self.last_error is None or self._pending is not None:
            return
        if self.clock_enabled:
            self._enqueue("clock", None, True)
        elif self.last_layout is not None:
            self._enqueue("layout", deepcopy(self.last_layout), True)

    @callback
    def async_start(self) -> None:
        self.battery.async_start()
        if self._freshness_unsubscribe is None:
            self._freshness_unsubscribe = async_track_time_interval(
                self.hass, self._on_freshness_tick, timedelta(seconds=10),
            )
        if self.clock_enabled:
            self._start_clock()
            self._enqueue("clock", None, True)
        elif self.last_layout is not None:
            self._watch_layout()
            self._enqueue("layout", deepcopy(self.last_layout), True)

    @property
    def stale(self) -> bool | None:
        if self._confirmed_at is None or self.report.get("firmware_freshness_supported") is False:
            return None
        if self.confirmed_timeout == 0:
            return False
        return self.hass.loop.time() - self._confirmed_at >= self.confirmed_timeout

    @callback
    def _on_freshness_tick(self, _now) -> None:
        if self._closed:
            return
        self._notify()
        now = self.hass.loop.time()
        if (not self.freshness_timeout or not self.auto_update or self._pending is not None
                or (self._worker is not None and not self._worker.done())
                or now - self._last_check < max(10, self.freshness_timeout / 3)):
            return
        if self.clock_enabled:
            self._enqueue("clock", None, False, check_only=True)
        elif self.last_layout is not None:
            self._enqueue("layout", deepcopy(self.last_layout), False, check_only=True)

    async def _confirm_unchanged(self) -> None:
        started = self.hass.loop.time()
        if (self.report.get("firmware_freshness_supported") is False or not self.freshness_timeout or self._confirmed_at is not None
                and started - self._confirmed_at < self.freshness_timeout / 3):
            return
        self._freshness_sequence += 1
        frame_id = self.report["frame_id"]
        if self.transport == TRANSPORT_BLE:
            frame_id = int(frame_id, 16)
        crc = int(self.report["raw_crc32"], 16)
        if self.zigbee is not None:
            await self.zigbee.async_confirm(frame_id, crc, self._freshness_sequence)
        else:
            await renew_freshness(self.hass, self.address, frame_id, crc, self._freshness_sequence)
        self._confirmed_at = started
        self.last_confirmation = dt_util.utcnow()

    @callback
    def _stop_watching(self) -> None:
        if self._template_tracker is not None:
            self._template_tracker.async_remove()
            self._template_tracker = None

    @callback
    def _watch_layout(self) -> None:
        self._stop_watching()
        if not self.auto_update or self.last_layout is None:
            return
        templates = []
        dynamic = False
        for element in self.last_layout["elements"]:
            if element["type"] == "text":
                template = Template(element["text"], self.hass)
                if not template.is_static:
                    templates.append(TrackTemplate(template, None))
            elif element["type"] in DYNAMIC_TYPES:
                templates.append(TrackTemplate(Template("{{ states[" + repr(element["entity_id"]) + "] }}", self.hass), None))
                dynamic = True
        if dynamic:
            # Forecast and history windows move even if the current state does not.
            templates.append(TrackTemplate(Template("{{ now().minute }}", self.hass), None))
        if templates:
            self._template_tracker = async_track_template_result(
                self.hass, templates, self._on_template_change,
            )
            self._template_tracker.async_refresh()

    @callback
    def _on_template_change(self, _event, _updates) -> None:
        if not self._closed and not self.clock_enabled and self.last_layout is not None:
            self._enqueue("layout", deepcopy(self.last_layout), False)

    @callback
    def _start_clock(self) -> None:
        if self._clock_unsubscribe is None:
            self._clock_unsubscribe = async_track_time_change(
                self.hass, self._on_minute, second=0,
            )

    @callback
    def _on_minute(self, _now: datetime) -> None:
        if self.clock_enabled and not self._closed:
            self._enqueue("clock", None, False)

    @callback
    def _stop_clock(self) -> None:
        self.clock_enabled = False
        if self._clock_unsubscribe is not None:
            self._clock_unsubscribe()
            self._clock_unsubscribe = None
        if self._pending is not None and self._pending.mode == "clock":
            self._resolve(self._pending, {"status": "superseded"})
            self._pending = None
            self._queue_changed.set()
            if self.status == "queued":
                self.status = "sent" if self.last_success else "idle"

    async def async_set_clock(self, enabled: bool) -> None:
        async with self._intent_lock:
            if enabled:
                self._stop_watching()
                self.last_layout = None
                self.clock_enabled = True
                self._start_clock()
            else:
                self._stop_clock()
            await self._save()
            self._notify()
            # A failed transfer must not turn the clock off: next minute retries.
            result = self._enqueue("clock", None, True) if enabled else None
        if result is not None:
            await asyncio.shield(result)

    async def async_draw(self, layout: dict[str, Any], *, force: bool = False,
                         auto_update: bool = True) -> dict:
        layout = self._validate_layout(layout)
        async with self._intent_lock:
            self._stop_clock()
            self._stop_watching()
            self.last_layout = deepcopy(layout)
            self.auto_update = auto_update
            await self._save()
            self._watch_layout()
            result = self._enqueue("layout", layout, force)
        return await asyncio.shield(result)

    async def async_draw_raw(self, raw: bytes) -> dict:
        if len(raw) != 4096:
            raise HomeAssistantError("Expected a 4096-byte framebuffer")
        async with self._intent_lock:
            self._stop_clock()
            self._stop_watching()
            self.last_layout = None
            await self._save()
            result = self._enqueue("raw", raw, True)
        return await asyncio.shield(result)

    async def async_refresh(self) -> dict:
        if self.clock_enabled:
            result = self._enqueue("clock", None, True)
        elif self.last_layout is not None:
            result = self._enqueue("layout", deepcopy(self.last_layout), True)
        elif self._last_frame is not None:
            result = self._enqueue("raw", self._last_frame, True)
        else:
            result = self._enqueue("clock", None, True)
        return await asyncio.shield(result)

    @callback
    def _enqueue(self, mode: str, content, force: bool, *, check_only: bool = False) -> asyncio.Future:
        if self._closed:
            raise HomeAssistantError("The display integration is unloading")
        result = self.hass.loop.create_future()
        # Timer-initiated requests have no waiter. Consume exceptions in either
        # case; an awaiting service caller still receives the same exception.
        result.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        if self._pending is not None:
            self._resolve(self._pending, {"status": "superseded"})
        else:
            self._pending_since = self.hass.loop.time()
        self._pending = DrawRequest(mode, content, force, result, check_only)
        self._queue_changed.set()
        if self.status != "sending":
            self.status = "queued"
        self._notify()
        if self._worker is None or self._worker.done():
            self._worker = self.hass.async_create_background_task(
                self._run_queue(), f"GTag draw {self.address}",
            )
        return result

    @staticmethod
    def _resolve(request: DrawRequest, result: dict | Exception) -> None:
        if request.result.done():
            return
        if isinstance(result, Exception):
            request.result.set_exception(result)
        else:
            request.result.set_result(result)

    async def _render(self, request: DrawRequest) -> RenderedFrame:
        if request.mode == "raw":
            return await self.hass.async_add_executor_job(from_raw, request.content)
        layout = clock_layout(dt_util.now()) if request.mode == "clock" else deepcopy(request.content)
        return await async_render_layout(self.hass, layout)

    async def _run_queue(self) -> None:
        try:
            while self._pending is not None and not self._closed:
                now = self.hass.loop.time()
                delay = max(0, self._pending_since + COALESCE_SECONDS - now,
                            self._last_attempt_end + (MIN_UPDATE_INTERVAL if self._pending.check_only
                                                      else self.update_interval) - now)
                self._queue_changed.clear()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._queue_changed.wait(), delay)
                    except TimeoutError:
                        pass
                    else:
                        continue  # Recalculate after newer content or interval settings.
                else:
                    await asyncio.sleep(0)
                request, self._pending = self._pending, None
                if request is None:
                    continue
                attempted = False
                self._last_check = self.hass.loop.time()
                try:
                    frame = await self._render(request)
                    if (not request.force and frame.raw == self._last_frame
                            and (self.confirmed_timeout or
                                 self.hass.loop.time() - self._last_success_time < UNCHANGED_MAX_AGE)):
                        await self._confirm_unchanged()
                        self.last_error = None
                        self.status = "unchanged"
                        self._resolve(request, {"status": "unchanged"})
                        continue
                    if request.check_only and self.hass.loop.time() < self._last_attempt_end + self.update_interval:
                        self.status = "sent" if self.last_success else "idle"
                        self._resolve(request, {"status": "deferred"})
                        continue
                    attempted = True
                    confirmation_start = self.hass.loop.time()
                    sent_timeout = self.freshness_timeout
                    self.status = "sending"
                    self._notify()
                    if self.zigbee is not None:
                        report_attributes = await self.zigbee.async_send(frame.raw, sent_timeout)
                    else:
                        prepared = await self.hass.async_add_executor_job(PreparedFrame.prepare, frame.raw)
                        async with get_operation_lock(self.hass, self.address):
                            sender = FrameSender(lambda: connect(self.hass, self.address), self.address,
                                                 freshness_timeout=sent_timeout)
                            try:
                                report = await sender.send_prepared(prepared)
                            finally:
                                if sender.report is not None and sender.report.firmware_info is not None:
                                    self.firmware_info = sender.report.firmware_info.attributes()
                        report_attributes = {"transport": TRANSPORT_BLE, **report.attributes()}
                    # Update preview only after the transport confirms this frame.
                    self.preview = frame.png
                    self._last_frame = frame.raw
                    self.last_success = dt_util.utcnow()
                    self._store.async_delay_save(self._saved_data, 30)
                    self._last_success_time = self.hass.loop.time()
                    self._confirmed_at = confirmation_start
                    self.confirmed_timeout = report_attributes.get("freshness_timeout", sent_timeout)
                    self.last_confirmation = self.last_success
                    self._freshness_sequence = 0
                    self.report = report_attributes
                    self.last_error = None
                    self.status = "sent"
                    self._resolve(request, {"status": "sent", **self.report})
                except asyncio.CancelledError:
                    self._resolve(request, HomeAssistantError("Display update cancelled during unload"))
                    raise
                except Exception as err:
                    self._last_frame = None
                    self.last_error = str(err)
                    self.status = "error"
                    _LOGGER.error("%s: display update failed: %s", self.address, err)
                    self._resolve(request, HomeAssistantError(str(err)))
                finally:
                    if attempted:
                        self._last_attempt_end = self.hass.loop.time()
                    if self._pending is not None:
                        self.status = "queued"
                    self._notify()
        finally:
            self._worker = None

    async def async_close(self) -> None:
        if self._closed:
            return
        saved_clock_enabled = self.clock_enabled
        self._closed = True
        await self.connection_check.async_close()
        if self._freshness_unsubscribe is not None:
            self._freshness_unsubscribe()
            self._freshness_unsubscribe = None
        await self.battery.async_close()
        self._stop_watching()
        self._stop_clock()
        if self._pending is not None:
            self._resolve(self._pending, HomeAssistantError("Display integration unloaded"))
            self._pending = None
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        if self.last_success is not None:
            # Flush/cancel the delayed write before a new Display instance loads
            # the same store. An old timer must never overwrite new settings.
            await self._store.async_save({**self._saved_data(), "clock_enabled": saved_clock_enabled})
        self._listeners.clear()
