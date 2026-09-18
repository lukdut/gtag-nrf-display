"""Versioned portable layouts and simultaneous remapping of entity references."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
import re

from jinja2 import Environment, TemplateError
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .layouts import DEFAULT_INTERVAL, normalize_saved_timing, preset_layout, validate_settings, value_count
from .render import LAYOUT_SCHEMA

FORMAT = "gtag-display-layout"
VERSION = 1
MAX_FILE_BYTES = 256 * 1024
ENTITY_ID = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
_ENV = Environment(extensions=["jinja2.ext.loopcontrols", "jinja2.ext.do"])


class LayoutFileError(ValueError):
    """A localized import/export error key."""


def _references(text: str) -> tuple[str, list[tuple[int, int, str, bool]]]:
    """Locate complete entity literals and states.domain.object expressions.

    Leave static display text, comments, raw blocks and partial IDs alone. Keep
    source spans to preserve Jinja whitespace controls and formatting exactly.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    _ENV.parse(text)
    tokens = []
    cursor = 0
    references = []
    for _line, kind, value in _ENV.lex(text):
        start = text.find(value, cursor)
        if start < 0:
            raise LayoutFileError("invalid_layout_file")
        cursor = start + len(value)
        if kind == "string":
            entity = ast.literal_eval(value)
            if isinstance(entity, str) and ENTITY_ID.fullmatch(entity):
                references.append((start, cursor, entity, False))
        if kind != "whitespace":
            tokens.append((kind, value, start, cursor))
    for index in range(len(tokens) - 4):
        part = tokens[index:index + 5]
        if ([item[0] for item in part] == ["name", "operator", "name", "operator", "name"]
                and part[0][1] == "states" and part[1][1] == part[3][1] == "."
                and (index == 0 or tokens[index - 1][1] != ".")):
            entity = f"{part[2][1]}.{part[4][1]}"
            if ENTITY_ID.fullmatch(entity):
                references.append((part[0][2], part[4][3], entity, True))
    return text, sorted(references)


def entity_references(settings: dict) -> list[str]:
    if settings["preset"] != "custom":
        return list(dict.fromkeys(settings[f"entity_{i}"] for i in range(1, value_count(settings["preset"]) + 1)))
    entities = []
    for element in settings["layout"]["elements"]:
        if element["type"] == "text":
            entities.extend(item[2] for item in _references(element["text"])[1])
    return list(dict.fromkeys(entities))


def remap_settings(settings: dict, mapping: dict[str, str]) -> dict:
    result = deepcopy(validate_settings(settings))
    if set(mapping) != set(entity_references(result)):
        raise LayoutFileError("invalid_entity_mapping")
    mapping = {source: cv.entity_id(target) for source, target in mapping.items()}
    if result["preset"] != "custom":
        for index in range(1, value_count(result["preset"]) + 1):
            result[f"entity_{index}"] = mapping[result[f"entity_{index}"]]
    else:
        for element in result["layout"]["elements"]:
            if element["type"] != "text":
                continue
            text, references = _references(element["text"])
            for start, end, source, dotted in reversed(references):
                if mapping[source] != source:
                    value = json.dumps(mapping[source], ensure_ascii=False)
                    text = text[:start] + (f"states[{value}]" if dotted else value) + text[end:]
            element["text"] = text
    return validate_settings(result)


def _canonical_settings(settings: dict) -> dict:
    settings = validate_settings(settings)
    # Do not share stale entity slots left behind by switching UI presets.
    result = {key: settings[key] for key in ("preset", "update_interval", "stale_after")}
    if settings["preset"] == "custom":
        result.update(layout=settings["layout"], auto_update=settings["auto_update"])
    else:
        for index in range(1, value_count(settings["preset"]) + 1):
            for field in ("entity", "label", "unit", "decimals"):
                result[f"{field}_{index}"] = settings[f"{field}_{index}"]
    return result


def export_document(settings: dict, name: str) -> dict:
    settings = _canonical_settings(settings)
    name = vol.All(str, vol.Length(min=1, max=80))(name.strip())
    # Entity IDs are the only installation-specific references. No addresses,
    # device/config-entry IDs, current states or rendered images are exported.
    return {"format": FORMAT, "version": VERSION, "name": name, "screen": settings}


def encode_document(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def decode_document(raw: bytes) -> dict:
    if len(raw) > MAX_FILE_BYTES:
        raise LayoutFileError("layout_file_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(value)

    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                              parse_constant=invalid_constant)
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise LayoutFileError("invalid_layout_file")
        if type(document.get("version")) is not int or document["version"] != VERSION:
            raise LayoutFileError("unsupported_layout_version")
        if set(document) != {"format", "version", "name", "screen"}:
            raise LayoutFileError("invalid_layout_file")
        result = export_document(document["screen"], document["name"])
        if len(entity_references(result["screen"])) > 64:
            raise LayoutFileError("invalid_layout_file")
        return result
    except LayoutFileError:
        raise
    except (vol.Invalid, ValueError, TypeError, KeyError, AttributeError, RecursionError, TemplateError, SyntaxError) as err:
        raise LayoutFileError("invalid_layout_file") from err


async def current_screen(hass, entry) -> dict:
    """Export the current intent, including Draw overrides and unloaded entries."""
    settings = normalize_saved_timing(dict(entry.options.get("screen", {})))
    display = getattr(entry, "runtime_data", None)
    if display is not None and not display._closed:
        clock, layout, auto_update = display.clock_enabled, display.last_layout, display.auto_update
        timing = {"update_interval": int(display._configured_interval or DEFAULT_INTERVAL),
                  "stale_after": display.freshness_timeout // 60}
    else:
        saved = await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_load() or {}
        if not isinstance(saved, dict):
            saved = {}
        if settings and saved.get("options_revision") != entry.options.get("screen_revision"):
            return _canonical_settings(settings)
        clock, layout, auto_update = saved.get("clock_enabled"), saved.get("layout"), saved.get("auto_update", True)
        timing = {"update_interval": settings.get("update_interval", DEFAULT_INTERVAL),
                  "stale_after": settings.get("stale_after", 15)}
    if clock:
        return _canonical_settings({"preset": "clock", **timing})
    if layout is None:
        raise LayoutFileError("no_layout_to_export")
    layout = LAYOUT_SCHEMA(deepcopy(layout))
    if settings and settings.get("preset") != "clock":
        if layout == LAYOUT_SCHEMA(preset_layout(settings)) and auto_update == settings.get("auto_update", True):
            return _canonical_settings({**settings, **timing})
    return _canonical_settings({"preset": "custom", "layout": layout, "auto_update": auto_update, **timing})
