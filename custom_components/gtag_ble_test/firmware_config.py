"""Validate hardware choices and render a self-contained ESPHome entry point.

Keep this module independent of HA and ESPHome so their test suites can validate
exactly the YAML produced by the wizard against the released component schema.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

# Firmware is unchanged in HA 1.0.0. Keep the wizard on the tested, published
# firmware tag so YAML also builds before the next HA release is published.
FIRMWARE_TAG = "v0.9.0"
PACKAGE_ROOT = "github://lukdut/gtag-nrf-display/config/esphome/packages"
LCD_PINS = ("dio_pin", "clk_pin", "cs_pin", "reset_pin")
RESERVED_GPIO = {0, 1, 9, 10, 18}
ADC_GPIO = {2, 3, 4, 5, 28, 29, 30, 31}
BOARDS = {
    "promicro": {
        "bootloader": "adafruit_nrf52_sd140_v6",
        "pins": ("P0.11", "P1.04", "P1.06", "P1.13"),
        "battery_enabled": True,
    },
    "super52840": {
        "bootloader": "adafruit_nrf52_sd140_v7",
        "pins": ("P1.15", "P1.13", "P1.14", "P1.12"),
        "battery_enabled": False,
    },
}


class FirmwareConfigError(ValueError):
    """Field names mapped to translated config-flow error keys."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


def hardware_defaults(board: str) -> dict[str, Any]:
    return {
        **dict(zip(LCD_PINS, BOARDS[board]["pins"], strict=True)),
        "battery_enabled": BOARDS[board]["battery_enabled"],
        "battery_pin": "P0.31", "calibration": 1.0,
        "empty_voltage": 3.306, "full_voltage": 4.19,
        "recovery_voltage": 3.45, "indicator": True,
    }


def validate_device(settings: dict) -> dict:
    result = dict(settings)
    errors = {}
    if result.get("board") not in BOARDS:
        errors["board"] = "invalid_board"
    if result.get("transport") not in ("ble", "zigbee"):
        errors["transport"] = "invalid_transport"
    elif result.get("board") == "super52840" and result["transport"] != "zigbee":
        errors["transport"] = "unsupported_profile"
    name = result.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?", name):
        errors["name"] = "invalid_device_name"
    friendly = result.get("friendly_name", "")
    if (not isinstance(friendly, str) or not 1 <= len(friendly.strip()) <= 80
            or any(ord(char) < 32 for char in friendly) or "${" in friendly):
        errors["friendly_name"] = "invalid_friendly_name"
    else:
        result["friendly_name"] = friendly.strip()
    if errors:
        raise FirmwareConfigError(errors)
    return result


def gpio_number(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("invalid_gpio")
    match = re.fullmatch(r"P([01])\.(\d{1,2})", value.strip().upper())
    if match is None or int(match[2]) >= (32 if match[1] == "0" else 16):
        raise ValueError("invalid_gpio")
    number = int(match[1]) * 32 + int(match[2])
    if number in RESERVED_GPIO:
        raise ValueError("reserved_gpio")
    return number


def validate_pins(settings: dict, *, battery: bool = False) -> dict:
    result = dict(settings)
    errors = {}
    if not isinstance(result.get("battery_enabled"), bool):
        errors["battery_enabled"] = "invalid_settings"
    used = set()
    fields = (*LCD_PINS, "battery_pin") if battery and result.get("battery_enabled") else LCD_PINS
    for field in fields:
        try:
            number = gpio_number(result.get(field))
        except ValueError as err:
            errors[field] = str(err)
            continue
        result[field] = f"P{number // 32}.{number % 32:02d}"
        if number in used:
            errors[field] = "duplicate_gpio"
        elif field == "battery_pin" and number not in ADC_GPIO:
            errors[field] = "invalid_adc_gpio"
        used.add(number)
    if errors:
        raise FirmwareConfigError(errors)
    return result


def validate_battery(settings: dict) -> dict:
    result = validate_pins(settings, battery=True)
    if not result["battery_enabled"]:
        return result
    errors = {}
    for field, low, high in (("calibration", 0.8, 1.2), ("empty_voltage", 2.5, 4.5),
                             ("full_voltage", 2.5, 4.5), ("recovery_voltage", 2.5, 4.5)):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            errors[field] = "invalid_settings"
        else:
            result[field] = float(value)
    if not isinstance(result.get("indicator"), bool):
        errors["indicator"] = "invalid_settings"
    if not errors:
        empty, full, recovery = (round(result[field] * 1000) for field in
                                 ("empty_voltage", "full_voltage", "recovery_voltage"))
        if full <= empty:
            errors["full_voltage"] = "invalid_full_voltage"
        if not empty < recovery <= full:
            errors["recovery_voltage"] = "invalid_recovery_voltage"
    if errors:
        raise FirmwareConfigError(errors)
    return result


def render_firmware_yaml(settings: dict) -> str:
    settings = validate_battery(validate_device(settings))
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [
        "# GTag Display — generated for ESPHome Device Builder 2026.9.0.",
        "# Keep the display's original power and DisplayCLK/S1 clock connected.",
        "substitutions:", f"  gtag_name: {quote(settings['name'])}",
        f"  gtag_friendly_name: {quote(settings['friendly_name'])}", "", "packages:",
        f"  gtag: {PACKAGE_ROOT}/{settings['transport']}.yaml@{FIRMWARE_TAG}",
    ]
    if settings["transport"] == "zigbee" and not settings["battery_enabled"]:
        lines.append(f"  no_battery: {PACKAGE_ROOT}/zigbee-no-battery.yaml@{FIRMWARE_TAG}")
    lines += ["", "nrf52:", "  board: adafruit_itsybitsy_nrf52840",
              f"  bootloader: {BOARDS[settings['board']]['bootloader']}", "  dcdc: false",
              "  framework:", "    version: 2.9.2", "", "gtag_display:"]
    lines += [f"  {field}: {settings[field]}" for field in LCD_PINS]
    lines += ["  battery_voltage:", f"    enabled: {str(settings['battery_enabled']).lower()}"]
    if settings["battery_enabled"]:
        lines += ["    # External divider: B+ -- 1M -- ADC -- 1M -- GND; ADC -- 100nF -- GND.",
                  f"    pin: {settings['battery_pin']}", f"    calibration: {settings['calibration']:g}"]
        lines += [f"    {field}: {round(settings[field] * 1000) / 1000:g}V" for field in
                  ("empty_voltage", "full_voltage", "recovery_voltage")]
        lines += [f"    indicator: {str(settings['indicator']).lower()}"]
    return "\n".join(lines) + "\n"
