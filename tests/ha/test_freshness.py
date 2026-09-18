"""Freshness follows the intended image, not just radio connectivity."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.gtag_ble_test import display as module
from custom_components.gtag_ble_test.binary_sensor import DisplayStale


def layout(text):
    return {"elements": [{"type": "text", "x": 8, "y": 8, "text": text, "size": 24}]}


async def test_unchanged_renews_exact_frame_without_resending_or_changing_preview(display, sent, monkeypatch):
    renew = AsyncMock()
    monkeypatch.setattr(module, "renew_freshness", renew)
    await display.async_draw(layout("Constant"))
    preview, update = display.preview, display.last_success
    display._confirmed_at -= 901
    assert display.stale is True
    await display.async_draw(layout("Constant"))
    renew.assert_awaited_once_with(display.hass, display.address, int(display.report["frame_id"], 16),
                                  int(display.report["raw_crc32"], 16), 1)
    assert len(sent) == 1 and display.stale is False
    assert display.preview == preview and display.last_success == update
    assert display.last_confirmation is not None
    await display.async_draw(layout("Constant"))
    assert renew.await_count == 1, 'Entity updates must not flood the radio with heartbeats'


async def test_failed_confirmation_stays_stale_then_retransmits_after_device_reset(display, sent, monkeypatch):
    await display.async_draw(layout("Constant"))
    display._confirmed_at -= 901
    previous = display._confirmed_at
    monkeypatch.setattr(module, "renew_freshness", AsyncMock(side_effect=HomeAssistantError("session lost")))
    with pytest.raises(HomeAssistantError):
        await display.async_draw(layout("Constant"))
    assert display.stale is True and display._confirmed_at == previous
    assert len(sent) == 1
    await display.async_draw(layout("Constant"))
    assert len(sent) == 2 and display.stale is False


async def test_changed_or_unrenderable_content_never_renews_old_image(display, sent, monkeypatch):
    await display.async_draw(layout("Old"))
    display._confirmed_at -= 901
    renew = AsyncMock()
    monkeypatch.setattr(module, "renew_freshness", renew)
    monkeypatch.setattr(module.FrameSender, "send_prepared", AsyncMock(side_effect=HomeAssistantError("offline")))
    with pytest.raises(HomeAssistantError):
        await display.async_draw(layout("New"))
    assert display.stale is True
    renew.assert_not_called()
    monkeypatch.setattr(display, "_render", AsyncMock(side_effect=HomeAssistantError("template error")))
    with pytest.raises(HomeAssistantError):
        await display.async_draw(layout("Old"))
    renew.assert_not_called()
    assert display.stale is True


async def test_periodic_confirmation_of_static_layout_and_timer_cleanup(display, hass, sent, monkeypatch):
    renew = AsyncMock()
    monkeypatch.setattr(module, "renew_freshness", renew)
    await display.async_draw(layout("Constant"))
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    count = len(sent)
    display._confirmed_at -= 901
    display._last_check -= 901
    display._configured_interval = 3600  # Heartbeats bypass the frame rate limit.
    display._on_freshness_tick(dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert renew.await_count == 1 and len(sent) == count
    assert display.stale is False
    assert display._freshness_unsubscribe is not None
    await display.async_close()
    assert display._freshness_unsubscribe is None


async def test_disabled_and_manually_updated_screens(display, hass, sent, monkeypatch):
    renew = AsyncMock()
    monkeypatch.setattr(module, "renew_freshness", renew)
    await display.async_draw(layout("Manual"), auto_update=False)
    display._confirmed_at -= 901
    display._on_freshness_tick(dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.stale is True and len(sent) == 1
    renew.assert_not_called()
    display.freshness_timeout = 0
    assert DisplayStale(display).is_on is True, 'Unsent settings cannot disable the physical timer'
    await display.async_draw(layout("Manual"), force=True)
    assert DisplayStale(display).is_on is False
    assert DisplayStale(display).extra_state_attributes["timeout_seconds"] == 0


async def test_new_screen_freshness_unknown_until_a_successful_transfer(display):
    assert display.stale is None
    assert DisplayStale(display).is_on is None


async def test_settings_changed_during_transfer_do_not_change_confirmed_timeout(display, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    original = module.FrameSender.send_prepared

    async def delayed(sender, prepared):
        assert sender._freshness_timeout == 900
        started.set()
        await finish.wait()
        return await original(sender, prepared)

    monkeypatch.setattr(module.FrameSender, "send_prepared", delayed)
    task = asyncio.create_task(display.async_draw(layout("Constant")))
    await started.wait()
    display.freshness_timeout = 0
    finish.set()
    await task
    assert display.confirmed_timeout == 900
    display._confirmed_at -= 901
    assert display.stale is True
