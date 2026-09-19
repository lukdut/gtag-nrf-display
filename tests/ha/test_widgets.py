"""Weather service, real Recorder history, dynamic layouts and portable presets."""
import asyncio
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest
import voluptuous as vol
import yaml
from homeassistant.components.weather import WeatherEntity, WeatherEntityFeature
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gtag_ble_test.layouts import async_render_layout, preset_layout, validate_settings
from custom_components.gtag_ble_test.layout_transfer import (
    current_screen, decode_document, encode_document, entity_references, export_document, remap_settings,
)
from custom_components.gtag_ble_test.render import LAYOUT_SCHEMA, render_layout
from custom_components.gtag_ble_test.widget_data import resolve_widget
from custom_components.gtag_ble_test.widget_render import graph_segments
from test_options import loaded, apply


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder):
    """Install Recorder's database fixtures before the autouse HA fixture."""


class ForecastWeather(WeatherEntity):
    _attr_name = "Weather test"
    _attr_condition = "partlycloudy"
    _attr_native_temperature = 21.5
    _attr_native_temperature_unit = "°C"
    _attr_humidity = 45
    _attr_supported_features = WeatherEntityFeature.FORECAST_HOURLY
    calls = 0

    async def async_forecast_hourly(self):
        self.calls += 1
        now = dt_util.utcnow()
        return [{"datetime": (now + timedelta(hours=i)).isoformat(), "condition": "rainy",
                 "native_temperature": 22 - i} for i in range(-1, 9)]


@pytest.fixture
async def weather(hass):
    assert await async_setup_component(hass, "weather", {})
    entity = ForecastWeather()
    await hass.data["weather"].async_add_entities([entity])
    return entity


async def test_container_demo_uses_supported_template_weather_yaml(hass):
    script = (Path(__file__).resolve().parents[2] / 'tests/container/run.sh').read_text()
    raw = script.split(".write_text('''", 1)[1].split("''', encoding=", 1)[0]
    config = yaml.safe_load(raw)
    hass.states.async_set('input_number.gtag_temperature', '21.5')
    hass.states.async_set('input_number.gtag_humidity', '45')
    assert await async_setup_component(hass, 'template', config)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get('weather.gtag_container_weather')
    assert state is not None and state.state == 'partlycloudy'
    response = await hass.services.async_call('weather', 'get_forecasts',
        {'entity_id': state.entity_id, 'type': 'hourly'}, blocking=True, return_response=True)
    assert len(response[state.entity_id]['forecast']) == 6
    await hass.data['weather'].async_remove_entity(state.entity_id)


async def test_real_forecast_service_conversion_cache_and_future_times(hass, weather, freezer):
    await hass.config.async_set_time_zone("Europe/Moscow")
    layout = preset_layout({"preset": "weather", "entity_1": weather.entity_id})
    item = layout["elements"][0]
    first, second = await asyncio.gather(resolve_widget(hass, item), resolve_widget(hass, item))
    assert first == second and weather.calls == 1
    assert len(first["forecast"]) == 6
    assert first["forecast"][0]["time"] == dt_util.now() + timedelta(hours=1)
    assert first["forecast"][0]["temperature"] == 21
    frame = await async_render_layout(hass, layout)
    assert len(frame.raw) == 4096 and weather.calls == 1
    # Firmware overlays own the bottom three rows; the layout must leave them free.
    assert Image.open(BytesIO(frame.png)).crop((0, 125, 256, 128)).getextrema() == (255, 255)
    freezer.tick(timedelta(minutes=11))
    await resolve_widget(hass, item)
    assert weather.calls == 2


async def test_dynamic_screen_moves_time_without_state_change_and_stops_on_manual_draw(hass, display, sent, freezer, monkeypatch):
    monkeypatch.setattr(type(display), "update_interval", property(lambda self: 0))
    hass.states.async_set("sensor.graph", "21", {"unit_of_measurement": "°C"})
    await display.async_apply_settings({"preset": "value_graph", "entity_1": "sensor.graph", "update_interval": 5}, "graph")
    before = sent[-1]
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] != before
    await display.async_draw({"elements": [{"type": "text", "x": 8, "y": 8, "text": "Manual"}]}, auto_update=False)
    count = len(sent)
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == count


async def test_weather_without_hourly_service_or_unavailable_keeps_current_information(hass):
    hass.states.async_set("weather.test", "sunny", {"temperature": 18, "temperature_unit": "°C"})
    layout = preset_layout({"preset": "weather", "entity_1": "weather.test"})
    data = await resolve_widget(hass, layout["elements"][0])
    assert data["temperature"] == 18 and not data["forecast"]
    first = await async_render_layout(hass, layout)
    hass.states.async_set("weather.test", "unavailable", {"temperature": 18})
    data = await resolve_widget(hass, layout["elements"][0])
    assert data["temperature"] is None and not data["forecast"]
    assert (await async_render_layout(hass, layout)).raw != first.raw


async def test_history_uses_real_recorder_with_unavailability_gap(recorder_mock, hass, freezer):
    freezer.move_to("2026-09-19T09:00:00+00:00")
    for state in ("20", "24", "unavailable", "22"):
        hass.states.async_set("sensor.graph", state, {"unit_of_measurement": "°C"})
        await hass.async_block_till_done()
        await recorder_mock.async_block_till_done()
        freezer.tick(timedelta(minutes=10))
    item = LAYOUT_SCHEMA(preset_layout({"preset": "value_graph", "entity_1": "sensor.graph"}))["elements"][0]
    data = await resolve_widget(hass, item)
    assert [v for _, v in data["points"]][:4] == [20, 24, None, 22]
    segments = graph_segments(data["points"], data["start"].timestamp(), data["now"].timestamp())
    assert len(segments) == 2
    frame = await async_render_layout(hass, {"elements": [item]})
    assert len(frame.raw) == 4096
    assert Image.open(BytesIO(frame.png)).crop((0, 125, 256, 128)).getextrema() == (255, 255)
    hass.states.async_set("sensor.graph", "72", {"unit_of_measurement": "°F"})
    changed_unit = await resolve_widget(hass, item)
    assert all(value is None for _, value in changed_unit["points"][:-1])
    assert changed_unit["points"][-1][1] == 72


async def test_graph_without_recorder_and_non_numeric_state(hass):
    hass.states.async_set("sensor.graph", "nan")
    layout = preset_layout({"preset": "value_graph", "entity_1": "sensor.graph"})
    data = await resolve_widget(hass, layout["elements"][0])
    assert not data["points"] and data["value"] is None
    assert len((await async_render_layout(hass, layout)).raw) == 4096


def test_graph_downsampling_keeps_peaks_gaps_constant_negative_and_extreme_values():
    segments = graph_segments([(0, -5), (.001, 999), (.002, -8), (.003, None), (50, -1), (100, -1)], 0, 100)
    assert len(segments) == 2
    assert 999 in [value for _, value in segments[0]]
    assert -8 in [value for _, value in segments[0]]
    # State changes and a live sample can share a timestamp. Sorting must not
    # compare the numeric value with None or erase the unavailable boundary.
    assert len(graph_segments([(0, 1), (10, None), (10, 2), (20, 3)], 0, 20)) == 2
    now = dt_util.now()
    item = LAYOUT_SCHEMA(preset_layout({"preset": "value_graph", "entity_1": "sensor.graph"}))["elements"][0]
    for values in ((-1, -1), (-1e308, 1e308)):
        data = {"label": "Тест", "now": now, "start": now - timedelta(hours=24), "value": values[-1],
                "unit": "V", "points": [(now.timestamp() - 3600, values[0]), (now.timestamp(), values[1])], "truncated": False}
        assert len(render_layout({"elements": [item]}, {0: data}).raw) == 4096


@pytest.mark.parametrize("preset,entity", [("weather", "weather.test"), ("value_graph", "sensor.test")])
async def test_portable_dynamic_presets_and_custom_widgets(hass, preset, entity):
    settings = {"preset": preset, "entity_1": entity, "history_hours": 6}
    for source in (settings, {"preset": "custom", "layout": preset_layout(settings)}):
        decoded = decode_document(encode_document(export_document(source, "Перенос")).encode())
        assert entity_references(decoded["screen"]) == [entity]
        target = entity + "_other"
        mapped = remap_settings(decoded["screen"], {entity: target})
        assert entity_references(mapped) == [target]
        if preset == "value_graph":
            assert preset_layout(mapped)["elements"][0]["hours"] == 6
        with pytest.raises(vol.Invalid):
            remap_settings(decoded["screen"], {entity: "light.invalid"})


async def test_graph_ui_preview_apply_reload_and_export(hass, loaded, sent):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "configure"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"preset": "value_graph", "update_interval": 60})
    assert result["step_id"] == "values"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": "sensor.temperature", "history_hours": 12, "decimals_1": "1"})
    assert result["step_id"] == "preview" and not result["errors"] and not sent
    await apply(hass, result)
    assert len(sent) == 1
    assert (await current_screen(hass, loaded))["history_hours"] == 12
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == 2
    assert loaded.runtime_data.last_layout["elements"][0]["hours"] == 12


async def test_weather_ui_rejects_daily_only_then_accepts_hourly(hass, loaded, weather, sent):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "configure"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"preset": "weather", "update_interval": 60})
    hass.states.async_set("weather.daily", "sunny", {"supported_features": 1})
    bad = await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": "weather.daily"})
    assert bad["errors"]["entity_1"] == "hourly_forecast_required"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entity_1": weather.entity_id})
    assert result["step_id"] == "preview" and not result["errors"]
    await apply(hass, result)
    assert (await current_screen(hass, loaded))["preset"] == "weather"


@pytest.mark.parametrize("hours", [0, 169, -1])
def test_graph_period_is_bounded(hours):
    with pytest.raises(vol.Invalid):
        validate_settings({"preset": "value_graph", "entity_1": "sensor.x", "history_hours": hours})
