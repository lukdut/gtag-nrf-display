"""Transfer diagnostics and battery voltage for either transport."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfElectricPotential
from homeassistant.helpers.entity import EntityCategory

from .entity import GTagEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        LastUpdate(entry.runtime_data), TransferStatus(entry.runtime_data),
        BatteryVoltage(entry.runtime_data),
    ])


class BatteryVoltage(GTagEntity, SensorEntity):
    _attr_translation_key = "battery_voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, display):
        super().__init__(display, "battery_voltage")

    @property
    def available(self):
        return self.display.battery.voltage is not None

    @property
    def native_value(self):
        return self.display.battery.voltage

    @property
    def extra_state_attributes(self):
        battery = self.display.battery
        return {"last_read": battery.last_read, "last_error": battery.last_error,
                "firmware_supported": battery.supported}


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
        # HA omits extra attributes of unavailable entities. Keep battery
        # diagnostics on this always-available status entity as well.
        battery = self.display.battery
        return {
            "transport": self.display.transport,
            "last_error": self.display.last_error, **self.display.report,
            "battery_last_read": battery.last_read,
            "battery_last_error": battery.last_error,
            "battery_firmware_supported": battery.supported,
        }
