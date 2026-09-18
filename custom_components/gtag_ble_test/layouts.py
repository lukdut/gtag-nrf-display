"""UI presets built from the same text templates used by the draw action."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import Template

from .render import RenderedFrame, render_layout

PRESETS = ("clock", "single_value", "clock_two_values")
DEFAULT_PRESET = "clock_two_values"
DEFAULT_INTERVAL = 5
DECIMAL_PLACES = ("original", "0", "1", "2", "3", "4", "5", "6")

SETTINGS_SCHEMA = vol.Schema({
    vol.Required("preset"): vol.In(PRESETS),
    vol.Optional("update_interval", default=DEFAULT_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
    vol.Optional("stale_after", default=15): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
    **{vol.Optional(f"entity_{index}"): cv.entity_id for index in (1, 2)},
    **{vol.Optional(f"label_{index}", default=""): vol.All(str, vol.Length(max=80)) for index in (1, 2)},
    **{vol.Optional(f"unit_{index}", default=""): vol.All(str, vol.Length(max=16)) for index in (1, 2)},
    **{vol.Optional(f"decimals_{index}", default="original"): vol.In(DECIMAL_PLACES) for index in (1, 2)},
})


class StaleAfterTooShort(vol.Invalid):
    """The freshness lease cannot cover two configured update intervals."""

    def __init__(self, minimum: int) -> None:
        self.minimum = minimum
        super().__init__(f"stale_after must be 0 or at least {minimum} minutes", ["stale_after"])


def validate_timing(settings: dict[str, Any]) -> None:
    """Validate timing before entity selection; round seconds up to minutes."""
    minimum = (2 * int(settings.get("update_interval", DEFAULT_INTERVAL)) + 59) // 60
    stale_after = int(settings.get("stale_after", 15))
    if 0 < stale_after < minimum:
        raise StaleAfterTooShort(minimum)


def normalize_saved_timing(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep previously saved configurations usable with the new timing limit."""
    settings = dict(settings)
    try:
        validate_timing(settings)
    except StaleAfterTooShort as err:
        settings["stale_after"] = err.minimum
    return settings


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    settings = SETTINGS_SCHEMA(settings)
    validate_timing(settings)
    for index in range(1, value_count(settings["preset"]) + 1):
        if not settings.get(f"entity_{index}"):
            raise vol.Invalid(f"entity_{index} is required for this preset", [f"entity_{index}"])
    return settings


def value_count(preset: str) -> int:
    return 2 if preset == "clock_two_values" else 1 if preset == "single_value" else 0


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _label(settings: dict, index: int) -> str:
    entity = _quoted(settings[f"entity_{index}"])
    label = settings.get(f"label_{index}", "").strip()
    if label:
        return "{{ " + _quoted(label) + " }}"
    return "{{ (state_attr(" + entity + ", 'friendly_name') or " + entity + ") | string | truncate(80, True, '…') }}"


def _value(settings: dict, index: int) -> str:
    entity = _quoted(settings[f"entity_{index}"])
    unit = settings.get(f"unit_{index}", "").strip()
    # '-' is an explicit request to hide a native unit; empty means automatic.
    unit_expression = _quoted("" if unit == "-" else unit) if unit else f"(state_attr({entity}, 'unit_of_measurement') or '')"
    decimals = settings.get(f"decimals_{index}", "original")
    formatting = ""
    if decimals != "original":
        # Round numeric states only. Adding zero avoids a displayed '-0.0'.
        formatting = (
            "{% if is_number(value) %}{% set value = " + _quoted(f"%.{decimals}f")
            + f" | format((value | float | round({decimals})) + 0.0) %}}"
            "{% endif %}"
        )
    return (
        "{% set value = states(" + entity + ") %}"
        "{% if value in ['unknown', 'unavailable'] %}—{% else %}"
        + formatting +
        "{{ value | truncate(128, True, '…') }}"
        "{% set unit = " + unit_expression + " %}"
        "{% if unit %} {{ unit | string | truncate(16, True, '…') }}{% endif %}"
        "{% endif %}"
    )


def preset_layout(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a persistent template layout. No HA states are read here."""
    settings = validate_settings(settings)
    text = lambda x, y, value, size, width, align="left": {
        "type": "text", "x": x, "y": y, "text": value, "size": size,
        "max_width": width, "align": align,
    }
    line = lambda x, y, x2, y2: {"type": "line", "x": x, "y": y, "x2": x2, "y2": y2}
    if settings["preset"] == "single_value":
        elements = [
            text(128, 10, _label(settings, 1), 20, 240, "center"),
            line(8, 38, 247, 38),
            text(128, 64, _value(settings, 1), 44, 240, "center"),
        ]
    elif settings["preset"] == "clock_two_values":
        elements = [
            text(8, 13, "{{ now().strftime('%H:%M') }}", 28, 112),
            text(248, 23, "{{ now().strftime('%d.%m.%Y') }}", 16, 122, "right"),
            line(8, 42, 247, 42), line(128, 53, 128, 119),
            text(8, 54, _label(settings, 1), 16, 112),
            text(140, 54, _label(settings, 2), 16, 108),
            text(8, 84, _value(settings, 1), 30, 112),
            text(140, 84, _value(settings, 2), 30, 108),
        ]
    else:
        # The existing dedicated clock renderer is used for this mode.
        raise ValueError("Use clock_layout for the clock preset")
    return {"elements": elements}


async def async_render_layout(hass: HomeAssistant, layout: dict[str, Any]) -> RenderedFrame:
    """Resolve templates on HA's loop and draw in the executor."""
    layout = deepcopy(layout)
    for element in layout["elements"]:
        if element["type"] == "text":
            element["text"] = Template(element["text"], hass).async_render(parse_result=False)
    return await hass.async_add_executor_job(render_layout, layout)
