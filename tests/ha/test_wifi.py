"""Exercise the real HA service registry, config flow and display queue."""
import asyncio
import base64
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.display import Display
from custom_components.gtag_ble_test.frame_codec import white_rle_v1_decode
from custom_components.gtag_ble_test.wifi import WifiTransport


@pytest.fixture
def device(hass):
    def create(name="gtag-kitchen", mac="aa:bb:cc:dd:ee:ff"):
        linked = MockConfigEntry(domain="esphome", title=name, unique_id=mac,
                                 data={"device_name": name})
        linked.add_to_hass(hass)
        calls = []
        state = {"frame": None, "crc": None, "codecs": 3}

        async def info(call):
            calls.append(call)
            packet = struct.pack('<BBBBIIHHHH', 1, 1, 1, 1, state["codecs"], 1, 256, 128, 4096, 4096)
            return {"wifi_protocol": 1, "info": (packet + b"1.1.0-beta.2".ljust(20, b'\0')).hex()}

        async def frame(call):
            calls.append(call)
            data = call.data
            payload = base64.b64decode(data["payload"], validate=True)
            state["raw"] = payload if data["codec"] == 0 else white_rle_v1_decode(payload)
            state["frame"], state["crc"] = data["frame_id"], data["crc32"]
            return {"rendered": True, "frame_id": state["frame"], "crc32": state["crc"]}

        async def confirm(call):
            calls.append(call)
            return {**call.data, "confirmed": call.data["frame_id"] == state["frame"] and call.data["crc32"] == state["crc"]}

        for suffix, handler in (("info", info), ("frame", frame), ("confirm", confirm)):
            hass.services.async_register("esphome", f"{name.replace('-', '_')}_gtag_{suffix}", handler,
                                         supports_response=SupportsResponse.ONLY)
        entry = MockConfigEntry(domain=DOMAIN, title=name, unique_id=f"wifi:{mac}",
                                data={"transport": "wifi", "address": mac, "esphome_entry_id": linked.entry_id})
        return SimpleNamespace(linked=linked, entry=entry, state=state, calls=calls,
                               transport=WifiTransport(hass, entry))
    return create


async def test_send_negotiates_codec_and_confirms_render(hass, device):
    dev = device()
    raw = b'\xff' * 4096
    report = await dev.transport.async_send(raw, 300)
    assert dev.state["raw"] == raw
    assert report["transport"] == "wifi" and report["encoded_size"] < 4096
    assert report["firmware_version"] == "1.1.0-beta.2"
    assert report["freshness_timeout"] == 300
    await dev.transport.async_confirm(report["frame_id"], int(report["raw_crc32"], 16), 1)
    dev.state["frame"] = None  # Device restarted and lost its framebuffer.
    with pytest.raises(HomeAssistantError, match="resend"):
        await dev.transport.async_confirm(report["frame_id"], int(report["raw_crc32"], 16), 2)


async def test_raw_only_firmware(hass, device):
    dev = device()
    dev.state["codecs"] = 1
    report = await dev.transport.async_send(b'\xff' * 4096, 0)
    assert report["codec"] == 0 and report["encoded_size"] == 4096


@pytest.mark.parametrize("response", [None, {}, {"rendered": False},
                                      {"rendered": True, "frame_id": "wrong", "crc32": "wrong"}])
async def test_no_success_without_matching_ack(hass, device, response):
    dev = device()
    async def bad(call):
        return response
    hass.services.async_register("esphome", "gtag_kitchen_gtag_frame", bad, supports_response=SupportsResponse.ONLY)
    with pytest.raises(HomeAssistantError):
        await dev.transport.async_send(b'\xff' * 4096, 300)


async def test_two_devices_are_independent(hass, device):
    first, second = device(), device("gtag-bedroom", "11:22:33:44:55:66")
    await asyncio.gather(first.transport.async_send(b'\xff' * 4096, 300),
                         second.transport.async_send(b'\x00' * 4096, 600))
    assert first.state["raw"] != second.state["raw"]
    hass.services.async_remove("esphome", "gtag_kitchen_gtag_info")
    with pytest.raises(HomeAssistantError):
        await first.transport.async_check_connection()
    assert (await second.transport.async_check_connection()).firmware_version == "1.1.0-beta.2"


async def test_config_flow_and_duplicate(hass, device, monkeypatch):
    import custom_components.gtag_ble_test as integration
    monkeypatch.setattr(integration, "async_setup_entry", AsyncMock(return_value=True))
    dev = device()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    assert result["step_id"] == "wifi" and not result["errors"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"esphome_entry_id": dev.linked.entry_id})
    assert result["type"] == "create_entry"
    assert result["data"]["address"] == "aa:bb:cc:dd:ee:ff"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "wifi"})
    assert result["errors"]["base"] == "no_wifi_devices"


async def test_display_preview_and_check_without_ble(hass, device, monkeypatch):
    from custom_components.gtag_ble_test import display as module
    from custom_components.gtag_ble_test import sensor
    monkeypatch.setattr(module, "COALESCE_SECONDS", 0)
    monkeypatch.setattr(module, "MIN_UPDATE_INTERVAL", 0)
    monkeypatch.setattr(module, "connect", AsyncMock(side_effect=AssertionError("Wi-Fi must not use BLE")))
    dev = device()
    dev.entry.add_to_hass(hass)
    display = Display(hass, dev.entry)
    try:
        await display.async_load()
        assert display.identity == "wifi:aa:bb:cc:dd:ee:ff"
        await display.async_draw_raw(b'\xff' * 4096)
        assert display.preview and display.last_success
        assert (await display.connection_check.async_run())["status"] == "ok"
        entities = []
        await sensor.async_setup_entry(hass, SimpleNamespace(runtime_data=display), entities.extend)
        assert not any(isinstance(entity, sensor.BatteryVoltage) for entity in entities)
        display._confirmed_at = None
        await display._confirm_unchanged()
        assert display.last_confirmation
    finally:
        await display.async_close()


async def test_wifi_wizard(hass, hass_client_no_auth):
    from custom_components.gtag_ble_test.firmware_config import hardware_defaults, WIFI_FIRMWARE_TAG
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "firmware"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "board": "esp32c3_supermini", "transport": "wifi", "name": "gtag-kitchen", "friendly_name": "Kitchen",
    })
    assert result["step_id"] == "firmware_pins"
    pins = {k: v for k, v in hardware_defaults("esp32c3_supermini").items() if k.endswith('_pin') and k != 'battery_pin'}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], pins)
    assert result["step_id"] == "firmware_wifi"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"wifi_ssid": "Test network", "wifi_password": "test-password"})
    assert result["step_id"] == "firmware_download" and not result["errors"]
    text = result["description_placeholders"]["yaml"]
    assert f"wifi.yaml@{WIFI_FIRMWARE_TAG}" in text and "GPIO0" in text
    assert "encryption:" in text and "test-password" in text
    assert "nrf52:" not in text
    client = await hass_client_no_auth()
    response = await client.get(result["description_placeholders"]["download_url"])
    assert response.status == 200 and await response.text() == text


async def test_setup_entities_device_identity_and_reload(hass, device, monkeypatch):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from homeassistant.setup import async_setup_component
    from custom_components.gtag_ble_test import display as module
    monkeypatch.setattr(module, "COALESCE_SECONDS", 0)
    monkeypatch.setattr(module, "MIN_UPDATE_INTERVAL", 0)
    # ESPHome owns the API connection; this fixture supplies its public actions.
    hass.config.components.add("esphome")
    dev = device()
    dev.entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    parent = devices.async_get_or_create(config_entry_id=dev.linked.entry_id,
                                         connections={(dr.CONNECTION_NETWORK_MAC, dev.linked.unique_id)})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    display = dev.entry.runtime_data
    assert isinstance(display, Display)
    await display.async_draw({"elements": [{"type": "text", "x": 8, "y": 8, "text": "Wi-Fi test"}]}, auto_update=False)
    await hass.async_block_till_done()
    device_info = devices.async_get_device_by_identifier((DOMAIN, display.identity), dev.entry.entry_id)
    # HA 2026.9 scopes device records to their owning config entry. Both
    # integrations retain the physical MAC without taking ownership of each other.
    assert device_info.connections == parent.connections
    assert device_info.config_entry_id == dev.entry.entry_id
    assert parent.config_entry_id == dev.linked.entry_id
    registry = er.async_get(hass)
    assert not registry.async_get_entity_id("sensor", DOMAIN, f"{display.identity}_battery_voltage")
    assert registry.async_get_entity_id("image", DOMAIN, f"{display.identity}_preview")
    assert await hass.config_entries.async_reload(dev.entry.entry_id)
    await hass.async_block_till_done()
    restored = dev.entry.runtime_data
    assert restored.last_layout == display.last_layout
    assert not restored.auto_update
    assert await hass.config_entries.async_unload(dev.entry.entry_id)
