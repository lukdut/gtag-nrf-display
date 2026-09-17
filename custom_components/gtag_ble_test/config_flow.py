from __future__ import annotations

from typing import Any
import logging
import secrets
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_UUID
from .layouts import DEFAULT_INTERVAL, DEFAULT_PRESET, PRESETS, async_render_layout, preset_layout, validate_settings, value_count
from .render import clock_layout, preview_svg

_LOGGER = logging.getLogger(__name__)


class GTagBLETestConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GTagOptionsFlow()

    def __init__(self) -> None:
        self._info: BluetoothServiceInfoBleak | None = None
        self._devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._info is not None

        if user_input is not None:
            return self.async_create_entry(
                title=self._info.name or self._info.address,
                data={CONF_ADDRESS: self._info.address},
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured = self._async_current_ids(include_ignore=False)

        for info in async_discovered_service_info(self.hass):
            uuids = {uuid.lower() for uuid in info.service_uuids}
            if (
                SERVICE_UUID not in uuids
                or info.address in configured
                or info.address in self._devices
            ):
                continue
            self._devices[info.address] = info

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            info = self._devices[address]

            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=info.name or info.address,
                data={CONF_ADDRESS: address},
            )

        if not self._devices:
            return self.async_abort(reason="no_devices_found")

        choices = {
            address: f"{info.name or 'GTag Display'} ({address})"
            for address, info in self._devices.items()
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(choices)}
            ),
        )


class GTagOptionsFlow(OptionsFlow):
    """Choose a preset, inspect local pixels, then explicitly apply it."""

    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None
        self._preview = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._settings is None:
            self._settings = {
                "preset": DEFAULT_PRESET, "update_interval": DEFAULT_INTERVAL,
                **self.config_entry.options.get("screen", {}),
            }
        schema = vol.Schema({
            vol.Required("preset", default=self._settings["preset"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(PRESETS), translation_key="preset",
                                              mode=selector.SelectSelectorMode.DROPDOWN),
            ),
            vol.Required("update_interval", default=self._settings["update_interval"]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=3600, step=1,
                                              mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s"),
            ),
        })
        errors = {}
        if user_input is not None:
            try:
                self._settings.update(schema(user_input))
                self._settings["update_interval"] = int(self._settings["update_interval"])
            except vol.Invalid:
                errors["base"] = "invalid_settings"
            else:
                if value_count(self._settings["preset"]):
                    return await self.async_step_values()
                return await self.async_step_preview()
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors, last_step=False)

    async def async_step_values(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        registry = er.async_get(self.hass)
        excluded = [item.entity_id for item in registry.entities.values()
                    if item.platform == DOMAIN and not (
                        item.domain == "sensor" and item.translation_key == "battery_voltage"
                    )]
        fields = {}
        count = value_count(self._settings["preset"])
        for index in range(1, count + 1):
            fields[vol.Required(f"entity_{index}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(exclude_entities=excluded),
            )
            fields[vol.Optional(f"label_{index}")] = selector.TextSelector()
            fields[vol.Optional(f"unit_{index}")] = selector.TextSelector()
        schema = vol.Schema(fields)
        errors = {}
        if user_input is not None:
            candidate = dict(self._settings)
            for index in range(1, count + 1):
                for field in ("entity", "label", "unit"):
                    candidate[f"{field}_{index}"] = user_input.get(f"{field}_{index}", "")
                entity_id = candidate[f"entity_{index}"]
                if entity_id in excluded:
                    errors[f"entity_{index}"] = "invalid_entity"
                elif not self.hass.states.get(entity_id) and not registry.async_get(entity_id):
                    errors[f"entity_{index}"] = "entity_not_found"
            try:
                candidate = validate_settings(candidate)
            except vol.Invalid as err:
                errors[str(err.path[0]) if err.path else "base"] = "invalid_settings"
            if not errors:
                self._settings = candidate
                return await self.async_step_preview()
            suggestions = candidate
        else:
            suggestions = self._settings
        return self.async_show_form(
            step_id="values", data_schema=self.add_suggested_values_to_schema(schema, suggestions),
            errors=errors, last_step=False,
        )

    async def async_step_preview(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get("action") == "edit":
                return await self.async_step_init()
            if user_input.get("action") == "apply" and self._preview:
                return self.async_create_entry(data={
                    "screen": validate_settings(self._settings),
                    # Applying the same preset again must also replace a later
                    # draw action; HA otherwise ignores identical options.
                    "screen_revision": secrets.token_hex(8),
                })
        errors = {}
        try:
            self._settings = validate_settings(self._settings)
            layout = (clock_layout(dt_util.now()) if self._settings["preset"] == "clock"
                      else preset_layout(self._settings))
            frame = await async_render_layout(self.hass, layout)
            self._preview = await self.hass.async_add_executor_job(preview_svg, frame.raw)
        except (HomeAssistantError, vol.Invalid, ValueError, OSError):
            _LOGGER.exception("Unable to render GTag layout preview")
            errors["base"] = "render_failed"
            self._preview = ""
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema({
                vol.Required("action", default="apply"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["apply", "edit", "refresh"], translation_key="preview_action",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    ),
                ),
            }),
            description_placeholders={"preview": self._preview}, errors=errors, last_step=True,
        )
