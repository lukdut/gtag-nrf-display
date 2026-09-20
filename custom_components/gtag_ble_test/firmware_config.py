"""Validate hardware choices and render a self-contained ESPHome entry point.

Keep this module independent of HA and ESPHome so their test suites can validate
exactly the YAML produced by the wizard against the released component schema.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

# nRF52840 firmware is unchanged. Keep its wizard on the tested firmware tag;
# the ESP32 profile has a separate release pin.
FIRMWARE_TAG = "v0.9.0"
WIFI_FIRMWARE_TAG = "v1.1.0"
PACKAGE_ROOT = "github://lukdut/gtag-nrf-display/config/esphome/packages"
LCD_PINS = ("dio_pin", "clk_pin", "cs_pin", "reset_pin")
RESERVED_GPIO = {0, 1, 9, 10, 18}
ADC_GPIO = {2, 3, 4, 5, 28, 29, 30, 31}
BOARDS = {
    "esp32c3_supermini": {
        "pins": ("GPIO0", "GPIO1", "GPIO3", "GPIO4"),
        "battery_enabled": False,
    },
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
    if result.get("transport") not in ("ble", "zigbee", "wifi"):
        errors["transport"] = "invalid_transport"
    elif result.get("board") == "super52840" and result["transport"] != "zigbee":
        errors["transport"] = "unsupported_profile"
    elif (result.get("board") == "esp32c3_supermini") != (result["transport"] == "wifi"):
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
    wifi = result.get("board") == "esp32c3_supermini"
    if wifi and result.get("battery_enabled"):
        errors["battery_enabled"] = "usb_power_only"
    fields = (*LCD_PINS, "battery_pin") if battery and result.get("battery_enabled") else LCD_PINS
    for field in fields:
        try:
            if wifi:
                match = re.fullmatch(r"GPIO(\d{1,2})", str(result.get(field, "")).strip().upper())
                if match is None or int(match[1]) not in (0, 1, 3, 4, 5, 6, 7, 10, 20, 21):
                    raise ValueError("invalid_esp32_gpio")
                number = int(match[1])
            else:
                number = gpio_number(result.get(field))
        except ValueError as err:
            errors[field] = str(err)
            continue
        result[field] = f"GPIO{number}" if wifi else f"P{number // 32}.{number % 32:02d}"
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
    if settings["transport"] == "wifi":
        settings = validate_wifi(settings)
        return "\n".join([
            "# GTag Display / ESP32-C3 Super Mini — ESPHome 2026.9.0, USB power.",
            "# Private configuration: contains Wi-Fi, API and OTA credentials.",
            "# Keep the original DisplayCLK/S1 clock and common ground connected.",
            "substitutions:", f"  gtag_name: {quote(settings['name'])}",
            f"  gtag_friendly_name: {quote(settings['friendly_name'])}", "", "packages:",
            f"  gtag: {PACKAGE_ROOT}/wifi.yaml@{WIFI_FIRMWARE_TAG}", "", "wifi:",
            f"  ssid: {quote(settings['wifi_ssid'])}", f"  password: {quote(settings['wifi_password'])}",
            '  # If Wi-Fi fails with "Auth Expired", uncomment both options below,',
            "  # rebuild and flash via USB. Lower TX power may reduce range.",
            "  # power_save_mode: none",
            "  # output_power: 8.5dB",
            "", "api:", "  encryption:", f"    key: {quote(settings['api_key'])}", "", "ota:",
            "  - platform: esphome", f"    password: {quote(settings['ota_password'])}", "", "gtag_display:",
            *[f"  {field}: {settings[field]}" for field in LCD_PINS],
            "  battery_voltage:", "    enabled: false", "",
            "# Add light:, sensor:, binary_sensor:, etc. here; these are standard ESPHome components.", "",
        ])
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


def validate_wifi(settings: dict) -> dict:
    import base64
    result = dict(settings)
    errors = {}
    for key, minimum, maximum in (("wifi_ssid", 1, 32), ("wifi_password", 8, 63), ("ota_password", 8, 64)):
        value = result.get(key, "")
        if (not isinstance(value, str) or not minimum <= len(value.encode("utf-8")) <= maximum
                or "${" in value or any(ord(char) < 32 for char in value)):
            errors[key] = "invalid_wifi_settings"
    try:
        key = base64.b64decode(result.get("api_key", ""), validate=True)
        if len(key) != 32 or key == bytes(32):
            raise ValueError
    except (ValueError, TypeError):
        errors["api_key"] = "invalid_wifi_settings"
    if errors:
        raise FirmwareConfigError(errors)
    return result
