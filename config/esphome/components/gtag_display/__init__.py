"""Combined G-Tag BLE receiver + proven LCD3-DIRECT-01 driver.

ESPHome 2026.8.x / NCS 2.9.2.
"""
import esphome.codegen as cg
import esphome.config_validation as cv

from esphome.const import CONF_ID
from esphome.core import CORE
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_prj_conf

DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = [
    "bthome",
    "gtag_ble_test",
    "lcd_standalone",
    "lcd_gpio_guard",
    "spi",
    "i2c",
]
MULTI_CONF = False

CONF_TX_POWER = "tx_power"

ns = cg.esphome_ns.namespace("gtag_display")
GTagDisplay = ns.class_("GTagDisplay", cg.Component)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(GTagDisplay),
    cv.Optional(CONF_TX_POWER, default=0): cv.one_of(0, 4, 8, int=True),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    if not CORE.is_nrf52:
        raise cv.Invalid("gtag_display requires nRF52")

    # adafruit_itsybitsy_nrf52840 is only the build surrogate for this board.
    zephyr_add_overlay("""
        &i2c0 { status = "disabled"; };
        &spi1 { status = "disabled"; };
        &spi2 { status = "disabled"; };
        &apa102 { status = "disabled"; };
        &qspi { status = "disabled"; };
        &gd25q16 { status = "disabled"; };
        &uart0 { status = "disabled"; };
    """)

    zephyr_add_prj_conf("SPI", False)
    zephyr_add_prj_conf("I2C", False)
    zephyr_add_prj_conf("PWM", False)

    zephyr_add_prj_conf("BT", True)
    zephyr_add_prj_conf("BT_PERIPHERAL", True)
    zephyr_add_prj_conf("BT_MAX_CONN", 1)
    zephyr_add_prj_conf("BT_DEVICE_NAME", '"GTag Display"')

    tx_option = {
        0: "BT_CTLR_TX_PWR_0",
        4: "BT_CTLR_TX_PWR_PLUS_4",
        8: "BT_CTLR_TX_PWR_PLUS_8",
    }
    zephyr_add_prj_conf(tx_option[config[CONF_TX_POWER]], True)

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
