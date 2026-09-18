"""Portable formats, exact entity remapping and isolation of two displays."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlsplit

from aiohttp import FormData
import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.layout_transfer import (
    MAX_FILE_BYTES, LayoutFileError, current_screen, decode_document,
    encode_document, entity_references, export_document, remap_settings,
)
from custom_components.gtag_ble_test.layouts import preset_layout
from custom_components.gtag_ble_test.render import LAYOUT_SCHEMA
from test_options import loaded, preview, apply
from test_zigbee import broker


def text_layout(text):
    return {"elements": [{"type": "text", "x": 8, "y": 8, "text": text}]}


def preset(**kwargs):
    return {"preset": "clock_two_values", "entity_1": "sensor.temperature", "entity_2": "sensor.humidity",
            "label_1": "Комната", "unit_1": "°C", "decimals_1": "1", "decimals_2": "0",
            "update_interval": 30, "stale_after": 5, **kwargs}


@pytest.mark.parametrize("settings", [preset(), {"preset": "clock"},
    {"preset": "custom", "layout": text_layout("Привет!"), "auto_update": False}])
def test_portable_file_round_trip(settings):
    document = export_document(settings, "Комната")
    assert decode_document(encode_document(document).encode()) == document
    assert "screen_revision" not in document and "device_id" not in document


@pytest.mark.parametrize("payload,error", [
    (b"not JSON", "invalid_layout_file"),
    (b"{}", "invalid_layout_file"),
    (b"[]", "invalid_layout_file"),
    (b"{\"format\":\"gtag-display-layout\",\"version\":2}", "unsupported_layout_version"),
    (b"{\"format\":\"gtag-display-layout\",\"version\":true}", "unsupported_layout_version"),
    (b"{\"format\":\"other\",\"format\":\"gtag-display-layout\"}", "invalid_layout_file"),
    (b"x" * (MAX_FILE_BYTES + 1), "layout_file_too_large"),
])
def test_reject_invalid_files(payload, error):
    with pytest.raises(LayoutFileError, match=error):
        decode_document(payload)


@pytest.mark.parametrize("change", [
    {"stale_after": 1, "update_interval": 60}, {"decimals_1": "9"},
    {"entity_1": "not-an-entity"}, {"unexpected": True},
    {"preset": "custom", "layout": text_layout("{{ invalid template")},
    {"preset": "custom", "layout": {"elements": [{"type": "text", "x": -1, "y": 0, "text": "X"}]}},
])
def test_invalid_screen_is_rejected_before_preview(change):
    document = {"format": "gtag-display-layout", "version": 1, "name": "Test", "screen": {**preset(), **change}}
    with pytest.raises(LayoutFileError, match="invalid_layout_file"):
        decode_document(json.dumps(document).encode())


def test_export_omits_unused_entity_slots_and_remap_is_simultaneous():
    clock = export_document(preset(preset="clock"), "Clock")
    assert entity_references(clock["screen"]) == []
    assert "entity_1" not in clock["screen"]
    original = preset(label_1="sensor.temperature")
    swapped = remap_settings(original, {"sensor.temperature": "sensor.humidity", "sensor.humidity": "sensor.temperature"})
    assert swapped["entity_1"] == "sensor.humidity" and swapped["entity_2"] == "sensor.temperature"
    assert swapped["label_1"] == original["label_1"]
    assert original["entity_1"] == "sensor.temperature"


async def test_custom_template_remapping_preserves_whitespace_static_text_comments_and_raw(hass):
    source = "sensor.old {% raw %}{{ states('sensor.old') }}{% endraw %}{# sensor.old #} {{- states('sensor.old') -}} / {{ states.sensor.other.state }} / {{ state_attr('sensor.old', 'unit_of_measurement') }}"
    settings = {"preset": "custom", "layout": text_layout(source)}
    assert entity_references(settings) == ["sensor.old", "sensor.other"]
    mapped = remap_settings(settings, {"sensor.old": "sensor.other", "sensor.other": "sensor.old"})
    actual = mapped["layout"]["elements"][0]["text"]
    assert "{% raw %}{{ states('sensor.old') }}{% endraw %}" in actual
    assert actual.startswith("sensor.old ") and "{# sensor.old #}" in actual
    assert '{{- states("sensor.other") -}}' in actual
    assert 'states["sensor.old"].state' in actual
    hass.states.async_set("sensor.old", "11", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.other", "22", {"unit_of_measurement": "B"})
    from homeassistant.helpers.template import Template
    assert Template(actual, hass).async_render(parse_result=False).endswith("22/ 11 / B")
    assert settings["layout"]["elements"][0]["text"] == source


async def test_export_tracks_manual_override_and_unloaded_state(hass, loaded, sent):
    await apply(hass, await preview(hass, loaded, "single_value", entity_1="sensor.temperature"))
    assert (await current_screen(hass, loaded))["preset"] == "single_value"
    custom = text_layout("{{ states('sensor.humidity') }}")
    await loaded.runtime_data.async_draw(custom, auto_update=False)
    exported = await current_screen(hass, loaded)
    assert exported["preset"] == "custom" and exported["auto_update"] is False
    assert exported["layout"] == LAYOUT_SCHEMA(custom)
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert await current_screen(hass, loaded) == exported


@pytest.fixture
async def files(hass, hass_client, monkeypatch):
    assert await async_setup_component(hass, "file_upload", {})
    # Mirror HomeAssistantHTTP.start; the aiohttp test server bypasses it.
    monkeypatch.setattr(hass.http.app.router, "freeze", lambda: None)
    return await hass_client()


async def upload(files, data):
    form = FormData()
    form.add_field("file", data, filename="gtag-layout.json", content_type="application/json")
    response = await files.post("/api/file_upload", data=form)
    assert response.status == 200
    return (await response.json())["file_id"]


async def start_option(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


async def export_flow(hass, entry, files):
    flow = await start_option(hass, entry, "export_layout")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Комната"})
    assert flow["step_id"] == "export_download" and not flow["errors"]
    url = flow["description_placeholders"]["download_url"]
    response = await files.get(url)
    assert response.status == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename="gtag-layout.json"'
    raw = await response.read()
    finished = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert finished["reason"] == "layout_exported"
    assert (await files.get(finished["description_placeholders"]["download_url"])).status == 200
    return raw


async def import_flow(hass, entry, files, raw):
    result = await start_option(hass, entry, "import_layout")
    file_id = await upload(files, raw)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"file": file_id})
    assert not hass.data["file_upload"].has_file(file_id)
    return result


async def test_transfer_between_two_displays_and_restore(hass, loaded, files, sent):
    await apply(hass, await preview(hass, loaded, label_1="Температура", decimals_1="1", decimals_2="0"))
    original_options = deepcopy(dict(loaded.options))
    original_preview = loaded.runtime_data.preview
    raw = await export_flow(hass, loaded, files)
    assert len(sent) == 1 and loaded.options == original_options
    assert loaded.data["address"].encode() not in raw and loaded.entry_id.encode() not in raw
    target = MockConfigEntry(domain=DOMAIN, title="Second", data={"address": "AA:BB:CC:DD:EE:02"}, unique_id="AA:BB:CC:DD:EE:02")
    target.add_to_hass(hass)
    assert await hass.config_entries.async_setup(target.entry_id)
    hass.states.async_set("sensor.kitchen_temperature", "18.123", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.kitchen_humidity", "39.876", {"unit_of_measurement": "%"})
    flow = await import_flow(hass, target, files, raw)
    assert flow["description_placeholders"]["source"] == "sensor.temperature"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.kitchen_temperature"})
    assert flow["description_placeholders"]["source"] == "sensor.humidity"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.kitchen_humidity"})
    assert flow["step_id"] == "preview" and not flow["errors"]
    assert not target.options and len(sent) == 1
    await apply(hass, flow)
    assert target.options["screen"]["entity_1"] == "sensor.kitchen_temperature"
    assert target.options["screen"]["entity_2"] == "sensor.kitchen_humidity"
    assert target.options["screen"]["decimals_1"] == "1"
    assert loaded.options == original_options and loaded.runtime_data.preview == original_preview
    assert len(sent) == 2
    assert await hass.config_entries.async_reload(target.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert target.runtime_data.last_layout == LAYOUT_SCHEMA(preset_layout(target.options["screen"]))
    before = len(sent)
    hass.states.async_set("sensor.kitchen_temperature", "19.6")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == before + 1 and loaded.runtime_data.preview == original_preview


async def test_custom_import_cancel_apply_remap_and_restart(hass, loaded, files, sent):
    await apply(hass, await preview(hass, loaded, "clock"))
    original = deepcopy(dict(loaded.options))
    settings = {"preset": "custom", "layout": text_layout("{{ states.sensor.temperature.state }} °C"),
                "auto_update": False, "update_interval": 45, "stale_after": 3}
    raw = encode_document(export_document(settings, "Custom")).encode()
    flow = await import_flow(hass, loaded, files, raw)
    missing = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.missing"})
    assert missing["errors"] == {"entity_id": "entity_not_found"}
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.humidity"})
    assert flow["step_id"] == "preview" and not flow["errors"]
    hass.config_entries.options.async_abort(flow["flow_id"])
    assert loaded.options == original and len(sent) == 1
    flow = await import_flow(hass, loaded, files, raw)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.temperature"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"action": "edit"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "import_remap"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.humidity"})
    await apply(hass, flow)
    assert loaded.options["screen"]["preset"] == "custom"
    assert 'states["sensor.humidity"]' in loaded.runtime_data.last_layout["elements"][0]["text"]
    assert loaded.runtime_data.auto_update is False
    assert loaded.runtime_data._configured_interval == 45
    assert loaded.runtime_data.freshness_timeout == 180
    expected = loaded.runtime_data.last_layout
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded.runtime_data.last_layout == expected and loaded.runtime_data.auto_update is False
    before = len(sent)
    hass.states.async_set("sensor.humidity", "78")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == before


async def test_clock_import_has_no_entity_mapping_and_corrupt_file_does_not_mutate(hass, loaded, files, sent):
    flow = await import_flow(hass, loaded, files, b"invalid")
    assert flow["errors"] == {"file": "invalid_layout_file"} and not loaded.options and not sent
    file_id = await upload(files, encode_document(export_document({"preset": "clock"}, "Clock")).encode())
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"file": file_id})
    assert flow["step_id"] == "preview" and not flow["errors"]
    await apply(hass, flow)
    assert loaded.runtime_data.clock_enabled and len(sent) == 1


async def test_layout_download_requires_auth_and_expires(hass, hass_client_no_auth, freezer, monkeypatch):
    from aiohttp.web_log import AccessLogger
    from custom_components.gtag_ble_test.layout_download import async_export_download
    monkeypatch.setattr(AccessLogger, "_get_local_time", staticmethod(lambda: datetime.now(timezone.utc)))
    one = await async_export_download(hass, '{"name":"one"}')
    two = await async_export_download(hass, '{"name":"two"}')
    client = await hass_client_no_auth()
    assert (await client.get(urlsplit(one["download_url"]).path)).status == 401
    response = await client.get(one["download_url"])
    assert response.status == 200 and await response.json() == {"name": "one"}
    assert await (await client.get(two["download_url"])).json() == {"name": "two"}
    freezer.tick(timedelta(minutes=21))
    assert (await client.get(one["download_url"])).status == 401


async def test_zigbee_layout_import_routes_only_to_selected_device(hass, files, broker, sent):
    from test_zigbee import IEEE, SECOND, inventory, make_entry
    source = make_entry(hass)
    target = make_entry(hass, SECOND, "second")
    broker.retained["zigbee2mqtt/bridge/devices"].append(inventory(SECOND, "second"))
    names = {IEEE: "room/display", SECOND: "second"}
    async def confirm(topic, data):
        broker.confirm(data, name=names[topic.split('/')[1]])
    broker.hook = confirm
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.temperature", "22.5")
    hass.states.async_set("sensor.humidity", "48")
    await apply(hass, await preview(hass, source, "single_value"))
    original = source.runtime_data.preview
    raw = await export_flow(hass, source, files)
    flow = await import_flow(hass, target, files, raw)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"entity_id": "sensor.humidity"})
    before = len(broker.messages)
    await apply(hass, flow)
    assert len(broker.messages) == before + 1
    assert broker.messages[-1][0] == f"zigbee2mqtt/{SECOND}/set"
    assert source.runtime_data.preview == original
    assert target.runtime_data.preview != original
    assert not sent


async def test_longer_entity_ids_cannot_overflow_imported_text(hass, loaded, files, sent):
    expression = "{{ states('sensor.a') }}"
    settings = {"preset": "custom", "layout": text_layout(expression + 'x' * (1024 - len(expression)))}
    raw = encode_document(export_document(settings, "Long text")).encode()
    result = await import_flow(hass, loaded, files, raw)
    hass.states.async_set('sensor.much_longer_id', '1')
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'entity_id': 'sensor.much_longer_id'})
    assert result['step_id'] == 'import_entities'
    assert result['errors'] == {'base': 'invalid_settings'}
    assert not loaded.options and not sent
    hass.config_entries.options.async_abort(result['flow_id'])
