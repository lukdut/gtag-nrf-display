"""GTag display integration for Bluetooth, Zigbee2MQTT and ESPHome Wi-Fi."""
from __future__ import annotations

import logging
from pathlib import Path
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
import voluptuous as vol

from .const import CONF_TRANSPORT, DOMAIN, TRANSPORT_ZIGBEE, TRANSPORT_WIFI
from .display import Display
from .render import DEFAULT_FONT, FONT_DIR

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.IMAGE, Platform.SENSOR, Platform.BINARY_SENSOR]

# Relaxed schema to allow font inside text elements
DRAW_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Optional("background", default="white"): cv.string,
    vol.Required("elements"): list,
    vol.Optional("force", default=False): cv.boolean,
    vol.Optional("auto_update", default=True): cv.boolean,
})

def _scan_fonts() -> list[str]:
    """Synchronous function to scan fonts, runs in executor."""
    if not FONT_DIR.exists():
        return [DEFAULT_FONT]
    try:
        fonts = sorted(p.name for p in FONT_DIR.glob("*.ttf"))
        return fonts if fonts else [DEFAULT_FONT]
    except OSError:
        return [DEFAULT_FONT]

async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    # Scan fonts once at startup to avoid blocking the event loop
    fonts = await hass.async_add_executor_job(_scan_fonts)
    hass.data.setdefault(DOMAIN, {})["fonts"] = fonts
    _LOGGER.debug("Loaded fonts for GTag Display: %s", fonts)

    def selected_display(call: ServiceCall) -> Display:
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is None:
            raise ServiceValidationError("GTag display device not found")
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN:
                display = getattr(entry, "runtime_data", None)
                if isinstance(display, Display):
                    return display
        raise ServiceValidationError("The selected GTag display is not loaded")

    async def draw(call: ServiceCall) -> dict | None:
        layout = {
            "background": call.data["background"],
            "elements": call.data["elements"]
        }

        result = await selected_display(call).async_draw(
            layout,
            force=call.data["force"],
            auto_update=call.data["auto_update"],
        )
        return result

    async def check_connection(call: ServiceCall) -> dict:
        return await selected_display(call).connection_check.async_run()

    hass.services.async_register(
        DOMAIN, "draw", draw, schema=DRAW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "check_connection", check_connection,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_ZIGBEE:
        from homeassistant.components import mqtt

        if not await mqtt.async_wait_for_mqtt_client(hass):
            raise ConfigEntryNotReady("Configure the MQTT integration to use Zigbee2MQTT")
    elif entry.data.get(CONF_TRANSPORT) == TRANSPORT_WIFI:
        linked = hass.config_entries.async_get_entry(entry.data["esphome_entry_id"])
        if linked is None or linked.domain != "esphome":
            raise ConfigEntryNotReady("Add the linked device to ESPHome first")
    elif not await async_setup_component(hass, "bluetooth", {}):
        raise ConfigEntryNotReady("Bluetooth is not ready")
    display = entry.runtime_data = Display(hass, entry)
    try:
        await display.async_load()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await display.async_close()
        entry.runtime_data = None
        raise

    async def stop(_event):
        await display.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    async def update_options(_hass: HomeAssistant, updated: ConfigEntry) -> None:
        if settings := updated.options.get("screen"):
            try:
                await display.async_apply_settings(settings, updated.options["screen_revision"])
            except HomeAssistantError:
                pass

    entry.async_on_unload(entry.add_update_listener(update_options))
    display.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_close()
    entry.runtime_data = None
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
