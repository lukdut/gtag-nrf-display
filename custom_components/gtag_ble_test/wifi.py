"""Wi-Fi frame delivery through public actions on HA's ESPHome connection."""
from __future__ import annotations

import asyncio
import base64

from homeassistant.exceptions import HomeAssistantError

from .firmware_info import FirmwareInfo
from .frame_codec import EncodedFrame, CODEC_RAW
from .frame_protocol import FrameDescriptor, PreparedFrame

ACTIONS = ("gtag_info", "gtag_frame", "gtag_confirm")


def service_prefix(entry) -> str:
    name = entry.data.get("device_name", "")
    return name.replace("-", "_") if isinstance(name, str) else ""


def available_devices(hass) -> dict:
    return {
        entry.entry_id: entry for entry in hass.config_entries.async_entries("esphome")
        if not entry.disabled_by and entry.unique_id and service_prefix(entry)
        and all(hass.services.has_service("esphome", f"{service_prefix(entry)}_{action}") for action in ACTIONS)
    }


class WifiTransport:
    # USB-powered profile intentionally does not create a battery entity/poller.
    supported = False
    voltage = last_read = last_error = None

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.esphome_entry_id = entry.data["esphome_entry_id"]
        self.firmware_info: dict = {}
        self._lock = asyncio.Lock()

    async def _call(self, action: str, data: dict | None = None) -> dict:
        entry = self.hass.config_entries.async_get_entry(self.esphome_entry_id)
        if entry is None or entry.domain != "esphome" or entry.disabled_by:
            raise HomeAssistantError("The linked ESPHome device is missing or disabled")
        name = f"{service_prefix(entry)}_{action}"
        if not self.hass.services.has_service("esphome", name):
            raise HomeAssistantError("ESPHome is not connected or the GTag Wi-Fi firmware is missing")
        async with asyncio.timeout(20):
            response = await self.hass.services.async_call(
                "esphome", name, data or {}, blocking=True, return_response=True,
            )
        if not isinstance(response, dict):
            raise HomeAssistantError("The ESPHome action did not return a GTag response")
        return response

    async def async_check_connection(self) -> FirmwareInfo:
        async with self._lock:
            return await self._info()

    async def _info(self) -> FirmwareInfo:
        response = await self._call("gtag_info")
        try:
            if response.get("wifi_protocol") != 1:
                raise ValueError("Unsupported GTag Wi-Fi protocol")
            info = FirmwareInfo.parse(bytes.fromhex(response["info"]))
            info.validate_transfer()
        except (KeyError, TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid GTag Wi-Fi firmware information: {err}") from err
        self.firmware_info = info.attributes()
        return info

    async def async_send(self, raw: bytes, freshness_timeout: int) -> dict:
        async with self._lock:
            started = self.hass.loop.time()
            info = await self._info()
            prepared = await self.hass.async_add_executor_job(PreparedFrame.prepare, raw)
            # Capability negotiation remains valid when new codecs are added.
            if not info.codecs & (1 << prepared.descriptor.codec):
                if not info.codecs & (1 << CODEC_RAW):
                    raise HomeAssistantError("Firmware does not support the selected frame codec")
                encoded = EncodedFrame(CODEC_RAW, raw, prepared.descriptor.raw_crc32, len(raw))
                prepared = PreparedFrame(FrameDescriptor.from_encoded(encoded), raw, len(raw), "raw")
            descriptor = prepared.descriptor
            if len(prepared.payload) > min(info.max_encoded_size, info.max_chunk_size):
                raise HomeAssistantError("Frame exceeds the firmware transfer limit")
            timeout = freshness_timeout if info.freshness else 0
            frame_id, crc = f"{descriptor.frame_id:08x}", f"{descriptor.raw_crc32:08x}"
            data = {"payload": base64.b64encode(prepared.payload).decode("ascii"),
                    "version": descriptor.version, "codec": descriptor.codec,
                    "frame_id": frame_id, "crc32": crc, "freshness_timeout": timeout}
            response = await self._call("gtag_frame", data)
            if (response.get("rendered") is not True or response.get("frame_id") != frame_id
                    or response.get("crc32") != crc):
                raise HomeAssistantError("The device did not confirm rendering this frame")
            return {"transport": "wifi", "frame_id": descriptor.frame_id,
                    "raw_crc32": crc, "codec": descriptor.codec, "codec_name": prepared.codec_name,
                    "raw_size": prepared.raw_size, "encoded_size": len(prepared.payload),
                    "bytes_saved": prepared.bytes_saved, "compression_ratio": prepared.compression_ratio,
                    "freshness_timeout": timeout, "duration_seconds": round(self.hass.loop.time() - started, 2),
                    **self.firmware_info}

    async def async_confirm(self, frame_id: int, crc: int, sequence: int) -> None:
        async with self._lock:
            request = {"frame_id": f"{frame_id:08x}", "crc32": f"{crc:08x}", "sequence": f"{sequence:08x}"}
            response = await self._call("gtag_confirm", request)
            if response.get("confirmed") is not True or any(response.get(key) != value for key, value in request.items()):
                raise HomeAssistantError("The device no longer has the confirmed frame; resend required")

    def async_start(self) -> None:
        pass

    async def async_close(self) -> None:
        pass
