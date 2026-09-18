"""Exercise the real HA options flow, including preview and live updates."""
import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, translation
from homeassistant.setup import async_setup_component

from custom_components.gtag_ble_test import display as display_module
from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.display import Display
from custom_components.gtag_ble_test.layouts import (
    StaleAfterTooShort, async_render_layout, preset_layout, validate_settings,
)
from custom_components.gtag_ble_test.render import preview_svg


@pytest.fixture
async def loaded(hass, entry, sent, monkeypatch):
    hass.config.components.add("bluetooth")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # Form validation still uses real 5..3600 second limits. Exercise the
    # scheduler's timing separately; these tests focus on UI transactions.
    monkeypatch.setattr(Display, "update_interval", property(lambda self: 0))
    hass.states.async_set("sensor.temperature", "22.5", {
        "friendly_name": "Температура", "unit_of_measurement": "°C",
    })
    hass.states.async_set("sensor.humidity", "48", {
        "friendly_name": "Влажность", "unit_of_measurement": "%",
    })
    return entry


async def preview(hass, entry, preset="clock_two_values", **fields):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": preset, "update_interval": 30,
    })
    if preset != "clock":
        values = {"entity_1": "sensor.temperature"}
        if preset == "clock_two_values":
            values["entity_2"] = "sensor.humidity"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**values, **fields})
    assert result["step_id"] == "preview"
    assert not result["errors"]
    assert '<svg ' in result["description_placeholders"]["preview"]
    return result


async def apply(hass, result):
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "apply"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == "create_entry"
    return result


async def test_preview_cancel_does_not_change_running_display(hass, loaded, sent):
    display = loaded.runtime_data
    await display.async_set_clock(True)
    old_image = display.preview
    result = await preview(hass, loaded)
    assert len(sent) == 1 and display.clock_enabled
    assert display.preview == old_image and not loaded.options
    hass.config_entries.options.async_abort(result["flow_id"])
    assert display.clock_enabled and not loaded.options


async def test_apply_preset_tracks_values_attributes_and_survives_reload(hass, loaded, sent):
    result = await preview(hass, loaded)
    expected_preview = result["description_placeholders"]["preview"]
    await apply(hass, result)
    assert preview_svg(sent[-1]) == expected_preview
    assert loaded.options["screen"]["update_interval"] == 30
    assert not loaded.runtime_data.clock_enabled
    hass.states.async_set("sensor.temperature", "23", {
        "friendly_name": "Гостиная", "unit_of_measurement": "°F",
    })
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 2
    expected = await async_render_layout(hass, preset_layout(loaded.options["screen"]))
    assert sent[-1] == expected.raw
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] == expected.raw and len(sent) == 3


async def test_single_value_custom_labels_units_and_unavailable(hass, loaded, sent):
    result = await preview(hass, loaded, "single_value", label_1="Комната {{ буквально }}", unit_1="град.")
    await apply(hass, result)
    assert "Комната" in loaded.runtime_data.last_layout["elements"][0]["text"]
    assert len(sent) == 1
    hass.states.async_set("sensor.temperature", "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 2
    expected = await async_render_layout(hass, preset_layout(loaded.options["screen"]))
    assert sent[-1] == expected.raw


async def test_refresh_and_edit_preview_do_not_send(hass, loaded, sent):
    result = await preview(hass, loaded, "single_value")
    previous = result["description_placeholders"]["preview"]
    hass.states.async_set("sensor.temperature", "99")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "refresh"})
    assert result["description_placeholders"]["preview"] != previous
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"action": "edit"})
    assert result["step_id"] == "init"
    assert sent == [] and not loaded.options
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_reapplying_same_preset_replaces_manual_draw(hass, loaded, sent):
    await apply(hass, await preview(hass, loaded))
    first_revision = loaded.options["screen_revision"]
    manual = {"elements": [{"type": "text", "x": 8, "y": 8, "text": "Другая страница"}]}
    await loaded.runtime_data.async_draw(manual)
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # A later draw action remains selected after a normal HA restart.
    assert loaded.runtime_data.last_layout["elements"][0]["text"] == "Другая страница"
    await apply(hass, await preview(hass, loaded))
    assert loaded.options["screen_revision"] != first_revision
    expected = await async_render_layout(hass, preset_layout(loaded.options["screen"]))
    assert sent[-1] == expected.raw


async def test_clock_preset_and_ble_failure_keep_saved_settings(hass, loaded, sent, monkeypatch):
    await apply(hass, await preview(hass, loaded, "clock"))
    assert loaded.runtime_data.clock_enabled and len(sent) == 1
    previous = loaded.runtime_data.preview
    monkeypatch.setattr(display_module.FrameSender, "send_prepared", AsyncMock(side_effect=HomeAssistantError("offline")))
    await apply(hass, await preview(hass, loaded, "single_value"))
    assert loaded.options["screen"]["preset"] == "single_value"
    assert loaded.runtime_data.preview == previous
    assert loaded.runtime_data.status == "error"
    assert loaded.runtime_data.last_error == "offline"


async def test_invalid_entities_and_lengths_keep_options_unchanged(hass, loaded, sent):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": "single_value", "update_interval": 5,
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": "sensor.missing"})
    assert result["errors"]["entity_1"] == "entity_not_found"
    registry = er.async_get(hass)
    own_sensor = registry.async_get_entity_id("sensor", DOMAIN, f"{loaded.data['address']}_transfer")
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": own_sensor})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "entity_1": "sensor.temperature", "label_1": "X" * 81,
    })
    assert result["errors"]["label_1"] == "invalid_settings"
    assert not loaded.options and not sent
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_options_saved_while_unloaded_apply_on_next_load(hass, loaded, sent):
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await apply(hass, await preview(hass, loaded, "single_value"))
    assert not sent
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 1
    assert loaded.runtime_data.last_layout


async def test_russian_options_translations_are_loaded(hass, loaded):
    values = await translation.async_get_translations(hass, "ru", "options", {DOMAIN})
    assert values[f"component.{DOMAIN}.options.step.init.title"] == "Макет экрана"
    assert "{preview}" in values[f"component.{DOMAIN}.options.step.preview.description"]
    assert "{minimum}" in values[f"component.{DOMAIN}.options.error.stale_after_too_short"]


@pytest.mark.parametrize(("preset", "interval", "stale_after", "minimum"), [
    ("clock", 31, 1, 2),
    ("single_value", 300, 9, 10),
    ("clock_two_values", 3600, 119, 120),
])
async def test_short_freshness_timeout_can_be_corrected(
    hass, loaded, sent, preset, interval, stale_after, minimum,
):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    values = {"preset": preset, "update_interval": interval, "stale_after": stale_after}
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["step_id"] == "init"
    assert result["errors"] == {"stale_after": "stale_after_too_short"}
    assert result["description_placeholders"]["minimum"] == str(minimum)
    assert not loaded.options and not sent
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**values, "stale_after": minimum},
    )
    assert result["step_id"] == ("preview" if preset == "clock" else "values")
    assert not result["errors"]
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize(("interval", "stale_after"), [
    (30, 1), (31, 2), (300, 10), (3600, 120), (3600, 0),
])
async def test_freshness_minimum_and_disabled_setting_apply(hass, loaded, sent, interval, stale_after):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": "clock", "update_interval": interval, "stale_after": stale_after,
    })
    assert result["step_id"] == "preview" and not result["errors"]
    assert not sent
    await apply(hass, result)
    assert loaded.options["screen"]["stale_after"] == stale_after
    assert loaded.runtime_data._configured_interval == interval
    assert loaded.runtime_data.freshness_timeout == stale_after * 60
    assert loaded.runtime_data.confirmed_timeout == stale_after * 60


async def test_increasing_interval_rechecks_freshness_timeout(hass, loaded):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    values = {"preset": "clock", "update_interval": 300, "stale_after": 10}
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    await apply(hass, result)
    previous_options = dict(loaded.options)
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        **values, "update_interval": 301,
    })
    assert result["step_id"] == "init"
    assert result["errors"] == {"stale_after": "stale_after_too_short"}
    assert result["description_placeholders"]["minimum"] == "11"
    assert loaded.options == previous_options
    assert loaded.runtime_data.confirmed_timeout == 600
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("matching_revision", [True, False])
@pytest.mark.parametrize(("saved_timeout", "expected_timeout"), [(1, 10), (0, 0)])
async def test_saved_short_timeout_loads_with_safe_minimum(
    hass, loaded, matching_revision, saved_timeout, expected_timeout,
):
    await apply(hass, await preview(hass, loaded, "clock"))
    assert await hass.config_entries.async_unload(loaded.entry_id)
    revision = loaded.options["screen_revision"] if matching_revision else "legacy-revision"
    hass.config_entries.async_update_entry(loaded, options={
        "screen": {"preset": "clock", "update_interval": 300, "stale_after": saved_timeout},
        "screen_revision": revision,
    })
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded.runtime_data._configured_interval == 300
    assert loaded.runtime_data.freshness_timeout == expected_timeout * 60
    assert loaded.runtime_data.confirmed_timeout == expected_timeout * 60
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    defaults = result["data_schema"]({"preset": "clock", "update_interval": 300})
    assert defaults["stale_after"] == expected_timeout
    hass.config_entries.options.async_abort(result["flow_id"])


def test_direct_settings_validation_rejects_short_timeout():
    with pytest.raises(StaleAfterTooShort) as error:
        validate_settings({"preset": "clock", "update_interval": 300, "stale_after": 9})
    assert error.value.minimum == 10
    assert error.value.path == ["stale_after"]


async def test_interval_change_wakes_waiting_queue(display, hass, sent, monkeypatch):
    layout = {"elements": [{"type": "text", "x": 0, "y": 0, "text": "A"}]}
    await display.async_draw(layout)
    display._configured_interval = 3600
    pending = asyncio.create_task(display.async_draw({**layout, "background": "black"}))
    await asyncio.sleep(0.02)
    assert len(sent) == 1
    display._configured_interval = 0.05
    latest = asyncio.create_task(display.async_draw(layout, force=True))
    first, second = await asyncio.wait_for(asyncio.gather(pending, latest), 1)
    assert first["status"] == "superseded"
    assert second["status"] == "sent" and len(sent) == 2


async def test_continuous_updates_do_not_postpone_sending_forever(display, sent, monkeypatch):
    monkeypatch.setattr(display_module, "COALESCE_SECONDS", 0.05)
    started = asyncio.Event()
    original = display_module.FrameSender.send_prepared

    async def send(sender, frame):
        report = await original(sender, frame)
        started.set()
        return report

    monkeypatch.setattr(display_module.FrameSender, "send_prepared", send)
    requests = []

    async def updates():
        while True:
            requests.append(asyncio.create_task(display.async_draw({"elements": [
                {"type": "text", "x": 8, "y": 8, "text": str(len(requests))},
            ]})))
            await asyncio.sleep(0.005)

    producer = asyncio.create_task(updates())
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
        await asyncio.gather(*requests)
    assert sent
