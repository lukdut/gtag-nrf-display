"""Real HA flows/entities and production MQTT adapter; only the broker is fake."""
import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zlib

import pytest
from homeassistant.components import mqtt
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.gtag_ble_test import zigbee as module
from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.zigbee import ZigbeeTransport

IEEE = "0xf4ce36b9f217542f"
SECOND = "0x0011223344556677"


def inventory(ieee=IEEE, name="room/display", compatible=True):
    return {"ieee_address": ieee, "friendly_name": name, "model_id": "GTag_Display_Frame_V1",
            "definition": {"exposes": [{"property": "frame_request_id"}] if compatible else []}}


@pytest.fixture
def broker(monkeypatch):
    class Broker:
        def __init__(self):
            self.callbacks = {}
            self.connections = []
            self.messages = []
            self.retained = {"zigbee2mqtt/bridge/devices": [inventory()]}
            self.hook = None

        def receive(self, topic, payload, retain=False):
            for listener in list(self.callbacks.get(topic, [])):
                listener(SimpleNamespace(payload=json.dumps(payload), retain=retain, topic=topic))

        async def subscribe(self, hass, topic, listener, *args, **kwargs):
            listeners = self.callbacks.setdefault(topic, [])
            listeners.append(listener)
            if topic in self.retained:
                listener(SimpleNamespace(payload=json.dumps(self.retained[topic]), retain=True, topic=topic))
            return lambda: listeners.remove(listener)

        async def publish(self, hass, topic, payload, *, qos, retain):
            assert qos == 0 and retain is False
            data = json.loads(payload)
            self.messages.append((topic, data))
            if self.hook:
                await self.hook(topic, data)

        def connection(self, hass, listener):
            self.connections.append(listener)
            return lambda: self.connections.remove(listener)

        def connected(self, value):
            for listener in list(self.connections):
                listener(value)

        def confirm(self, data, name="room/display", **changes):
            envelope = data["frame"]
            raw = base64.b64decode(envelope["data"])
            state = {"frame_status": "displayed", "frame_request_id": envelope["request_id"],
                     "frame_crc32": f"{zlib.crc32(raw):08x}", "frame_id": 42,
                     "frame_bytes": 128, "frame_retries": 0, "frame_transfer_ms": 2000}
            state.update(changes)
            self.receive(f"zigbee2mqtt/{name}", state)

    fake = Broker()
    monkeypatch.setattr(mqtt, "async_subscribe", fake.subscribe)
    monkeypatch.setattr(mqtt, "async_publish", fake.publish)
    monkeypatch.setattr(mqtt, "async_subscribe_connection_status", fake.connection)
    monkeypatch.setattr(mqtt, "is_connected", lambda hass: True)
    monkeypatch.setattr(mqtt, "async_wait_for_mqtt_client", AsyncMock(return_value=True))
    return fake


def make_entry(hass, ieee=IEEE, name="room/display"):
    entry = MockConfigEntry(domain=DOMAIN, title=name, unique_id=f"zigbee:{ieee}", data={
        "address": ieee, "transport": "zigbee", "base_topic": "zigbee2mqtt", "friendly_name": name,
    })
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def zigbee_entry(hass):
    return make_entry(hass)


@pytest.fixture
async def transport(hass, zigbee_entry, broker):
    value = ZigbeeTransport(hass, zigbee_entry, lambda: None)
    await value.async_setup()
    yield value
    await value.async_close()
    assert not any(broker.callbacks.values())
    assert not broker.connections


async def test_only_matching_live_lcd_ack_confirms_frame(transport, broker):
    raw = b"\xff" * 4096
    task = asyncio.create_task(transport.async_send(raw))
    await asyncio.sleep(0)
    topic, data = broker.messages[-1]
    assert topic == f"zigbee2mqtt/{IEEE}/set"
    assert base64.b64decode(data["frame"]["data"]) == raw
    broker.confirm(data, frame_request_id="another-sender")
    broker.receive("zigbee2mqtt/room/display", {
        "frame_status": "displayed", "frame_request_id": data["frame"]["request_id"],
        "frame_crc32": f"{zlib.crc32(raw):08x}",
    }, retain=True)
    broker.confirm(data, frame_status="sending")
    await asyncio.sleep(0)
    assert not task.done()
    broker.confirm(data)
    report = await task
    assert report["receiver_status"] == "displayed" and report["transport"] == "zigbee"
    assert transport._pending is None


@pytest.mark.parametrize("failure", ["crc", "radio", "mqtt", "bridge", "device", "timeout"])
async def test_failures_release_transfer_and_allow_retry(transport, broker, monkeypatch, failure):
    monkeypatch.setattr(module, "TRANSFER_TIMEOUT", 0.05)
    task = asyncio.create_task(transport.async_send(bytes(4096)))
    await asyncio.sleep(0)
    data = broker.messages[-1][1]
    if failure == "crc":
        broker.confirm(data, frame_crc32="bad")
    elif failure == "radio":
        broker.confirm(data, frame_status="error", frame_error="No Zigbee reply")
    elif failure == "mqtt":
        broker.connected(False)
    elif failure == "bridge":
        broker.receive("zigbee2mqtt/bridge/state", {"state": "offline"})
    elif failure == "device":
        broker.receive("zigbee2mqtt/room/display/availability", {"state": "offline"})
    with pytest.raises(HomeAssistantError):
        await task
    assert transport._pending is None
    broker.connected(True)
    broker.receive("zigbee2mqtt/bridge/state", {"state": "online"})
    broker.receive("zigbee2mqtt/room/display/availability", {"state": "online"})
    async def confirm(topic, data):
        broker.confirm(data)
    broker.hook = confirm
    assert (await transport.async_send(bytes(4096)))["receiver_status"] == "displayed"


async def test_unload_cancels_pending_and_removes_subscriptions(transport, broker):
    task = asyncio.create_task(transport.async_send(bytes(4096)))
    await asyncio.sleep(0)
    await transport.async_close()
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await task
    assert not any(broker.callbacks.values())
    assert not broker.connections


async def test_rename_and_passive_battery(hass, transport, broker, zigbee_entry):
    broker.receive("zigbee2mqtt/room/display", {"battery_voltage_1": 4.1, "last_seen": "2026-09-18T01:00:00Z"})
    assert transport.voltage == 4.1 and transport.last_read is not None
    assert not broker.messages  # No BLE connection or extra radio battery polls.
    broker.receive("zigbee2mqtt/bridge/devices", [inventory(name="new/name")])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert zigbee_entry.data["friendly_name"] == "new/name"
    assert not broker.callbacks["zigbee2mqtt/room/display"]
    broker.receive("zigbee2mqtt/new/name", {"battery_voltage_1": 3.9})
    assert transport.voltage == 3.9
    broker.receive("zigbee2mqtt/new/name/availability", {"state": "offline"})
    assert transport.voltage is None
    broker.receive("zigbee2mqtt/new/name/availability", {"state": "online"})
    assert transport.voltage == 3.9


async def test_inventory_removal_and_return_recovers_transport(transport, broker):
    broker.receive("zigbee2mqtt/bridge/devices", [])
    assert not transport.available
    broker.receive("zigbee2mqtt/bridge/devices", [inventory()])
    assert transport.available


async def test_two_displays_do_not_share_confirmations(hass, transport, broker):
    second_entry = make_entry(hass, SECOND, "second")
    broker.retained["zigbee2mqtt/bridge/devices"].append(inventory(SECOND, "second"))
    second = ZigbeeTransport(hass, second_entry, lambda: None)
    await second.async_setup()
    try:
        first_task = asyncio.create_task(transport.async_send(bytes(4096)))
        second_task = asyncio.create_task(second.async_send(b"\xff" * 4096))
        await asyncio.sleep(0)
        first_data, second_data = [message[1] for message in broker.messages[-2:]]
        broker.confirm(first_data, name="second")
        await asyncio.sleep(0)
        assert not first_task.done() and not second_task.done()
        broker.confirm(second_data, name="second")
        await second_task
        assert not first_task.done()
        broker.confirm(first_data)
        await first_task
    finally:
        await second.async_close()


async def test_flow_selects_transport_and_discovers_by_ieee(hass, broker, monkeypatch):
    hass.config.components.add("bluetooth")
    monkeypatch.setattr("custom_components.gtag_ble_test.async_setup_entry", AsyncMock(return_value=True))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "menu"
    assert result["menu_options"] == ["ble", "zigbee"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "zigbee"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"base_topic": "zigbee2mqtt"})
    assert result["step_id"] == "zigbee_device"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": IEEE})
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == f"zigbee:{IEEE}"
    assert result["data"]["transport"] == "zigbee"
    assert not any(broker.callbacks.values())


@pytest.mark.parametrize("case,error", [
    ("mqtt", "mqtt_not_ready"), ("empty", "no_zigbee_devices"),
    ("old", "converter_update_required"), ("topic", "invalid_topic"), ("timeout", "cannot_connect"),
])
async def test_flow_reports_setup_problems(hass, broker, monkeypatch, case, error):
    hass.config.components.add("bluetooth")
    if case == "mqtt":
        mqtt.async_wait_for_mqtt_client.return_value = False
    elif case == "empty":
        broker.retained["zigbee2mqtt/bridge/devices"] = []
    elif case == "old":
        broker.retained["zigbee2mqtt/bridge/devices"] = [inventory(compatible=False)]
    elif case == "timeout":
        broker.retained.clear()
        monkeypatch.setattr(module, "DISCOVERY_TIMEOUT", 0.01)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "zigbee"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"base_topic": "#" if case == "topic" else "zigbee2mqtt"})
    assert error in result["errors"].values()
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert not any(broker.callbacks.values())


async def test_real_entities_options_preview_failure_and_reload(hass, zigbee_entry, broker, sent, battery_client):
    raw_frames = []
    async def confirm(topic, data):
        raw_frames.append(base64.b64decode(data["frame"]["data"]))
        broker.confirm(data)
    broker.hook = confirm
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    display = zigbee_entry.runtime_data
    registry = er.async_get(hass)
    identity = f"zigbee:{IEEE}"
    assert registry.async_get_entity_id("switch", DOMAIN, f"{identity}_clock_screen")
    assert not registry.async_get_entity_id("switch", DOMAIN, f"{identity}_led")
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, identity), zigbee_entry.entry_id)
    assert device is not None and not device.connections
    broker.receive("zigbee2mqtt/room/display", {"battery_voltage_1": 4.1})
    await hass.async_block_till_done()
    battery_id = registry.async_get_entity_id("sensor", DOMAIN, f"{identity}_battery_voltage")
    assert hass.states.get(battery_id).state == "4.1"
    result = await hass.config_entries.options.async_init(zigbee_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"preset": "clock", "update_interval": 5})
    assert result["step_id"] == "preview"
    assert not raw_frames  # Preview is local until Apply.
    await hass.config_entries.options.async_configure(result["flow_id"], {"action": "apply"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.preview is not None and len(raw_frames) == 1
    assert display.report["transport"] == "zigbee"
    assert sent == []
    battery_client.read_gatt_char.assert_not_awaited()
    assert "bluetooth" not in hass.config.components
    old_preview = display.preview
    async def fail(topic, data):
        broker.confirm(data, frame_status="error", frame_error="No radio reply")
    broker.hook = fail
    display._configured_interval = 0
    with pytest.raises(HomeAssistantError, match="No radio reply"):
        await display.async_draw({"elements": [{"type": "text", "x": 0, "y": 0, "text": "NEW"}]})
    assert display.preview == old_preview and display.status == "error"
    broker.hook = confirm
    broker.connected(False)
    broker.connected(True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.status == "sent" and display.last_error is None
    # The intended static layout also survives an integration reload.
    assert await hass.config_entries.async_reload(zigbee_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert zigbee_entry.runtime_data.status == "sent"
    assert zigbee_entry.runtime_data.last_layout["elements"][0]["text"] == "NEW"
    assert await hass.config_entries.async_unload(zigbee_entry.entry_id)
    assert not any(broker.callbacks.values())


async def test_discovery_uses_custom_base_and_filters_existing_ieee(hass, broker, zigbee_entry):
    hass.config.components.add("bluetooth")
    broker.retained["house/zigbee/bridge/devices"] = [inventory(), inventory(SECOND, "second")]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "zigbee"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"base_topic": "house/zigbee"})
    choices = next(iter(result["data_schema"].schema.values())).container
    assert IEEE not in choices and SECOND in choices
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("payload", ["null", "{}", "[null]", "broken", '[{"model_id":"other"}]'])
def test_inventory_rejects_unrelated_or_malformed_values(payload):
    assert module.devices_from_payload(payload) == {}


def test_inventory_handles_invalid_exposes_without_crashing():
    device = inventory()
    for exposes in [None, 42, [None, {"property": []}]]:
        device["definition"]["exposes"] = exposes
        assert not module.devices_from_payload(json.dumps([device]))[IEEE]["compatible"]


async def test_real_ha_mqtt_client_subscription_and_publication(hass, zigbee_entry, mqtt_mock, mqtt_client_mock):
    """Use the actual HA MQTT dispatcher/client with only its socket mocked."""
    assert await mqtt.async_wait_for_mqtt_client(hass)
    transport = ZigbeeTransport(hass, zigbee_entry, lambda: None)
    await transport.async_setup()
    raw = b"\xff" * 4096
    task = asyncio.create_task(transport.async_send(raw))
    try:
        await hass.async_block_till_done()
        mqtt_mock.async_publish.assert_called_once()
        call = mqtt_mock.async_publish.call_args
        assert call.args[0] == f"zigbee2mqtt/{IEEE}/set"
        payload = json.loads(call.args[1])
        reply = json.dumps({"frame_status": "displayed", "frame_request_id": payload["frame"]["request_id"],
                            "frame_crc32": f"{zlib.crc32(raw):08x}"})
        async_fire_mqtt_message(hass, "zigbee2mqtt/room/display", reply, retain=True)
        await hass.async_block_till_done()
        assert not task.done()
        async_fire_mqtt_message(hass, "zigbee2mqtt/room/display", reply)
        assert (await asyncio.wait_for(task, 2))["receiver_status"] == "displayed"
    finally:
        task.cancel()
        await transport.async_close()
        # The fake socket must emit the close callback that Paho normally emits.
        mqtt_client_mock.on_socket_close(mqtt_client_mock, None, SimpleNamespace(fileno=lambda: -1))
        await hass.async_block_till_done()
