"""Three independent entity/text fields, their options flow and portable files."""
from itertools import product
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.helpers import translation
from homeassistant.helpers.template import Template

from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.layout_transfer import (
    current_screen, decode_document, encode_document, entity_references,
    export_document, remap_settings,
)
from custom_components.gtag_ble_test.layouts import (
    async_render_layout, preset_layout, validate_settings,
)
from custom_components.gtag_ble_test.render import preview_svg
from test_options import loaded, apply
from test_layout_transfer import files, export_flow, import_flow


def settings(sources=("entity", "entity", "entity")):
    result = {
        "preset": "three_values", "update_interval": 30,
        "entity_1": "sensor.temperature", "entity_2": "sensor.humidity",
        "entity_3": "sensor.co2", "label_3": "CO₂", "decimals_3": "0",
    }
    for index, source in enumerate(sources, 1):
        result[f"source_{index}"] = source
        if source == "text":
            result[f"text_{index}"] = f"Текст {index}: {{{{ states('sensor.temperature') }}}}"
    return result


async def choose_sources(hass, entry, sources):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "configure"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "preset": "three_values", "update_interval": 30,
    })
    assert result["step_id"] == "value_sources"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        f"source_{index}": source for index, source in enumerate(sources, 1)
    })
    assert result["step_id"] == "three_values"
    fields = {field.schema for field in result["data_schema"].schema}
    for index, source in enumerate(sources, 1):
        assert (f"text_{index}" in fields) == (source == "text")
        assert (f"entity_{index}" in fields) == (source == "entity")
        assert (f"decimals_{index}" in fields) == (source == "entity")
    return result


@pytest.mark.parametrize("sources", list(product(("entity", "text"), repeat=3)))
async def test_every_source_combination_renders_and_remaps_without_evaluating_text(hass, sources):
    hass.states.async_set("sensor.temperature", "22.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.humidity", "48", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.co2", "684.2", {"unit_of_measurement": "ppm"})
    original = settings(sources)
    layout = preset_layout(original)
    values = {3: layout["elements"][0], 1: layout["elements"][-2], 2: layout["elements"][-1]}
    expected = {1: "22.5 °C", 2: "48 %", 3: "CO₂: 684 ppm"}
    for index, element in values.items():
        rendered = Template(element["text"], hass).async_render(parse_result=False)
        assert rendered == (original[f"text_{index}"] if sources[index - 1] == "text" else expected[index])
    assert values[3]["align"] == "center" and values[3]["x"] == 128
    frame = await async_render_layout(hass, layout)
    assert len(frame.raw) == 4096

    document = export_document(original, "Комната")
    assert decode_document(encode_document(document).encode()) == document
    exported = document["screen"]
    active_entities = [original[f"entity_{index}"] for index, source in enumerate(sources, 1) if source == "entity"]
    assert entity_references(exported) == active_entities
    mapping = {entity: entity + "_new" for entity in active_entities}
    remapped = remap_settings(exported, mapping)
    for index, source in enumerate(sources, 1):
        if source == "entity":
            assert remapped[f"entity_{index}"] == mapping[original[f"entity_{index}"]]
            assert f"text_{index}" not in exported
        else:
            assert remapped[f"text_{index}"] == original[f"text_{index}"]
            assert f"entity_{index}" not in exported
            assert f"unit_{index}" not in exported


async def test_three_entities_preview_apply_track_each_field_and_reload(hass, loaded, sent):
    hass.states.async_set("sensor.co2", "684", {"unit_of_measurement": "ppm"})
    result = await choose_sources(hass, loaded, ("entity",) * 3)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "entity_1": "sensor.temperature", "entity_2": "sensor.humidity",
        "entity_3": "sensor.co2", "label_3": "CO₂", "decimals_3": "0",
    })
    assert result["step_id"] == "preview" and not result["errors"]
    expected = result["description_placeholders"]["preview"]
    assert not sent
    await apply(hass, result)
    assert preview_svg(sent[-1]) == expected
    # The agreed preview is produced by the real preset without changing the lower fields.
    reference = Path(__file__).resolve().parents[2] / "docs/images/layouts/three-values.png"
    frame = await async_render_layout(hass, preset_layout(loaded.options["screen"]))
    assert frame.png == reference.read_bytes()

    for entity, value in (("sensor.co2", "800"), ("sensor.temperature", "25"), ("sensor.humidity", "52")):
        previous = len(sent)
        hass.states.async_set(entity, value, hass.states.get(entity).attributes)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(sent) == previous + 1
        frame = await async_render_layout(hass, preset_layout(loaded.options["screen"]))
        assert sent[-1] == frame.raw
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] == frame.raw
    assert (await current_screen(hass, loaded))["preset"] == "three_values"


async def test_switch_to_all_text_clears_entity_subscriptions_and_keeps_literal_text(hass, loaded, sent):
    hass.states.async_set("sensor.co2", "684")
    result = await choose_sources(hass, loaded, ("entity",) * 3)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "entity_1": "sensor.temperature", "entity_2": "sensor.humidity", "entity_3": "sensor.co2",
    })
    await apply(hass, result)
    result = await choose_sources(hass, loaded, ("text",) * 3)
    literal = '{{ states("sensor.temperature") }} / \\ {% raw %}'
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "text_1": literal, "text_2": "Закрыто", "label_2": "Окно", "text_3": "Гостиная",
    })
    assert result["step_id"] == "preview" and not result["errors"]
    await apply(hass, result)
    stored = loaded.options["screen"]
    assert entity_references(stored) == []
    assert all(f"entity_{i}" not in stored for i in (1, 2, 3))
    texts = [Template(element["text"], hass).async_render(parse_result=False)
             for element in loaded.runtime_data.last_layout["elements"] if element["type"] == "text"]
    assert texts == ["Гостиная", "", "Окно", literal, "Закрыто"]
    count, raw = len(sent), sent[-1]
    for entity in ("sensor.temperature", "sensor.humidity", "sensor.co2"):
        hass.states.async_set(entity, "999")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(sent) == count
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sent[-1] == raw
    result = await choose_sources(hass, loaded, ("text",) * 3)
    suggestions = {field.schema: field.description.get("suggested_value")
                   for field in result["data_schema"].schema}
    assert suggestions["text_1"] == literal and suggestions["text_3"] == "Гостиная"
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("value", ["", "  ", "X" * 129, "Первая\nВторая", "Первая\rВторая"])
def test_empty_long_or_multiline_text_is_rejected(value):
    for index in (1, 2, 3):
        original = settings(("text",) * 3)
        original[f"text_{index}"] = value
        with pytest.raises(vol.Invalid) as error:
            validate_settings(original)
        assert error.value.path == [f"text_{index}"]


async def test_header_entity_and_text_errors_can_be_corrected(hass, loaded, sent):
    result = await choose_sources(hass, loaded, ("text", "text", "entity"))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "text_1": "Лево", "text_2": "Право", "entity_3": "sensor.missing",
    })
    assert result["errors"] == {"entity_3": "entity_not_found"}
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await choose_sources(hass, loaded, ("text",) * 3)
    fields = {"text_1": "Лево", "text_2": "Право", "text_3": " "}
    result = await hass.config_entries.options.async_configure(result["flow_id"], fields)
    assert result["errors"] == {"text_3": "invalid_text"}
    assert not sent and not loaded.options
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**fields, "text_3": "Комната"})
    assert result["step_id"] == "preview" and not result["errors"]
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("all_text", [False, True])
async def test_export_import_only_requests_active_entities(hass, loaded, files, sent, all_text):
    sources = ("text" if all_text else "entity", "text", "text")
    result = await choose_sources(hass, loaded, sources)
    values = {"text_2": "sensor.temperature", "text_3": "Гостиная"}
    values.update({"text_1": "Открыто"} if all_text else {"entity_1": "sensor.temperature"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    await apply(hass, result)
    raw = await export_flow(hass, loaded, files)
    result = await import_flow(hass, loaded, files, raw)
    if not all_text:
        assert result["step_id"] == "import_entities"
        assert result["description_placeholders"]["count"] == "1"
        hass.states.async_set("sensor.other_temperature", "26.5")
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "entity_id": "sensor.other_temperature",
        })
    assert result["step_id"] == "preview" and not result["errors"]
    await apply(hass, result)
    assert loaded.options["screen"]["text_2"] == "sensor.temperature"
    assert loaded.options["screen"]["text_3"] == "Гостиная"
    assert entity_references(loaded.options["screen"]) == ([] if all_text else ["sensor.other_temperature"])


async def test_new_options_translations_are_available(hass, loaded):
    for language in ("en", "ru"):
        values = await translation.async_get_translations(hass, language, "options", {DOMAIN})
        prefix = f"component.{DOMAIN}.options"
        assert values[f"{prefix}.step.value_sources.data.source_3"]
        assert values[f"{prefix}.step.three_values.data.text_1"]
        assert values[f"{prefix}.error.invalid_text"]
