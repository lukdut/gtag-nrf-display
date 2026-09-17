"""Build-only isolation for the nRF52840 LCD-only diagnostic.

For ESPHome 2026.8.2 / NCS 2.9.2 / adafruit_itsybitsy_nrf52840.
This component has no runtime object and does not change the bootloader.
"""
import esphome.config_validation as cv
from esphome.core import CORE
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_prj_conf

DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = ["bthome", "gtag_ble_test", "spi", "i2c"]
CONFIG_SCHEMA = cv.Schema({})


async def to_code(config):
    if not CORE.is_nrf52:
        raise cv.Invalid("lcd_gpio_guard requires nRF52")
    # The selected board is a build surrogate, NOT the user's Pro Micro.
    # Disable its unrelated external buses and peripherals, keep USB CDC,
    # the GPIO ports, internal flash/NVS and clocks alone.
    zephyr_add_overlay('''
        &i2c0 { status = "disabled"; };
        &spi1 { status = "disabled"; };
        &spi2 { status = "disabled"; };
        &apa102 { status = "disabled"; };
        &qspi { status = "disabled"; };
        &gd25q16 { status = "disabled"; };
        &uart0 { status = "disabled"; };
    ''')
    zephyr_add_prj_conf("SPI", False)
    zephyr_add_prj_conf("I2C", False)
    zephyr_add_prj_conf("PWM", False)
