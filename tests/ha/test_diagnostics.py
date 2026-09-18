"""Fresh device replies, useful errors, real HA flows and per-device isolation."""
import asyncio
from dataclasses import asdict
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bleak.exc import BleakCharacteristicNotFoundError
import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.gtag_ble_test import transport as ble, zigbee
from custom_components.gtag_ble_test.connection import ConnectionCheckError
from custom_components.gtag_ble_test.const import DOMAIN, INFO_CHAR_UUID, STATUS_CHAR_UUID
from custom_components.gtag_ble_test.display import Display
from custom_components.gtag_ble_test.firmware_info import FirmwareInfo
from custom_components.gtag_ble_test.diagnostic_flow import safe_text
from test_options import loaded
from test_zigbee import broker, inventory, make_entry, IEEE, SECOND

INFO = FirmwareInfo('0.9.0', features=15)
RAW_INFO = struct.pack('<BBBBIIHHHH20s', 1, 1, 1, 1, 3, 15, 256, 128, 4096, 32, b'0.9.0')


@pytest.fixture
def radio(monkeypatch):
    client = SimpleNamespace(read_gatt_char=AsyncMock(return_value=RAW_INFO),
                             disconnect=AsyncMock(), clear_cache=AsyncMock())
    monkeypatch.setattr(ble, 'connect', AsyncMock(return_value=client))
    return client


async def test_ble_check_keeps_image_errors_and_freshness_unchanged(display, sent, radio):
    await display.async_refresh()
    display.last_error = 'Previous image update failed'
    before = (display.preview, display.last_success, display.last_confirmation, display._confirmed_at,
              display.confirmed_timeout, display.last_error, display.status, dict(display.report))
    result = await display.connection_check.async_run()
    assert result['status'] == 'ok'
    assert result['firmware_version'] == '0.9.0'
    assert result['last_display_update'] == display.last_success.isoformat()
    assert result['display_error'] == 'Previous image update failed'
    assert before == (display.preview, display.last_success, display.last_confirmation, display._confirmed_at,
                      display.confirmed_timeout, display.last_error, display.status, display.report)
    assert len(sent) == 1
    radio.read_gatt_char.assert_awaited_once_with(INFO_CHAR_UUID)
    radio.disconnect.assert_awaited_once()


@pytest.mark.parametrize('real_legacy', [True, False])
async def test_ble_refreshes_gatt_before_legacy_and_requires_status(hass, radio, real_legacy):
    missing = BleakCharacteristicNotFoundError(INFO_CHAR_UUID)
    radio.read_gatt_char.side_effect = [missing, missing, bytes(8)] if real_legacy else [missing, RAW_INFO]
    result = await ble.check_connection(hass, 'AA:BB:CC:DD:EE:FF')
    assert result.legacy is real_legacy
    radio.clear_cache.assert_awaited_once()
    assert radio.disconnect.await_count == 2
    assert ble.connect.call_args_list[-1].kwargs['use_services_cache'] is False
    if real_legacy:
        radio.read_gatt_char.assert_awaited_with(STATUS_CHAR_UUID)


@pytest.mark.parametrize(('response','code'), [(b'bad','invalid_response'), (TimeoutError(),'timeout')])
async def test_bad_ble_reply_and_timeout_are_not_legacy(display, radio, response, code):
    if isinstance(response, Exception):
        radio.read_gatt_char.side_effect = response
    else:
        radio.read_gatt_char.return_value = response
    result = await display.connection_check.async_run()
    assert result['status'] == 'error' and result['error_code'] == code
    assert not display.device_firmware_info
    radio.disconnect.assert_awaited_once()
    radio.read_gatt_char.side_effect = None
    radio.read_gatt_char.return_value = RAW_INFO
    assert (await display.connection_check.async_run())['status'] == 'ok'


async def test_concurrent_checks_share_one_reply_and_cancelled_waiter_does_not_cancel_device(display, radio):
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(_uuid):
        entered.set()
        await release.wait()
        return RAW_INFO
    radio.read_gatt_char.side_effect = read
    first = asyncio.create_task(display.connection_check.async_run())
    await entered.wait()
    second = asyncio.create_task(display.connection_check.async_run())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)['status'] == 'ok'
    radio.read_gatt_char.assert_awaited_once()


async def test_busy_ble_check_does_not_interleave_a_transfer(display, radio):
    async with ble.get_operation_lock(display.hass, display.address):
        result = await display.connection_check.async_run()
    assert result['error_code'] == 'device_busy'
    radio.read_gatt_char.assert_not_awaited()


async def test_unload_cancels_radio_and_releases_lock(display, radio):
    entered = asyncio.Event()
    async def read(_uuid):
        entered.set()
        await asyncio.Event().wait()
    radio.read_gatt_char.side_effect = read
    task = asyncio.create_task(display.connection_check.async_run())
    await entered.wait()
    await display.async_close()
    with pytest.raises(asyncio.CancelledError):
        await task
    radio.disconnect.assert_awaited_once()
    assert not ble.get_operation_lock(display.hass, display.address).locked()
    assert display.connection_check.error_code == 'unloaded'


async def test_success_time_survives_unload_without_claiming_fresh_data(display, hass, entry):
    await display.async_set_clock(True)
    last_success = display.last_success
    await display.async_close()
    await display.async_close()  # Repeated cleanup must not save the stopped clock.
    restored = Display(hass, entry)
    try:
        await restored.async_load()
        assert restored.last_success == last_success
        assert restored.clock_enabled
        assert restored.stale is None and restored.last_confirmation is None
        assert restored.connection_check.status == 'not_checked'
    finally:
        await restored.async_close()


def test_diagnostic_errors_are_plain_text():
    escaped = safe_text('<img src=x> [open](https://example.org)\n**bold**')
    assert '<img' not in escaped and '\\[open\\]' in escaped
    assert '\n' not in escaped and '\\*\\*bold' in escaped


@pytest.mark.parametrize('delayed', [False, True])
async def test_options_diagnostics_progress_translations_and_button_service(hass, loaded, radio, delayed):
    entry = loaded
    options_before = dict(entry.options)
    result = await hass.config_entries.options.async_init(entry.entry_id, context={'language':'ru'})
    assert 'diagnostics' in result['menu_options']
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'next_step_id':'diagnostics'})
    assert result['description_placeholders']['check_status'] == 'Ещё не проверяли'
    if delayed:
        async def read(_uuid):
            await asyncio.sleep(0)
            return RAW_INFO
        radio.read_gatt_char.side_effect = read
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'action':'check'})
    if delayed:
        assert result['type'] == 'progress'
        await hass.async_block_till_done(wait_background_tasks=True)
        result = await hass.config_entries.options.async_configure(result['flow_id'])
    assert result['step_id'] == 'diagnostics'
    assert result['description_placeholders']['firmware'] == '0.9.0'
    assert result['description_placeholders']['check_status'] == 'Плата ответила'
    assert 'Защита от разряда' in result['description_placeholders']['features']
    assert entry.options == options_before
    entry.runtime_data.status = 'error'
    entry.runtime_data.last_error = 'Image CRC mismatch'
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'action':'refresh'})
    assert result['description_placeholders']['display_status'] == 'Ошибка передачи изображения'
    assert result['description_placeholders']['check_status'] == 'Плата ответила'
    assert result['description_placeholders']['display_error'] == 'Image CRC mismatch'
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'action':'back'})
    assert result['type'] == 'menu'
    hass.config_entries.options.async_abort(result['flow_id'])
    registry = er.async_get(hass)
    sensor = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.runtime_data.identity}_connection_check')
    button = registry.async_get_entity_id('button', DOMAIN, f'{entry.runtime_data.identity}_check_connection')
    assert hass.states.get(sensor).state == 'ok'
    await hass.services.async_call('button','press',{'entity_id':button},blocking=True)
    device = dr.async_get(hass).async_get(registry.async_get(sensor).device_id)
    response = await hass.services.async_call(DOMAIN,'check_connection',{'device_id':device.id},blocking=True,return_response=True)
    assert response['status'] == 'ok'
    assert response['firmware_features'] == ['freshness','battery_voltage','battery_bar','battery_protection']
    assert radio.read_gatt_char.await_count == 3


async def test_unloading_during_progress_returns_explanation(hass, loaded, radio):
    entered = asyncio.Event()
    async def read(_uuid):
        entered.set()
        await asyncio.Event().wait()
    radio.read_gatt_char.side_effect = read
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'next_step_id':'diagnostics'})
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'action':'check'})
    assert result['type'] == 'progress'
    await entered.wait()
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_configure(result['flow_id'])
    assert result['type'] == 'abort' and result['reason'] == 'diagnostics_unavailable'


def current_inventory(ieee=IEEE, name='room/display'):
    item = inventory(ieee,name)
    item['definition']['exposes'].append({'property':'check_connection'})
    return item


@pytest.fixture
async def zigbee_display(hass, broker):
    broker.retained['zigbee2mqtt/bridge/devices'] = [current_inventory()]
    value = Display(hass, make_entry(hass))
    await value.async_load()
    yield value
    await value.async_close()


def reply(broker, request, name='room/display', **changes):
    state = {'connection_request_id':request['check_connection']['request_id'], 'connection_status':'ok',
             'firmware_capabilities':json.dumps(asdict(INFO))}
    state.update(changes)
    broker.receive(f'zigbee2mqtt/{name}',state)


async def test_zigbee_requires_matching_live_reply(zigbee_display, broker):
    display=zigbee_display
    entered=asyncio.Event()
    async def publish(_topic,data):
        entered.set()
    broker.hook=publish
    task=asyncio.create_task(display.connection_check.async_run())
    await entered.wait()
    topic,data=broker.messages[-1]
    assert topic==f'zigbee2mqtt/{IEEE}/set'
    state={'connection_request_id':data['check_connection']['request_id'], 'connection_status':'ok',
           'firmware_capabilities':json.dumps(asdict(INFO))}
    broker.receive('zigbee2mqtt/room/display',state,retain=True)
    reply(broker,data,connection_request_id='previous-check')
    reply(broker,data,connection_status='checking')
    reply(broker,data,name='different-device')
    await asyncio.sleep(0)
    assert not task.done()
    reply(broker,data)
    assert (await task)['status']=='ok'
    assert display.last_success is None and display.last_confirmation is None
    assert display.preview is None and not display.zigbee._lock.locked()


@pytest.mark.parametrize('code',['mqtt_offline','bridge_offline','device_missing','converter_update_required','timeout','invalid_response','incompatible_firmware'])
async def test_zigbee_error_reasons(zigbee_display, broker, monkeypatch, code):
    display=zigbee_display
    monkeypatch.setattr(zigbee,'CHECK_TIMEOUT',0.01)
    if code=='mqtt_offline': broker.connected(False)
    elif code=='bridge_offline': broker.receive('zigbee2mqtt/bridge/state',{'state':'offline'})
    elif code=='device_missing': broker.receive('zigbee2mqtt/bridge/devices',[])
    elif code=='converter_update_required': broker.receive('zigbee2mqtt/bridge/devices',[inventory()])
    elif code in ('invalid_response','incompatible_firmware'):
        async def hook(_topic,data):
            reply(broker,data,firmware_capabilities={} if code=='invalid_response' else asdict(FirmwareInfo('2.0',protocol_min=2,protocol_max=2)))
        broker.hook=hook
    result=await display.connection_check.async_run()
    assert result['status']=='error' and result['error_code']==code
    assert not display.zigbee._lock.locked() and display.zigbee._check_pending is None
    if code=='incompatible_firmware': assert display.device_firmware_info['firmware_version']=='2.0'


async def test_two_zigbee_checks_are_independent_and_disconnect_finishes_pending(hass, zigbee_display, broker):
    broker.receive('zigbee2mqtt/bridge/devices',[current_inventory(),current_inventory(SECOND,'second')])
    broker.retained['zigbee2mqtt/bridge/devices']=[current_inventory(),current_inventory(SECOND,'second')]
    second=Display(hass,make_entry(hass,SECOND,'second'))
    await second.async_load()
    entered=asyncio.Event()
    async def hook(topic,data):
        if SECOND in topic: reply(broker,data,'second',firmware_capabilities=asdict(FirmwareInfo.legacy_v1()))
        else: entered.set()
    broker.hook=hook
    try:
        first=asyncio.create_task(zigbee_display.connection_check.async_run())
        await entered.wait()
        result=await second.connection_check.async_run()
        assert result['status']=='ok' and result['firmware_legacy'] is True
        assert not first.done()
        broker.connected(False)
        assert (await first)['error_code']=='mqtt_offline'
        assert second.connection_check.status=='ok'
    finally:
        await second.async_close()
