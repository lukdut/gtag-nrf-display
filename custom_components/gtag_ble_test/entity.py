"""Common event subscription and device identity for display entities."""
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, TRANSPORT_BLE, TRANSPORT_WIFI
from .display import Display


class GTagEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, display: Display, key: str) -> None:
        self.display = display
        self._attr_unique_id = f"{display.identity}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, display.identity)},
            connections=({(CONNECTION_BLUETOOTH, display.address)} if display.transport == TRANSPORT_BLE else
                         {(CONNECTION_NETWORK_MAC, display.address)} if display.transport == TRANSPORT_WIFI else set()),
            manufacturer="DIY", model="ESP32 G-Tag Display" if display.transport == TRANSPORT_WIFI else "nRF52840 G-Tag Display", name=display.name,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.display.subscribe(self.async_write_ha_state))
