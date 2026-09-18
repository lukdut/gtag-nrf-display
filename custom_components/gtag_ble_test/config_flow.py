from __future__ import annotations

from typing import Any
import asyncio
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
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .const import CONF_TRANSPORT, DOMAIN, SERVICE_UUID, TRANSPORT_BLE, TRANSPORT_ZIGBEE
from .firmware_flow import FirmwareWizardMixin
from .layouts import (
    DECIMAL_PLACES, DEFAULT_INTERVAL, DEFAULT_PRESET, PRESETS, StaleAfterTooShort,
    async_render_layout, normalize_saved_timing, preset_layout,
    validate_settings, validate_timing, value_count,
)
from .render import clock_layout, preview_svg

_LOGGER = logging.getLogger(__name__)


class GTagBLETestConfigFlow(FirmwareWizardMixin, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GTagOptionsFlow()

    def __init__(self) -> None:
        self._info: BluetoothServiceInfoBleak | None = None
        self._devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._zigbee_devices: dict[str, dict] = {}
        self._base_topic = "zigbee2mqtt"

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
        return self.async_show_menu(step_id="user", menu_options=["ble", "zigbee", "firmware"])

    async def async_step_zigbee(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from homeassistant.components import mqtt
        from .zigbee import async_discover, valid_base_topic

        errors = {}
        if user_input is not None:
            try:
                self._base_topic = valid_base_topic(user_input["base_topic"])
            except ValueError:
                errors["base_topic"] = "invalid_topic"
            else:
                try:
                    async with asyncio.timeout(15):
                        ready = await mqtt.async_wait_for_mqtt_client(self.hass)
                    if not ready:
                        errors["base"] = "mqtt_not_ready"
                    else:
                        found = await async_discover(self.hass, self._base_topic)
                        configured = self._async_current_ids(include_ignore=False)
                        found = {ieee: device for ieee, device in found.items() if f"zigbee:{ieee}" not in configured}
                        self._zigbee_devices = {ieee: device for ieee, device in found.items() if device["compatible"]}
                        if self._zigbee_devices:
                            return await self.async_step_zigbee_device()
                        errors["base"] = "converter_update_required" if found else "no_zigbee_devices"
                except (TimeoutError, HomeAssistantError):
                    errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="zigbee", data_schema=vol.Schema({
            vol.Required("base_topic", default=self._base_topic): str,
        }), errors=errors)

    async def async_step_zigbee_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            device = self._zigbee_devices[address]
            await self.async_set_unique_id(f"zigbee:{address}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=device["friendly_name"], data={
                CONF_TRANSPORT: TRANSPORT_ZIGBEE, CONF_ADDRESS: address,
                "base_topic": self._base_topic, "friendly_name": device["friendly_name"],
                "battery_supported": device["battery_supported"],
            })
        return self.async_show_form(step_id="zigbee_device", data_schema=vol.Schema({
            vol.Required(CONF_ADDRESS): vol.In({
                ieee: f"{device['friendly_name']} ({ieee})" for ieee, device in self._zigbee_devices.items()
            }),
        }))

    async def async_step_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not await async_setup_component(self.hass, "bluetooth", {}):
            return self.async_abort(reason="bluetooth_not_ready")
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
                data={CONF_ADDRESS: address, CONF_TRANSPORT: TRANSPORT_BLE},
            )

        if not self._devices:
            return self.async_abort(reason="no_devices_found")

        choices = {
            address: f"{info.name or 'GTag Display'} ({address})"
            for address, info in self._devices.items()
        }

        return self.async_show_form(
            step_id="ble",
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
            self._settings = normalize_saved_timing({
                "preset": DEFAULT_PRESET, "update_interval": DEFAULT_INTERVAL, "stale_after": 15,
                **self.config_entry.options.get("screen", {}),
            })
        schema = vol.Schema({
            vol.Required("preset", default=self._settings["preset"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(PRESETS), translation_key="preset",
                                              mode=selector.SelectSelectorMode.DROPDOWN),
            ),
            vol.Required("update_interval", default=self._settings["update_interval"]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=3600, step=1,
                                              mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s"),
            ),
            vol.Optional("stale_after", default=self._settings["stale_after"]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=1440, step=1,
                                              mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"),
            ),
        })
        errors = {}
        placeholders = {}
        if user_input is not None:
            try:
                self._settings.update(schema(user_input))
                self._settings["update_interval"] = int(self._settings["update_interval"])
                self._settings["stale_after"] = int(self._settings["stale_after"])
                validate_timing(self._settings)
            except StaleAfterTooShort as err:
                errors["stale_after"] = "stale_after_too_short"
                placeholders["minimum"] = str(err.minimum)
            except vol.Invalid:
                errors["base"] = "invalid_settings"
            else:
                if value_count(self._settings["preset"]):
                    return await self.async_step_values()
                return await self.async_step_preview()
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, self._settings),
            errors=errors, description_placeholders=placeholders, last_step=False,
        )

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
            fields[vol.Optional(f"decimals_{index}", default="original")] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(DECIMAL_PLACES), translation_key="decimal_places",
                                              mode=selector.SelectSelectorMode.DROPDOWN),
            )
        schema = vol.Schema(fields)
        errors = {}
        if user_input is not None:
            candidate = dict(self._settings)
            for index in range(1, count + 1):
                for field in ("entity", "label", "unit"):
                    candidate[f"{field}_{index}"] = user_input.get(f"{field}_{index}", "")
                candidate[f"decimals_{index}"] = user_input.get(f"decimals_{index}", "original")
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
