from __future__ import annotations

from typing import Any
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, LED_CHAR_UUID


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            GTagLED(
                hass,
                config_entry.title,
                config_entry.data[CONF_ADDRESS],
            )
        ]
    )


class GTagLED(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Test LED"

    def __init__(self, hass: HomeAssistant, name: str, address: str) -> None:
        self.hass = hass
        self._address = address
        self._attr_unique_id = f"{address}_led"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="DIY",
            model="nRF52840 GATT Test",
            name=name,
        )

    async def _write(self, state: bool) -> None:
        device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            raise HomeAssistantError("BLE device not reachable")

        client = await establish_connection(
            BleakClient,
            device,
            self._address,
        )

        try:
            await client.write_gatt_char(
                LED_CHAR_UUID,
                bytes([1 if state else 0]),
                response=True,
            )
        finally:
            await client.disconnect()

        self._attr_is_on = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)
