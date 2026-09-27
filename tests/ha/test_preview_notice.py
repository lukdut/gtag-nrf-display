"""Bitmap fallback notices follow current data without blocking screen setup."""
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import translation

from custom_components.gtag_ble_test import config_flow
from custom_components.gtag_ble_test.const import DOMAIN
from test_options import loaded, preview, apply
from test_three_values import choose_sources


def notice(result):
    fields = result["data_schema"].schema
    return next((value for key, value in fields.items() if key.schema == "render_notice"), None)


@pytest.mark.parametrize("preset", ["single_value", "clock_two_values"])
async def test_notice_follows_data_refresh_and_does_not_send(hass, loaded, sent, preset):
    result = await preview(hass, loaded, preset)
    assert notice(result) is None
    # Missing glyphs require a bitmap even when the text is short.
    hass.states.async_set("sensor.temperature", "🙂")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "refresh"})
    field = notice(result)
    assert field is not None and field.config["read_only"]
    assert field.config["translation_key"] == "render_notice"
    assert not result["errors"] and not sent and not loaded.options
    hass.states.async_set("sensor.temperature", "23.4", {"unit_of_measurement": "°C"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "refresh"})
    assert notice(result) is None
    assert not sent and not loaded.options
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_large_value_notice_can_be_fixed_by_rounding(hass, loaded, sent):
    hass.states.async_set("sensor.temperature", "23.456789012345", {"unit_of_measurement": "°C"})
    result = await preview(hass, loaded, "single_value")
    assert notice(result) is not None
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "edit"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": "single_value", "update_interval": 30,
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "entity_1": "sensor.temperature", "decimals_1": "1",
    })
    assert notice(result) is None and not sent
    await apply(hass, result)
    assert loaded.options["screen"]["decimals_1"] == "1"


async def test_notice_does_not_block_apply_or_leak_into_settings(hass, loaded, sent):
    hass.states.async_set("sensor.temperature", "Недоступно")
    result = await preview(hass, loaded, "single_value")
    assert notice(result) is not None
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "action": "apply", "render_notice": "bitmap_fallback",
    })
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == "create_entry" and len(sent) == 1
    assert "render_notice" not in loaded.options["screen"]


async def test_three_fields_warn_when_entity_changes(hass, loaded, sent):
    result = await choose_sources(hass, loaded, ("entity", "text", "text"))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "entity_1": "sensor.temperature", "text_2": "48 %", "text_3": "Гостиная",
    })
    assert notice(result) is None
    hass.states.async_set("sensor.temperature", "🙂")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "refresh"})
    assert notice(result) is not None and not sent
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_clock_has_no_false_warning(hass, loaded, sent):
    result = await preview(hass, loaded, "clock")
    assert notice(result) is None and not sent
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_render_error_clears_notice(hass, loaded, monkeypatch):
    hass.states.async_set("sensor.temperature", "🙂")
    result = await preview(hass, loaded, "single_value")
    assert notice(result) is not None
    monkeypatch.setattr(config_flow, "async_render_layout", AsyncMock(side_effect=HomeAssistantError("failed")))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "refresh"})
    assert result["errors"] == {"base": "render_failed"}
    assert notice(result) is None
    assert not result["description_placeholders"]["preview"]
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("language", ["en", "ru"])
async def test_notice_translations_available_in_ha(hass, loaded, language):
    options = await translation.async_get_translations(hass, language, "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options.step.preview"
    assert "⚠️" in options[f"{prefix}.data.render_notice"]
    assert options[f"{prefix}.data_description.render_notice"]
    selectors = await translation.async_get_translations(hass, language, "selector", {DOMAIN})
    assert selectors[f"component.{DOMAIN}.selector.render_notice.options.bitmap_fallback"]
