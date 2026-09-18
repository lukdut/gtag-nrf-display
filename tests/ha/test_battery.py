"""Battery GATT protocol, scheduled reads, transport arbitration and HA entity."""
import asyncio
from datetime import timedelta

from bleak.exc import BleakCharacteristicNotFoundError, BleakError
import pytest

from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gtag_ble_test import battery as battery_module
from custom_components.gtag_ble_test.battery import parse_battery_voltage
from custom_components.gtag_ble_test.const import BATTERY_CHAR_UUID, DOMAIN
from custom_components.gtag_ble_test.transport import TransferResyncError, get_operation_lock


@pytest.mark.parametrize("raw,expected", [
    (b"h\x10", 4.2), (b"\x10\x0e", 3.6), (b"\0\0", 0), (b"\xff\xff", None),
])
def test_voltage_wire_format(raw, expected):
    assert parse_battery_voltage(raw) == expected


@pytest.mark.parametrize("raw", [b"", b"\0", b"\0\0\0", b"\xfe\xff"])
def test_invalid_voltage_packet(raw):
    with pytest.raises(ValueError):
        parse_battery_voltage(raw)


async def advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_scheduled_reads_and_unload(display, hass, battery_client):
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    battery_client.read_gatt_char.assert_awaited_once_with(BATTERY_CHAR_UUID)
    battery_client.disconnect.assert_awaited_once()
    assert display.battery.voltage == 4.2
    await advance(hass, 299)
    assert battery_client.read_gatt_char.await_count == 1
    battery_client.read_gatt_char.return_value = (3700).to_bytes(2, "little")
    await advance(hass, 301)
    assert display.battery.voltage == 3.7
    assert battery_client.read_gatt_char.await_count == 2
    await display.async_close()
    await advance(hass, 1000)
    assert battery_client.read_gatt_char.await_count == 2


@pytest.mark.parametrize("failure", [
    TransferResyncError("No connectable advertisement"), b"\xff\xff",
])
async def test_first_voltage_retries_after_boot_then_uses_normal_interval(
    display, hass, battery_client, failure,
):
    if isinstance(failure, Exception):
        battery_module.connect.side_effect = failure
    else:
        battery_client.read_gatt_char.return_value = failure
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.battery.voltage is None
    assert battery_module.connect.await_count == 1
    await advance(hass, 4)
    assert battery_module.connect.await_count == 1
    battery_module.connect.side_effect = None
    battery_client.read_gatt_char.return_value = (4100).to_bytes(2, "little")
    await advance(hass, 6)
    assert display.battery.voltage == 4.1
    assert battery_module.connect.await_count == 2
    await advance(hass, 40)
    assert battery_module.connect.await_count == 2


async def test_startup_retries_are_bounded(display, hass, battery_client, freezer):
    battery_module.connect.side_effect = TransferResyncError("No advertisement")
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    for delay in (5, 15, 30):
        freezer.tick(delay + 1)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert battery_module.connect.await_count == 4
    freezer.tick(60)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert battery_module.connect.await_count == 4
    freezer.tick(241)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert battery_module.connect.await_count == 5


async def test_waits_for_existing_transfer(display, hass, battery_client):
    lock = get_operation_lock(hass, display.address)
    async with lock:
        display.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        battery_client.read_gatt_char.assert_not_awaited()
    await advance(hass, 31)
    assert display.battery.voltage == 4.2
    assert not lock.locked()


async def test_lock_held_until_disconnect(display, hass, battery_client):
    lock = get_operation_lock(hass, display.address)
    async def disconnect():
        assert lock.locked()
    battery_client.disconnect.side_effect = disconnect
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not lock.locked()


async def test_legacy_firmware_does_not_poll_repeatedly(display, hass, battery_client):
    battery_client.read_gatt_char.side_effect = BleakCharacteristicNotFoundError(BATTERY_CHAR_UUID)
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.battery.supported is None
    await advance(hass, 6)
    assert display.battery.supported is False
    assert display.battery.voltage is None
    assert display.battery.last_error == "firmware_unsupported"
    await advance(hass, 900)
    assert battery_client.read_gatt_char.await_count == 2
    battery_client.clear_cache.assert_awaited_once()
    assert display.status == "idle"
    await display.async_draw({"elements": []})
    assert display.status == "sent"


@pytest.mark.parametrize("cache_cleared", [True, False])
async def test_firmware_upgrade_rediscovers_battery_once(
    display, hass, battery_client, cache_cleared,
):
    battery_client.read_gatt_char.side_effect = [
        BleakCharacteristicNotFoundError(BATTERY_CHAR_UUID), (3892).to_bytes(2, "little"),
        (3900).to_bytes(2, "little"),
    ]
    async def clear_cache():
        assert get_operation_lock(hass, display.address).locked()
        battery_client.disconnect.assert_not_awaited()
        return cache_cleared
    battery_client.clear_cache.side_effect = clear_cache
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.battery.last_error == "battery_services_refresh_pending"
    battery_client.disconnect.assert_awaited_once()
    await advance(hass, 6)
    assert display.battery.voltage == 3.892
    assert display.battery.supported is True
    assert display.battery.last_error is None
    await advance(hass, 307)
    assert display.battery.voltage == 3.9
    assert [call.kwargs["use_services_cache"] for call in battery_module.connect.await_args_list] == [
        True, False, True,
    ]
    battery_client.clear_cache.assert_awaited_once()


async def test_unload_cancels_cache_refresh(display, hass, battery_client):
    refreshing = asyncio.Event()
    async def clear_cache():
        refreshing.set()
        await asyncio.Event().wait()
    battery_client.clear_cache.side_effect = clear_cache
    battery_client.read_gatt_char.side_effect = BleakCharacteristicNotFoundError(BATTERY_CHAR_UUID)
    display.async_start()
    await refreshing.wait()
    await display.async_close()
    battery_client.disconnect.assert_awaited_once()
    assert not get_operation_lock(hass, display.address).locked()
    assert display.battery._cancel_timer is None


@pytest.mark.parametrize("failure", [BleakError("out of range"), b"\xff\xff", b"bad"])
async def test_failed_read_is_unavailable_and_recovers(display, hass, battery_client, failure):
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.battery.voltage == 4.2
    if isinstance(failure, Exception):
        battery_client.read_gatt_char.side_effect = failure
    else:
        battery_client.read_gatt_char.return_value = failure
    await advance(hass, 301)
    assert display.battery.voltage is None
    assert display.battery.last_error
    assert display.last_error is None  # A battery fault is not a failed frame.
    battery_client.read_gatt_char.side_effect = None
    battery_client.read_gatt_char.return_value = (4000).to_bytes(2, "little")
    await advance(hass, 602)
    assert display.battery.voltage == 4.0
    assert display.battery.last_error is None


async def test_timeout_disconnects_and_releases_lock(display, hass, battery_client, monkeypatch):
    monkeypatch.setattr(battery_module, "READ_TIMEOUT", 0.01)
    async def stalled_read(_uuid):
        await asyncio.Event().wait()
    battery_client.read_gatt_char.side_effect = stalled_read
    display.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.battery.voltage is None
    assert display.battery.last_error == "TimeoutError"
    battery_client.disconnect.assert_awaited_once()
    assert not get_operation_lock(hass, display.address).locked()
    assert display.battery._cancel_timer is not None


async def test_unload_cancels_active_read(display, hass, battery_client):
    reading = asyncio.Event()
    async def stalled_read(_uuid):
        reading.set()
        await asyncio.Event().wait()
    battery_client.read_gatt_char.side_effect = stalled_read
    display.async_start()
    await reading.wait()
    await display.async_close()
    battery_client.disconnect.assert_awaited_once()
    assert display.battery._task is None
    assert display.battery._cancel_timer is None
    assert not get_operation_lock(hass, display.address).locked()


async def test_voltage_entity_available_as_screen_source(hass, entry, sent, battery_client):
    hass.config.components.add("bluetooth")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.data['address']}_battery_voltage")
    state = hass.states.get(entity_id)
    assert state.state == "4.2"
    assert state.attributes["device_class"] == "voltage"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["unit_of_measurement"] == "V"
    assert state.attributes["firmware_supported"] is True
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "configure"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": "single_value", "update_interval": 5,
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": entity_id})
    assert result["step_id"] == "preview" and not result["errors"]
    await hass.config_entries.options.async_configure(result["flow_id"], {"action": "apply"})
    await hass.async_block_till_done(wait_background_tasks=True)
    first_frame = sent[-1]
    # A new battery value updates the selected screen without a feedback loop.
    entry.runtime_data._configured_interval = 0
    battery_client.read_gatt_char.return_value = (3900).to_bytes(2, "little")
    await advance(hass, 301)
    assert sent[-1] != first_frame
    assert len(sent) == 2
    battery_client.read_gatt_char.side_effect = BleakError("disconnected")
    await advance(hass, 602)
    assert hass.states.get(entity_id).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await advance(hass, 1200)
    assert battery_client.read_gatt_char.await_count == 3


@pytest.mark.parametrize("failure,error,supported", [
    (BleakError("out of range"), "out of range", None),
    (b"\xff\xff", "measurement_unavailable", True),
    (BleakCharacteristicNotFoundError(BATTERY_CHAR_UUID), "firmware_unsupported", False),
])
async def test_diagnostics_visible_while_voltage_unavailable(
    hass, entry, sent, battery_client, failure, error, supported,
):
    if isinstance(failure, Exception):
        battery_client.read_gatt_char.side_effect = failure
    else:
        battery_client.read_gatt_char.return_value = failure
    hass.config.components.add("bluetooth")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done(wait_background_tasks=True)
    if supported is False:
        await advance(hass, 6)
    registry = er.async_get(hass)
    voltage_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.data['address']}_battery_voltage")
    transfer_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.data['address']}_transfer")
    assert hass.states.get(voltage_id).state == "unavailable"
    state = hass.states.get(transfer_id)
    assert state.state == "idle"
    assert state.attributes["last_error"] is None
    assert state.attributes["battery_last_error"] == error
    assert state.attributes["battery_firmware_supported"] is supported
    assert (state.attributes["battery_last_read"] is not None) == (supported is True)
    assert await hass.config_entries.async_unload(entry.entry_id)
