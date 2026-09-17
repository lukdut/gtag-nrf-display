"""Autonomous G-Tag LCD bring-up, ESPHome 2026.8.2 / NCS 2.9.2.

No BLE, imported lcd_local headers, or YAML-controlled LCD stages.
The four physical pins remain identical to the earlier prototype.
"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID
from esphome.core import CORE
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_prj_conf

DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = ["bthome", "gtag_ble_test", "lcd_gpio_guard", "spi", "i2c"]
MULTI_CONF = False

ns = cg.esphome_ns.namespace("lcd_standalone")
LCDStandalone = ns.class_("LCDStandalone", cg.Component)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(LCDStandalone),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    if not CORE.is_nrf52:
        raise cv.Invalid("lcd_standalone requires nRF52")

    # The board is a build surrogate, not a literal ItsyBitsy Express.
    # Only disable known, unrelated ItsyBitsy buses. Preserve internal
    # flash/NVS, GPIO, GPIOTE's normal Zephyr driver and USB logging.
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
    zephyr_add_prj_conf("BT", False)

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
