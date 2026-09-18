"""Prepare firmware before a physical display is paired with Home Assistant."""
from __future__ import annotations

from html import escape
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .firmware_config import (
    BOARDS, FIRMWARE_TAG, LCD_PINS, FirmwareConfigError, hardware_defaults,
    render_firmware_yaml, validate_battery, validate_device, validate_pins,
)
from .firmware_download import async_prepare_download


def select(options: list[str], translation_key: str):
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=options, translation_key=translation_key, mode=selector.SelectSelectorMode.DROPDOWN,
    ))


def number(low: float, high: float, step: float = 0.001, unit: str | None = None):
    config = {"min": low, "max": high, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


class FirmwareWizardMixin:
    """A config-flow branch which never creates a dummy device or config entry."""

    _firmware_settings: dict[str, Any] | None = None

    async def async_step_firmware(self, user_input: dict | None = None) -> ConfigFlowResult:
        if self._firmware_settings is None:
            self._firmware_settings = {
                "board": "promicro", "transport": "zigbee",
                "name": f"gtag-display-{secrets.token_hex(2)}", "friendly_name": "GTag Display",
                **hardware_defaults("promicro"),
            }
        settings = self._firmware_settings
        errors = {}
        if user_input is not None:
            try:
                candidate = validate_device({**settings, **user_input})
            except FirmwareConfigError as err:
                errors = err.errors
            else:
                if candidate["board"] != settings["board"]:
                    candidate.update(hardware_defaults(candidate["board"]))
                self._firmware_settings = candidate
                return await self.async_step_firmware_pins()
        schema = vol.Schema({
            vol.Required("board"): select(list(BOARDS), "firmware_board"),
            vol.Required("transport"): select(["zigbee", "ble"], "firmware_transport"),
            vol.Required("name"): selector.TextSelector(),
            vol.Required("friendly_name"): selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="firmware", data_schema=self.add_suggested_values_to_schema(schema, user_input or settings),
            errors=errors, last_step=False,
        )

    async def async_step_firmware_pins(self, user_input: dict | None = None) -> ConfigFlowResult:
        settings = self._firmware_settings
        errors = {}
        if user_input is not None:
            try:
                self._firmware_settings = validate_pins({**settings, **user_input})
            except FirmwareConfigError as err:
                errors = err.errors
            else:
                if self._firmware_settings["battery_enabled"]:
                    return await self.async_step_firmware_battery()
                return await self.async_step_firmware_download()
        schema = vol.Schema({
            **{vol.Required(field): selector.TextSelector() for field in LCD_PINS},
            vol.Required("battery_enabled"): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="firmware_pins", data_schema=self.add_suggested_values_to_schema(schema, user_input or settings),
            errors=errors, last_step=False,
        )

    async def async_step_firmware_battery(self, user_input: dict | None = None) -> ConfigFlowResult:
        settings = self._firmware_settings
        errors = {}
        if user_input is not None:
            try:
                self._firmware_settings = validate_battery({**settings, **user_input})
            except FirmwareConfigError as err:
                errors = err.errors
            else:
                return await self.async_step_firmware_download()
        schema = vol.Schema({
            vol.Required("battery_pin"): selector.TextSelector(),
            vol.Required("calibration"): number(0.8, 1.2),
            vol.Required("empty_voltage"): number(2.5, 4.5, unit="V"),
            vol.Required("full_voltage"): number(2.5, 4.5, unit="V"),
            vol.Required("recovery_voltage"): number(2.5, 4.5, unit="V"),
            vol.Required("indicator"): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="firmware_battery", data_schema=self.add_suggested_values_to_schema(schema, user_input or settings),
            errors=errors, last_step=False,
        )

    async def async_step_firmware_download(self, user_input: dict | None = None) -> ConfigFlowResult:
        action = user_input.get("action") if user_input is not None else None
        if action == "edit":
            return await self.async_step_firmware()
        yaml = render_firmware_yaml(self._firmware_settings)
        filename = f"{self._firmware_settings['name']}.yaml"
        errors = {}
        try:
            url = await async_prepare_download(
                self.hass, self.flow_id, filename, yaml, finished=action == "finish",
            )
        except HomeAssistantError:
            # The visible YAML still permits copying when HTTP could not start.
            url = ""
            errors["base"] = "firmware_download_unavailable"
        placeholders = {
            "download_url": url, "filename": filename, "yaml": yaml, "firmware_tag": FIRMWARE_TAG,
            # HA parses translations as ICU messages before rendering Markdown.
            # HTML attributes must arrive as values, not as translation syntax.
            "download_link_start": f'<a href="{escape(url, quote=True)}" target="_blank">' if url else "",
            "download_link_end": "</a>" if url else "",
        }
        if action == "finish" and not errors:
            return self.async_abort(reason="firmware_ready", description_placeholders=placeholders)
        return self.async_show_form(
            step_id="firmware_download", data_schema=vol.Schema({
                vol.Required("action", default="finish"): select(["finish", "edit", "refresh"], "firmware_action"),
            }), errors=errors, description_placeholders=placeholders, last_step=True,
        )
