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

from .const import CONF_TRANSPORT, DOMAIN, SERVICE_UUID, TRANSPORT_BLE, TRANSPORT_ZIGBEE, TRANSPORT_WIFI
from .firmware_flow import FirmwareWizardMixin
from .diagnostic_flow import DiagnosticFlowMixin
from .layout_flow import LayoutTransferMixin, excluded_entities
from .layouts import (
    DECIMAL_PLACES, DEFAULT_INTERVAL, DEFAULT_PRESET, PRESETS, VALUE_SOURCES, StaleAfterTooShort,
    async_render_layout, is_text_value, normalize_saved_timing, preset_layout,
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
        return self.async_show_menu(step_id="user", menu_options=["ble", "zigbee", "wifi", "firmware"])

    async def async_step_wifi(self, user_input=None) -> ConfigFlowResult:
        from types import SimpleNamespace
        from .wifi import WifiTransport, available_devices

        configured = self._async_current_ids(include_ignore=False)
        devices = {key: entry for key, entry in available_devices(self.hass).items()
                   if f"wifi:{entry.unique_id}" not in configured}
        errors = {}
        if user_input is not None:
            linked = devices.get(user_input["esphome_entry_id"])
            if linked is None:
                errors["base"] = "no_wifi_devices"
            else:
                data = {CONF_TRANSPORT: TRANSPORT_WIFI, CONF_ADDRESS: linked.unique_id,
                        "esphome_entry_id": linked.entry_id}
                try:
                    await WifiTransport(self.hass, SimpleNamespace(data=data)).async_check_connection()
                except (HomeAssistantError, TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"wifi:{linked.unique_id}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=linked.title, data=data)
        if not devices and not errors:
            errors["base"] = "no_wifi_devices"
        return self.async_show_form(step_id="wifi", data_schema=vol.Schema({
            vol.Required("esphome_entry_id"): vol.In({key: entry.title for key, entry in devices.items()}),
        }), errors=errors)

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


class GTagOptionsFlow(DiagnosticFlowMixin, LayoutTransferMixin, OptionsFlow):
    """Choose a preset, inspect local pixels, then explicitly apply it."""

    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None
        self._preview = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["configure", "export_layout", "import_layout", "diagnostics"])

    async def async_step_configure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._settings is None:
            self._settings = normalize_saved_timing({
                "preset": DEFAULT_PRESET, "update_interval": DEFAULT_INTERVAL, "stale_after": 15,
                **self.config_entry.options.get("screen", {}),
            })
        schema = vol.Schema({
            vol.Required("preset", default=self._settings["preset"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(PRESETS) + (["custom"] if self._settings["preset"] == "custom" else []), translation_key="preset",
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
                if self._settings["preset"] == "three_values":
                    return await self.async_step_value_sources()
                if value_count(self._settings["preset"]):
                    return await self.async_step_values()
                return await self.async_step_preview()
        return self.async_show_form(
            step_id="configure", data_schema=self.add_suggested_values_to_schema(schema, self._settings),
            errors=errors, description_placeholders=placeholders, last_step=False,
        )

    async def async_step_value_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({
            vol.Required(f"source_{index}", default=self._settings.get(f"source_{index}", "entity")):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=list(VALUE_SOURCES), translation_key="value_source",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ))
            for index in (3, 1, 2)
        })
        if user_input is not None:
            self._settings.update(schema(user_input))
            return await self.async_step_three_values()
        return self.async_show_form(
            step_id="value_sources", data_schema=schema, last_step=False,
        )

    async def async_step_three_values(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_values(user_input)

    async def async_step_values(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        registry = er.async_get(self.hass)
        excluded = excluded_entities(self.hass)
        fields = {}
        count = value_count(self._settings["preset"])
        weather = self._settings["preset"] == "weather"
        graph = self._settings["preset"] == "value_graph"
        three_values = self._settings["preset"] == "three_values"
        domains = ["weather"] if weather else ["sensor", "input_number", "number"] if graph else None
        for index in ((3, 1, 2) if three_values else range(1, count + 1)):
            if is_text_value(self._settings, index):
                if index != 3:
                    fields[vol.Optional(f"label_{index}")] = selector.TextSelector()
                fields[vol.Required(f"text_{index}")] = selector.TextSelector()
                continue
            fields[vol.Required(f"entity_{index}")] = selector.EntitySelector(
                selector.EntitySelectorConfig(exclude_entities=excluded, **({"filter": {"domain": domains}} if domains else {})),
            )
            fields[vol.Optional(f"label_{index}")] = selector.TextSelector()
            if weather:
                continue
            fields[vol.Optional(f"unit_{index}")] = selector.TextSelector()
            fields[vol.Optional(f"decimals_{index}", default="original")] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(DECIMAL_PLACES), translation_key="decimal_places",
                                              mode=selector.SelectSelectorMode.DROPDOWN),
            )
        if graph:
            fields[vol.Required("history_hours", default=self._settings.get("history_hours", 24))] = selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX,
                                              unit_of_measurement="h"))
        schema = vol.Schema(fields)
        errors = {}
        if user_input is not None:
            candidate = dict(self._settings)
            for index in range(1, count + 1):
                if is_text_value(candidate, index):
                    candidate[f"text_{index}"] = user_input.get(f"text_{index}", "")
                    candidate[f"label_{index}"] = user_input.get(f"label_{index}", "")
                    for field in ("entity", "unit", "decimals"):
                        candidate.pop(f"{field}_{index}", None)
                    continue
                candidate.pop(f"text_{index}", None)
                for field in ("entity", "label", "unit"):
                    candidate[f"{field}_{index}"] = user_input.get(f"{field}_{index}", "")
                candidate[f"decimals_{index}"] = user_input.get(f"decimals_{index}", "original")
                entity_id = candidate[f"entity_{index}"]
                if entity_id in excluded:
                    errors[f"entity_{index}"] = "invalid_entity"
                elif not self.hass.states.get(entity_id) and not registry.async_get(entity_id):
                    errors[f"entity_{index}"] = "entity_not_found"
                else:
                    state = self.hass.states.get(entity_id)
                    if weather:
                        from homeassistant.components.weather import WeatherEntityFeature
                        if state and state.state not in ("unknown", "unavailable") and not int(state.attributes.get("supported_features", 0)) & WeatherEntityFeature.FORECAST_HOURLY:
                            errors[f"entity_{index}"] = "hourly_forecast_required"
                    elif graph and state and state.state not in ("unknown", "unavailable"):
                        from .widget_data import number
                        if number(state.state) is None:
                            errors[f"entity_{index}"] = "numeric_entity_required"
            if graph:
                candidate["history_hours"] = user_input.get("history_hours", 24)
            try:
                candidate = validate_settings(candidate)
            except vol.Invalid as err:
                field = str(err.path[0]) if err.path else "base"
                errors[field] = "invalid_text" if field.startswith("text_") else "invalid_settings"
            if not errors:
                self._settings = candidate
                return await self.async_step_preview()
            suggestions = candidate
        else:
            suggestions = self._settings
        return self.async_show_form(
            step_id="three_values" if three_values else "values",
            data_schema=self.add_suggested_values_to_schema(schema, suggestions),
            errors=errors, last_step=False,
        )

    async def async_step_preview(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get("action") == "edit":
                if self._import_document is not None:
                    return await self.async_step_import_settings()
                return await self.async_step_configure()
            if user_input.get("action") == "apply" and self._preview:
                return self.async_create_entry(data={
                    **self.config_entry.options,
                    "screen": validate_settings(self._settings),
                    # Applying the same preset again must also replace a later
                    # draw action; HA otherwise ignores identical options.
                    "screen_revision": secrets.token_hex(8),
                })
        errors = {}
        bitmap_fallback = False
        try:
            self._settings = validate_settings(self._settings)
            layout = (clock_layout(dt_util.now()) if self._settings["preset"] == "clock"
                      else preset_layout(self._settings))
            frame = await async_render_layout(self.hass, layout)
            self._preview = await self.hass.async_add_executor_job(preview_svg, frame.raw)
            bitmap_fallback = (self._settings["preset"] in
                               ("clock", "single_value", "clock_two_values", "three_values")
                               and frame.template_payload is None)
        except (HomeAssistantError, vol.Invalid, ValueError, OSError):
            _LOGGER.exception("Unable to render GTag layout preview")
            errors["base"] = "render_failed"
            self._preview = ""
        fields = {}
        if bitmap_fallback:
            # Read-only selector labels use the HA user's language, unlike
            # backend-generated prose. This notice never blocks Apply.
            fields[vol.Optional("render_notice", default="bitmap_fallback")] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=["bitmap_fallback"], translation_key="render_notice",
                                              mode=selector.SelectSelectorMode.DROPDOWN, read_only=True))
        fields[vol.Required("action", default="apply")] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["apply", "edit", "refresh"], translation_key="preview_action",
                mode=selector.SelectSelectorMode.DROPDOWN,
            ),
        )
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema(fields),
            description_placeholders={"preview": self._preview}, errors=errors, last_step=True,
        )
