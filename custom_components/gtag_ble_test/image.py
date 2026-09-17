"""Preview of the last successfully transferred framebuffer."""
from homeassistant.components.image import ImageEntity

from .entity import GTagEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([GTagPreview(entry.runtime_data)])


class GTagPreview(GTagEntity, ImageEntity):
    _attr_name = "Display preview"
    _attr_content_type = "image/png"

    def __init__(self, display):
        ImageEntity.__init__(self, display.hass)
        GTagEntity.__init__(self, display, "preview")

    @property
    def available(self):
        return self.display.preview is not None

    @property
    def image_last_updated(self):
        return self.display.last_success

    async def async_image(self):
        return self.display.preview
