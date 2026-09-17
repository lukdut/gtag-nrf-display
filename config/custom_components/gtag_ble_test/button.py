from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .transport import FrameSender, connect, get_operation_lock


WIDTH = 256
HEIGHT = 128
ROW_BYTES = WIDTH // 8
FRAME_SIZE = WIDTH * HEIGHT // 8


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            SendTestFrameButton(
                hass,
                config_entry.title,
                config_entry.data[CONF_ADDRESS],
            )
        ]
    )


def _set_black(frame: bytearray, x: int, y: int) -> None:
    """Native G-Tag row-lsb packing: 8 horizontal pixels per byte."""
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return
    idx = y * ROW_BYTES + (x >> 3)
    frame[idx] &= ~(1 << (x & 7))


def _hline(frame: bytearray, x0: int, x1: int, y: int) -> None:
    for x in range(x0, x1 + 1):
        _set_black(frame, x, y)


def _vline(frame: bytearray, x: int, y0: int, y1: int) -> None:
    for y in range(y0, y1 + 1):
        _set_black(frame, x, y)


_FONT_5X7 = {
    "R": (
        "11110",
        "10001",
        "10001",
        "11110",
        "10100",
        "10010",
        "10001",
    ),
    "L": (
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "11111",
    ),
    "E": (
        "11111",
        "10000",
        "10000",
        "11110",
        "10000",
        "10000",
        "11111",
    ),
    "O": (
        "01110",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "01110",
    ),
    "K": (
        "10001",
        "10010",
        "10100",
        "11000",
        "10100",
        "10010",
        "10001",
    ),
}


def _char(frame: bytearray, ch: str, x: int, y: int, scale: int = 4) -> None:
    glyph = _FONT_5X7[ch]
    for gy, row in enumerate(glyph):
        for gx, bit in enumerate(row):
            if bit != "1":
                continue
            for sy in range(scale):
                for sx in range(scale):
                    _set_black(frame, x + gx * scale + sx, y + gy * scale + sy)


def _text(frame: bytearray, text: str, x: int, y: int, scale: int = 4) -> None:
    cursor = x
    for ch in text:
        if ch == " ":
            cursor += 3 * scale
            continue
        _char(frame, ch, cursor, y, scale)
        cursor += 6 * scale


def make_sparse_test_frame() -> bytes:
    """Recognisable, mostly-white test image in the real framebuffer layout."""
    frame = bytearray(b"\xFF" * FRAME_SIZE)

    _hline(frame, 4, WIDTH - 5, 4)
    _hline(frame, 4, WIDTH - 5, HEIGHT - 5)
    _vline(frame, 4, 4, HEIGHT - 5)
    _vline(frame, WIDTH - 5, 4, HEIGHT - 5)

    _text(frame, "RLE OK", 46, 28, scale=4)

    for y in range(92, 116):
        for x in range(18, 42):
            if ((x - 18) // 4 + (y - 92) // 4) & 1:
                _set_black(frame, x, y)

    for i in range(24):
        _set_black(frame, 210 + i, 92 + i)

    return bytes(frame)


class SendTestFrameButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Send RLE test image"

    def __init__(self, hass: HomeAssistant, name: str, address: str) -> None:
        self.hass = hass
        self._address = address
        self._attr_unique_id = f"{address}_send_test_frame"
        self._attr_extra_state_attributes = {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="DIY",
            model="nRF52840 G-Tag Display",
            name=name,
        )

    async def async_press(self) -> None:
        lock = get_operation_lock(self.hass, self._address)
        async with lock:
            sender = FrameSender(
                lambda: connect(self.hass, self._address),
                self._address,
            )
            report = await sender.send(make_sparse_test_frame())

        self._attr_extra_state_attributes = report.attributes()
        self.async_write_ha_state()
