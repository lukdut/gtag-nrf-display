from __future__ import annotations

from typing import Any
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, TRANSPORT_BLE
from .transport import write_led


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    display = config_entry.runtime_data
    entities = [GTagClock(display)]
    if display.transport == TRANSPORT_BLE:
        entities.insert(0, GTagLED(hass, config_entry.title, config_entry.data[CONF_ADDRESS]))
    async_add_entities(entities)


class GTagLED(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Virtual LED"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, name: str, address: str) -> None:
        self.hass = hass
        self._address = address
        self._attr_unique_id = f"{address}_led"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="DIY",
            model="nRF52840 G-Tag Display",
            name=name,
        )

    async def _write(self, state: bool) -> None:
        await write_led(self.hass, self._address, state)

        self._attr_is_on = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)


class GTagClock(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Clock screen"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, display) -> None:
        self.display = display
        self._attr_unique_id = f"{display.identity}_clock_screen"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, display.identity)})

    @property
    def is_on(self):
        return self.display.clock_enabled

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.display.subscribe(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs):
        await self.display.async_set_clock(True)

    async def async_turn_off(self, **kwargs):
        await self.display.async_set_clock(False)
