"""Local transfer diagnostics; these entities never poll the BLE peripheral."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import GTagEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LastUpdate(entry.runtime_data), TransferStatus(entry.runtime_data)])


class LastUpdate(GTagEntity, SensorEntity):
    _attr_name = "Last display update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, display):
        super().__init__(display, "last_update")

    @property
    def native_value(self):
        return self.display.last_success


class TransferStatus(GTagEntity, SensorEntity):
    _attr_name = "Display transfer"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "queued", "sending", "sent", "unchanged", "error"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, display):
        super().__init__(display, "transfer")

    @property
    def native_value(self):
        return self.display.status

    @property
    def extra_state_attributes(self):
        return {"last_error": self.display.last_error, **self.display.report}
