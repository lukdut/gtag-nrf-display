import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.script import Script
from homeassistant.helpers.service import async_get_all_descriptions
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gtag_ble_test import display as display_module
from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.display import Display
from custom_components.gtag_ble_test.render import clock_layout, render_layout


def layout(text):
    return {"elements": [{"type": "text", "x": 8, "y": 8, "text": text, "size": 24}]}


async def test_draw_duplicate_and_force(display, sent):
    result = await display.async_draw(layout("Привет"))
    assert result["status"] == "sent"
    preview, updated = display.preview, display.last_success
    assert (await display.async_draw(layout("Привет")))["status"] == "unchanged"
    assert len(sent) == 1
    assert display.preview == preview and display.last_success == updated
    assert (await display.async_draw(layout("Привет"), force=True))["status"] == "sent"
    assert len(sent) == 2


async def test_failed_transfer_keeps_successful_preview(display, sent, monkeypatch):
    await display.async_draw(layout("Первый"))
    preview, updated = display.preview, display.last_success
    monkeypatch.setattr(display_module.FrameSender, "send_prepared",
                        AsyncMock(side_effect=HomeAssistantError("BLE unavailable")))
    with pytest.raises(HomeAssistantError, match="BLE unavailable"):
        await display.async_draw(layout("Второй"))
    assert display.status == "error"
    assert display.last_error == "BLE unavailable"
    assert display.preview == preview and display.last_success == updated


async def test_pending_updates_are_combined_and_rate_limited(display, sent, monkeypatch):
    monkeypatch.setattr(display_module, "COALESCE_SECONDS", 0.03)
    monkeypatch.setattr(display_module, "MIN_UPDATE_INTERVAL", 0.06)
    results = await asyncio.gather(*(display.async_draw(layout(text)) for text in ("A", "B", "C")))
    assert [result["status"] for result in results] == ["superseded", "superseded", "sent"]
    assert sent == [render_layout(layout("C")).raw]
    last_finished = display.hass.loop.time()
    await display.async_draw(layout("D"))
    assert display.hass.loop.time() - last_finished >= 0.05


async def test_templates_follow_state_changes(display, hass, sent):
    hass.states.async_set("sensor.gtag_demo", "21")
    await display.async_draw(layout("Температура {{ states('sensor.gtag_demo') }}"))
    assert sent[-1] == render_layout(layout("Температура 21")).raw
    hass.states.async_set("sensor.gtag_demo", "22")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] == render_layout(layout("Температура 22")).raw
    assert len(sent) == 2
    await display.async_draw(layout("Статичный экран"))
    hass.states.async_set("sensor.gtag_demo", "23")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 3


async def test_auto_update_can_be_disabled(display, hass, sent):
    hass.states.async_set("sensor.gtag_demo", "1")
    await display.async_draw(layout("{{ states('sensor.gtag_demo') }}"), auto_update=False)
    hass.states.async_set("sensor.gtag_demo", "2")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 1
    await display.async_refresh()
    assert sent[-1] == render_layout(layout("2")).raw


async def test_clock_tracks_ha_time_and_stops_after_custom_draw(display, hass, sent, freezer):
    freezer.move_to("2026-09-17T12:35:10+00:00")
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Moscow"))
    await display.async_set_clock(True)
    assert sent[-1] == render_layout(clock_layout(dt_util.now())).raw
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 2
    assert sent[-1] == render_layout(clock_layout(dt_util.now())).raw
    await display.async_draw(layout("Своя страница"))
    assert not display.clock_enabled
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 3


async def test_saved_layout_and_clock_restore(display, hass, entry, sent):
    await display.async_draw(layout("Сохранено"), auto_update=False)
    restored = Display(hass, entry)
    await restored.async_load()
    assert restored.last_layout == display.last_layout
    assert not restored.auto_update
    restored.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 2
    await restored.async_set_clock(True)
    await restored.async_close()
    another = Display(hass, entry)
    await another.async_load()
    assert another.clock_enabled
    await another.async_close()


async def test_unload_cancels_active_and_pending_requests(display, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever(_sender, _prepared):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(display_module.FrameSender, "send_prepared", wait_forever)
    active = asyncio.create_task(display.async_draw(layout("A")))
    await started.wait()
    pending = asyncio.create_task(display.async_draw(layout("B")))
    await asyncio.sleep(0)
    await display.async_close()
    results = await asyncio.gather(active, pending, return_exceptions=True)
    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert cancelled.is_set()
    assert display._worker is None


async def test_full_integration_entities_and_draw_action(hass, entry, sent):
    # The physical adapter is the boundary; the HA integration, platforms,
    # entity registry, service registry and template engine are real.
    hass.config.components.add("bluetooth")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    descriptions = await async_get_all_descriptions(hass)
    assert descriptions[DOMAIN]["draw"]["fields"]["device_id"]["required"]
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 7
    image_id = registry.async_get_entity_id("image", DOMAIN, f"{entry.data['address']}_preview")
    assert hass.states.get(image_id).state == "unavailable"
    device = dr.async_get(hass).async_get(entities[0].device_id)
    result = await hass.services.async_call(
        DOMAIN, "draw", {"device_id": device.id, **layout("Тест HA")},
        blocking=True, return_response=True,
    )
    assert result["status"] == "sent"
    assert hass.states.get(image_id).state != "unavailable"
    assert sent[-1] == render_layout(layout("Тест HA")).raw
    image = hass.data["image"].get_entity(image_id)
    assert await image.async_image() == entry.runtime_data.preview
    # HA action sequences render templates before passing service data. A raw
    # block preserves the inner template for our persistent state tracker.
    hass.states.async_set("sensor.gtag_demo", "5")
    script = Script(hass, cv.SCRIPT_SCHEMA([{
        "action": f"{DOMAIN}.draw",
        "data": {
            "device_id": device.id,
            **layout("{% raw %}{{ states('sensor.gtag_demo') }}{% endraw %}"),
        },
    }]), "GTag template example", DOMAIN)
    await script.async_run(context=Context())
    assert sent[-1] == render_layout(layout("5")).raw
    hass.states.async_set("sensor.gtag_demo", "6")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] == render_layout(layout("6")).raw
    # Ordinary action templates may yield a native int, not a string.
    script = Script(hass, cv.SCRIPT_SCHEMA([{
        "action": f"{DOMAIN}.draw",
        "data": {
            "device_id": device.id,
            **layout("{{ states('sensor.gtag_demo') | int + 1 }}"),
            "auto_update": False,
        },
    }]), "GTag numeric value", DOMAIN)
    await script.async_run(context=Context())
    assert sent[-1] == render_layout(layout("7")).raw
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "draw", {"device_id": "missing", **layout("Тест")},
            blocking=True, return_response=True,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert getattr(entry, "runtime_data", None) is None
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN, "draw", {"device_id": device.id, **layout("Тест")},
            blocking=True, return_response=True,
        )


async def test_invalid_draw_preserves_saved_layout(display, sent):
    await display.async_draw(layout("Рабочий экран"))
    saved = display.last_layout
    with pytest.raises(HomeAssistantError):
        await display.async_draw(layout("{{ broken"))
    with pytest.raises(vol.Invalid):
        await display.async_draw({"elements": [
            {"type": "rectangle", "x": 20, "y": 20, "x2": 10, "y2": 10},
        ]})
    assert display.last_layout == saved
    assert (await display._store.async_load())["layout"] == saved
    assert len(sent) == 1


@pytest.mark.parametrize("saved", [
    ["invalid settings"],
    {"layout": {"elements": [{"type": "unknown"}]}},
    {"layout": layout("{{ broken")},
])
async def test_invalid_storage_does_not_prevent_loading(display, saved):
    await display._store.async_save(saved)
    await display.async_load()
    assert display.last_layout is None
