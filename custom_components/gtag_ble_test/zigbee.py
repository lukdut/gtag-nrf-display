"""Zigbee2MQTT discovery and confirmed framebuffer delivery through HA's MQTT client."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from contextlib import suppress
import json
import math
import re
import secrets
import zlib

from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .firmware_info import FirmwareInfo

DISCOVERY_TIMEOUT = 10
TRANSFER_TIMEOUT = 240
DEFAULT_BASE_TOPIC = "zigbee2mqtt"
NO_BATTERY_MODEL = "GTag_Display_Frame_NoBat"


def valid_base_topic(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip("/ ")
            or any(char in value for char in ("+", "#", "\0"))
            or len(value.encode("utf-8")) > 512):
        raise ValueError("Invalid Zigbee2MQTT base topic")
    return value


def devices_from_payload(payload: str) -> dict[str, dict]:
    """Accept only the frame-capable GTag model, indexed by immutable IEEE address."""
    try:
        values = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    if not isinstance(values, list):
        return {}
    devices = {}
    for item in values:
        if not isinstance(item, dict) or item.get("model_id") not in ("GTag_Display_Frame_V1", NO_BATTERY_MODEL):
            continue
        ieee = item.get("ieee_address", "")
        name = item.get("friendly_name")
        if (not isinstance(ieee, str) or not re.fullmatch(r"0x[0-9a-fA-F]{16}", ieee)
                or not isinstance(name, str) or not name or any(c in name for c in "+#\0")):
            continue
        definition = item.get("definition") or {}
        exposes = definition.get("exposes", []) if isinstance(definition, dict) else []
        properties = {e["property"] for e in exposes
                      if isinstance(e, dict) and isinstance(e.get("property"), str)} if isinstance(exposes, list) else set()
        devices[ieee.lower()] = {"friendly_name": name, "compatible": "frame_request_id" in properties,
                                 "battery_supported": item["model_id"] != NO_BATTERY_MODEL}
    return devices


async def async_discover(hass, base_topic: str) -> dict[str, dict]:
    """Read the retained inventory; no radio command or pairing is needed."""
    result = hass.loop.create_future()

    @callback
    def receive(message):
        if not result.done():
            result.set_result(devices_from_payload(message.payload))

    unsubscribe = await mqtt.async_subscribe(hass, f"{valid_base_topic(base_topic)}/bridge/devices", receive)
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            return await result
    finally:
        unsubscribe()


def _json_object(payload) -> dict:
    try:
        value = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _online(payload) -> bool | None:
    value = _json_object(payload).get("state", payload)
    return True if value == "online" else False if value == "offline" else None


class ZigbeeTransport:
    """One subscription set per screen, also supplying passive battery readings.

    Only a matching request ID plus raw CRC and LCD confirmation can complete
    a draw. MQTT retained/cached messages and other senders cannot confirm it.
    Commands use QoS 0 without retain so reconnects do not replay old pictures.
    """

    def __init__(self, hass, entry, notify: Callable[[], None], recovered: Callable[[], None] | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.address = entry.data["address"]
        self.base_topic = valid_base_topic(entry.data["base_topic"])
        self.friendly_name = entry.data["friendly_name"]
        self._notify = notify
        self._recovered = recovered
        self._voltage: float | None = None
        self.last_read = None
        self.supported: bool | None = entry.data.get("battery_supported")
        self._battery_error: str | None = None
        self._connected = False
        self._bridge_online: bool | None = None
        self._device_online: bool | None = None
        self._present = True
        self._closed = False
        self._unsubscribers: list[Callable] = []
        self._device_unsubscribers: list[Callable] = []
        self._rename_task: asyncio.Task | None = None
        self._pending: asyncio.Future | None = None
        self._request_id: str | None = None
        self._confirming = False
        self._lock = asyncio.Lock()
        self.firmware_info: dict = {}

    @property
    def available(self) -> bool:
        return (not self._closed and self._present and self._connected
                and self._bridge_online is not False and self._device_online is not False)

    @property
    def voltage(self) -> float | None:
        return self._voltage if self.available else None

    @property
    def last_error(self) -> str | None:
        return self._battery_error if self.available else "zigbee_or_mqtt_offline"

    async def async_setup(self) -> None:
        self._connected = mqtt.is_connected(self.hass)
        self._unsubscribers.append(mqtt.async_subscribe_connection_status(self.hass, self._connection_changed))
        await self._subscribe_device(self.friendly_name)
        self._unsubscribers.append(await mqtt.async_subscribe(
            self.hass, f"{self.base_topic}/bridge/state", self._bridge_state))
        self._unsubscribers.append(await mqtt.async_subscribe(
            self.hass, f"{self.base_topic}/bridge/devices", self._inventory))

    @callback
    def async_start(self) -> None:
        # Battery reports arrive through MQTT. No extra radio polling.
        pass

    @callback
    def _fail_pending(self, message: str) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(HomeAssistantError(message))

    @callback
    def _connection_changed(self, connected: bool) -> None:
        was_available = self.available
        self._connected = connected
        if not connected:
            self._fail_pending("MQTT disconnected during display transfer")
        self._notify_link(was_available)

    @callback
    def _notify_link(self, was_available: bool) -> None:
        self._notify()
        if not was_available and self.available and self._recovered is not None:
            self._recovered()

    @callback
    def _bridge_state(self, message) -> None:
        was_available = self.available
        self._bridge_online = _online(message.payload)
        if self._bridge_online is False:
            self._fail_pending("Zigbee2MQTT is offline")
        self._notify_link(was_available)

    @callback
    def _availability(self, message) -> None:
        was_available = self.available
        self._device_online = _online(message.payload)
        if self._device_online is False:
            self._fail_pending("Zigbee display is offline")
        self._notify_link(was_available)

    @callback
    def _inventory(self, message) -> None:
        # Ignore malformed inventories instead of treating them as device removal.
        try:
            if not isinstance(json.loads(message.payload), list):
                return
        except (ValueError, TypeError):
            return
        device = devices_from_payload(message.payload).get(self.address)
        was_available = self.available
        self._present = device is not None
        if device is not None:
            self.supported = device["battery_supported"]
            if not self.supported:
                self._voltage = None
                self._battery_error = None
                self.last_read = None
        if device is None:
            self._fail_pending("GTag is missing from Zigbee2MQTT")
            self._notify()
            return
        self._notify_link(was_available)
        if device["friendly_name"] != self.friendly_name:
            if self._rename_task is not None:
                self._rename_task.cancel()
            self._rename_task = self.hass.async_create_background_task(
                self._subscribe_device(device["friendly_name"]), f"GTag rename {self.address}")

    async def _subscribe_device(self, name: str) -> None:
        new_subscriptions = []
        try:
            new_subscriptions.append(await mqtt.async_subscribe(self.hass, f"{self.base_topic}/{name}", self._state))
            new_subscriptions.append(await mqtt.async_subscribe(
                self.hass, f"{self.base_topic}/{name}/availability", self._availability))
        except BaseException:
            for unsubscribe in new_subscriptions:
                unsubscribe()
            raise
        for unsubscribe in self._device_unsubscribers:
            unsubscribe()
        self._device_unsubscribers = new_subscriptions
        self.friendly_name = name
        if self.entry.data["friendly_name"] != name:
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, "friendly_name": name})

    @callback
    def _state(self, message) -> None:
        state = _json_object(message.payload)
        if "firmware_capabilities" in state:
            try:
                value = state["firmware_capabilities"]
                info = FirmwareInfo.from_dict(json.loads(value) if isinstance(value, str) else value)
                self.firmware_info = info.attributes()
            except (ValueError, TypeError):
                self.firmware_info = {}
        if self.supported is not False and "battery_voltage_1" in state:
            value = state["battery_voltage_1"]
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and 0 <= value <= 6)
            self._voltage = float(value) if valid else None
            self.supported = True
            self._battery_error = None if valid else "measurement_unavailable"
            seen = state.get("last_seen")
            if isinstance(seen, str):
                self.last_read = dt_util.parse_datetime(seen)
            elif isinstance(seen, (int, float)) and not isinstance(seen, bool):
                with suppress(ValueError, OverflowError, OSError):
                    self.last_read = dt_util.utc_from_timestamp(seen / 1000 if seen > 1e11 else seen)
        pending = self._pending
        if (not getattr(message, "retain", False) and pending is not None and not pending.done()
                and state.get("freshness_request_id" if self._confirming else "frame_request_id") == self._request_id):
            if self._confirming:
                if state.get("freshness_status") == "error":
                    pending.set_exception(HomeAssistantError(str(state.get("freshness_error") or "Freshness confirmation failed")))
                elif state.get("freshness_status") == "confirmed":
                    pending.set_result(state)
            elif state.get("frame_status") == "error":
                pending.set_exception(HomeAssistantError(str(state.get("frame_error") or "Zigbee frame transfer failed")))
            elif state.get("frame_status") == "displayed":
                pending.set_result(state)
        self._notify()

    async def async_send(self, raw: bytes, freshness_timeout: int = 0) -> dict:
        if len(raw) != 4096:
            raise ValueError("Expected 4096 framebuffer bytes")
        async with self._lock:
            if self._closed or not self.available:
                raise HomeAssistantError("Zigbee display or MQTT is offline")
            request_id = secrets.token_hex(16)
            crc = f"{zlib.crc32(raw):08x}"
            self._request_id = request_id
            pending = self._pending = self.hass.loop.create_future()
            started = self.hass.loop.time()
            try:
                async with asyncio.timeout(TRANSFER_TIMEOUT):
                    await mqtt.async_publish(self.hass, f"{self.base_topic}/{self.address}/set", json.dumps({
                        "frame": {"data": base64.b64encode(raw).decode("ascii"), "request_id": request_id,
                                  "freshness_timeout": freshness_timeout},
                    }), qos=0, retain=False)
                    result = await pending
                if str(result.get("frame_crc32", "")).lower() != crc:
                    raise HomeAssistantError("Zigbee framebuffer CRC confirmation mismatch")
                effective_timeout = freshness_timeout
                info_attributes = {}
                if "firmware_capabilities" in result:
                    try:
                        value = result["firmware_capabilities"]
                        info = FirmwareInfo.from_dict(json.loads(value) if isinstance(value, str) else value)
                        info.validate_transfer()
                    except (ValueError, TypeError) as err:
                        raise HomeAssistantError(f"Invalid firmware capabilities: {err}") from err
                    info_attributes = info.attributes()
                    if not info.freshness:
                        effective_timeout = 0
                if freshness_timeout and result.get("frame_freshness_timeout") != effective_timeout:
                    raise HomeAssistantError("Update the Zigbee2MQTT converter: freshness timeout was not confirmed")
                return {
                    **info_attributes, "freshness_timeout": effective_timeout,
                    "codec": result.get("frame_codec"),
                    "transport": "zigbee", "transport_mode": "zigbee2mqtt",
                    "request_id": request_id, "frame_id": result.get("frame_id"), "raw_crc32": crc,
                    "raw_size": 4096, "encoded_size": result.get("frame_bytes"),
                    "operation_retries": result.get("frame_retries"),
                    "duration_seconds": round(self.hass.loop.time() - started, 2),
                    "receiver_status": "displayed", "frame_transfer_ms": result.get("frame_transfer_ms"),
                }
            except TimeoutError as err:
                raise HomeAssistantError("No matching display confirmation from Zigbee2MQTT; check the converter and radio connection") from err
            finally:
                if not pending.done():
                    pending.cancel()
                elif not pending.cancelled():
                    pending.exception()
                self._pending = None
                self._request_id = None

    async def async_confirm(self, frame_id: int, crc: int, sequence: int) -> None:
        async with self._lock:
            if self._closed or not self.available:
                raise HomeAssistantError("Zigbee display or MQTT is offline")
            self._confirming = True
            self._request_id = secrets.token_hex(16)
            pending = self._pending = self.hass.loop.create_future()
            try:
                async with asyncio.timeout(30):
                    await mqtt.async_publish(self.hass, f"{self.base_topic}/{self.address}/set", json.dumps({
                        "frame_freshness": {"frame_id": frame_id, "crc": crc, "sequence": sequence,
                                            "request_id": self._request_id},
                    }), qos=0, retain=False)
                    result = await pending
                if (result.get("freshness_frame_id") != frame_id or result.get("freshness_sequence") != sequence
                        or str(result.get("freshness_crc32", "")).lower() != f"{crc:08x}"):
                    raise HomeAssistantError("Freshness confirmation does not match the displayed frame")
            finally:
                if not pending.done():
                    pending.cancel()
                elif not pending.cancelled():
                    pending.exception()
                self._pending = None
                self._request_id = None
                self._confirming = False

    async def async_close(self) -> None:
        self._closed = True
        self._fail_pending("Display integration unloaded")
        if self._rename_task is not None:
            self._rename_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._rename_task
        for unsubscribe in self._device_unsubscribers + self._unsubscribers:
            unsubscribe()
        self._device_unsubscribers.clear()
        self._unsubscribers.clear()
