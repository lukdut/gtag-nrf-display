"""Locally estimated freshness of the last confirmed image."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import GTagEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DisplayStale(entry.runtime_data)])


class DisplayStale(GTagEntity, BinarySensorEntity):
    _attr_translation_key = "display_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, display):
        super().__init__(display, "stale")

    @property
    def is_on(self):
        return self.display.stale

    @property
    def extra_state_attributes(self):
        return {"timeout_seconds": self.display.confirmed_timeout,
                "configured_timeout_seconds": self.display.freshness_timeout,
                "last_confirmation": self.display.last_confirmation}
