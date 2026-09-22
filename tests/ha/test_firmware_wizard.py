"""Exercise the preparation flow and real authenticated YAML downloads."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from homeassistant.setup import async_setup_component

from custom_components.gtag_ble_test import config_flow
from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.firmware_config import (
    FirmwareConfigError, hardware_defaults, render_firmware_yaml,
)


async def start(hass, *, board="promicro", transport="zigbee", name="gtag-kitchen"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "firmware"})
    assert result["step_id"] == "firmware"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "board": board, "transport": transport, "name": name, "friendly_name": "Экран кухни",
    })
    return result


async def prepare(hass, *, board="promicro", transport="zigbee", battery=False, name="gtag-kitchen"):
    result = await start(hass, board=board, transport=transport, name=name)
    assert result["step_id"] == "firmware_pins"
    settings = hardware_defaults(board)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        **{key: value for key, value in settings.items() if key in ("dio_pin", "clk_pin", "cs_pin", "reset_pin")},
        "battery_enabled": battery,
    })
    if battery:
        assert result["step_id"] == "firmware_battery"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            key: settings[key] for key in ("battery_pin", "calibration", "empty_voltage", "full_voltage", "recovery_voltage", "indicator")
        })
    assert result["step_id"] == "firmware_download"
    assert not result["errors"]
    return result


@pytest.mark.parametrize("board,transport,battery", [
    ("promicro", "ble", True), ("promicro", "ble", False),
    ("promicro", "zigbee", True), ("promicro", "zigbee", False),
    ("super52840", "zigbee", False),
])
async def test_prepare_download_without_radio_or_config_entry(hass, hass_client_no_auth, monkeypatch, board, transport, battery):
    radio_setup = AsyncMock(side_effect=AssertionError("Preparation must not need radio or MQTT"))
    monkeypatch.setattr(config_flow, "async_setup_component", radio_setup)
    result = await prepare(hass, board=board, transport=transport, battery=battery)
    placeholders = result["description_placeholders"]
    client = await hass_client_no_auth()
    response = await client.get(placeholders["download_url"])
    assert response.status == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename="gtag-kitchen.yaml"'
    assert response.headers["Cache-Control"] == "no-store"
    text = await response.text()
    assert text == placeholders["yaml"]
    tag = "v1.1.1" if transport == "zigbee" else "v0.9.0"
    assert f"/{transport}.yaml@{tag}" in text
    assert "Экран кухни" in text
    assert ("zigbee-no-battery.yaml@v1.1.1" in text) == (transport == "zigbee" and not battery)
    assert ("    pin: P0.31" in text) == battery
    assert ("sd140_v7" in text) == (board == "super52840")
    assert not hass.config_entries.async_entries(DOMAIN)
    radio_setup.assert_not_called()
    finished = await hass.config_entries.flow.async_configure(result["flow_id"], {"action": "finish"})
    assert finished["type"] == "abort" and finished["reason"] == "firmware_ready"
    assert (await client.get(finished["description_placeholders"]["download_url"])).status == 200
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_finish_before_downloading_keeps_signed_file_available(hass, hass_client_no_auth, freezer, monkeypatch):
    from aiohttp.web_log import AccessLogger
    monkeypatch.setattr(AccessLogger, "_get_local_time", staticmethod(lambda: datetime.now(timezone.utc)))
    result = await prepare(hass)
    client = await hass_client_no_auth()
    finished = await hass.config_entries.flow.async_configure(result["flow_id"], {"action": "finish"})
    assert finished["type"] == "abort"
    assert not hass.config_entries.async_entries(DOMAIN)
    url = finished["description_placeholders"]["download_url"]
    assert (await client.get(urlsplit(url).path)).status == 401
    response = await client.get(url)
    assert response.status == 200
    assert await response.text() == result["description_placeholders"]["yaml"]
    freezer.tick(timedelta(minutes=21))
    assert (await client.get(url)).status == 401


async def test_finish_does_not_lose_yaml_if_download_cannot_be_prepared(hass, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.gtag_ble_test import firmware_flow
    result = await prepare(hass)
    monkeypatch.setattr(firmware_flow, "async_prepare_download", AsyncMock(side_effect=HomeAssistantError))
    failed = await hass.config_entries.flow.async_configure(result["flow_id"], {"action": "finish"})
    assert failed["step_id"] == "firmware_download"
    assert failed["errors"] == {"base": "firmware_download_unavailable"}
    assert failed["description_placeholders"]["yaml"] == result["description_placeholders"]["yaml"]
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("translation", ["strings.json", "translations/en.json", "translations/ru.json"])
async def test_download_links_bypass_ha_navigation_on_both_screens(hass, translation):
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.anchors = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.anchors.append(dict(attrs))

    strings = json.loads((Path(config_flow.__file__).parent / translation).read_text())
    result = await prepare(hass)
    for finished in (False, True):
        if finished:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"action": "finish"})
            description = strings["config"]["abort"]["firmware_ready"]
        else:
            description = strings["config"]["step"]["firmware_download"]["description"]
        links = Links()
        links.feed(description.format(**result["description_placeholders"]))
        assert len(links.anchors) == 1
        assert links.anchors[0]["href"] == result["description_placeholders"]["download_url"]
        # HA's isNavigationClick intercepts same-origin links without a target,
        # closes the flow and routes to a dashboard instead of downloading.
        assert links.anchors[0].get("target") == "_blank"


async def test_download_auth_expiry_refresh_and_flow_isolation(hass, hass_client_no_auth, freezer, monkeypatch):
    # freezegun's localtime lacks tm_gmtoff on Python 3.14; only the access-log
    # timestamp is replaced. Real HA signing/authentication still checks expiry.
    from aiohttp.web_log import AccessLogger
    monkeypatch.setattr(AccessLogger, "_get_local_time", staticmethod(lambda: datetime.now(timezone.utc)))
    first = await prepare(hass, name="gtag-first")
    second = await prepare(hass, board="super52840", name="gtag-second")
    client = await hass_client_no_auth()
    url = first["description_placeholders"]["download_url"]
    assert (await client.get(urlsplit(url).path)).status == 401
    assert (await client.get(url.replace(first["flow_id"], second["flow_id"]))).status == 401
    response = await client.get(second["description_placeholders"]["download_url"])
    assert response.status == 200 and 'gtag_name: "gtag-second"' in await response.text()
    freezer.tick(timedelta(minutes=21))
    assert (await client.get(url)).status == 401
    refreshed = await hass.config_entries.flow.async_configure(first["flow_id"], {"action": "refresh"})
    assert (await client.get(refreshed["description_placeholders"]["download_url"])).status == 200
    hass.config_entries.flow.async_abort(first["flow_id"])
    assert (await client.get(refreshed["description_placeholders"]["download_url"])).status == 404
    hass.config_entries.flow.async_abort(second["flow_id"])


async def test_download_can_be_registered_after_http_is_running(hass, hass_client_no_auth, monkeypatch):
    assert await async_setup_component(hass, "http", {})
    # The aiohttp client bypasses HomeAssistantHTTP.start(), which deliberately
    # keeps the router mutable so integrations can register views after startup.
    monkeypatch.setattr(hass.http.app.router, "freeze", lambda: None)
    client = await hass_client_no_auth()
    result = await prepare(hass)
    response = await client.get(result["description_placeholders"]["download_url"])
    assert response.status == 200
    assert await response.text() == result["description_placeholders"]["yaml"]
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_edit_preserves_choices_and_existing_display_options(hass, entry, hass_client_no_auth):
    hass.config_entries.async_update_entry(entry, options={"screen": {"preset": "clock"}, "screen_revision": "keep"})
    previous = deepcopy(dict(entry.options))
    result = await prepare(hass, battery=True)
    client = await hass_client_no_auth()
    old_url = result["description_placeholders"]["download_url"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"action": "edit"})
    assert result["step_id"] == "firmware"
    assert (await client.get(old_url)).status == 404
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "board": "super52840", "transport": "zigbee", "name": "gtag-other", "friendly_name": "Другой экран",
    })
    suggestions = {key.schema: key.description["suggested_value"] for key in result["data_schema"].schema}
    assert suggestions["dio_pin"] == "P1.15"
    assert suggestions["battery_enabled"] is False
    assert entry.options == previous
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_pin_and_battery_errors_keep_the_wizard_on_the_field(hass):
    result = await start(hass)
    pins = {"dio_pin": "P0.11", "clk_pin": "P0.11", "cs_pin": "P1.06", "reset_pin": "P1.13", "battery_enabled": True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], pins)
    assert result["errors"] == {"clk_pin": "duplicate_gpio"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**pins, "clk_pin": "P1.04"})
    battery = {"battery_pin": "P0.11", "calibration": 1.0, "empty_voltage": 3.306, "full_voltage": 4.19, "recovery_voltage": 3.45, "indicator": True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], battery)
    assert result["errors"] == {"battery_pin": "duplicate_gpio"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**battery, "battery_pin": "P0.31", "recovery_voltage": 3.306})
    assert result["errors"] == {"recovery_voltage": "invalid_recovery_voltage"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**battery, "battery_pin": "P0.31"})
    assert result["step_id"] == "firmware_download" and not result["errors"]
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("field,value,error", [
    ("name", "../bad", "invalid_device_name"), ("name", "Bad Name", "invalid_device_name"),
    ("friendly_name", "${gtag_name}", "invalid_friendly_name"),
    ("dio_pin", "P0.18", "reserved_gpio"), ("dio_pin", "P1.16", "invalid_gpio"),
    ("dio_pin", "P0.09", "reserved_gpio"), ("battery_pin", "P1.02", "invalid_adc_gpio"),
    ("full_voltage", 3.306, "invalid_full_voltage"), ("recovery_voltage", 4.2, "invalid_recovery_voltage"),
    ("calibration", float('nan'), "invalid_settings"),
])
def test_invalid_hardware_cannot_be_rendered(field, value, error):
    settings = {"board": "promicro", "transport": "zigbee", "name": "gtag-test", "friendly_name": "Test", **hardware_defaults("promicro"), field: value}
    with pytest.raises(FirmwareConfigError) as caught:
        render_firmware_yaml(settings)
    assert caught.value.errors[field] == error


async def test_unsupported_profile_has_clear_error(hass):
    result = await start(hass, board="super52840", transport="ble")
    assert result["step_id"] == "firmware"
    assert result["errors"] == {"transport": "unsupported_profile"}
    hass.config_entries.flow.async_abort(result["flow_id"])
