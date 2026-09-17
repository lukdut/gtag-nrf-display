import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.const import CONF_ID
from esphome.core import CORE
from esphome.components.zephyr import zephyr_add_prj_conf

DEPENDENCIES = ["nrf52"]
MULTI_CONF = False
CONF_LED_PIN = "led_pin"
CONF_TX_POWER = "tx_power"
CONF_LCD = "lcd"
LCD_PINS = ("dio_pin", "clk_pin", "cs_pin", "reset_pin")
ns = cg.esphome_ns.namespace("gtag_ble_test")
GTagBLETest = ns.class_("GTagBLETest", cg.Component)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(GTagBLETest),
    cv.Required(CONF_LED_PIN): pins.gpio_output_pin_schema,
    cv.Optional(CONF_TX_POWER, default=0): cv.one_of(0, 4, 8, int=True),
    cv.Optional(CONF_LCD): cv.Schema({
        **{cv.Required(name): pins.internal_gpio_output_pin_schema for name in LCD_PINS},
        cv.Optional("spi_half_period_us", default=1): cv.int_range(min=1, max=20),
        cv.Optional("boot_test_pattern", default=True): cv.boolean,
    }),
}).extend(cv.COMPONENT_SCHEMA)

async def to_code(config):
    if not CORE.is_nrf52:
        raise cv.Invalid("gtag_ble_test requires nRF52")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    pin = await cg.gpio_pin_expression(config[CONF_LED_PIN])
    cg.add(var.set_led_pin(pin))
    if CONF_LCD in config:
        lcd = config[CONF_LCD]
        for name in LCD_PINS:
            pin = await cg.gpio_pin_expression(lcd[name])
            cg.add(getattr(var, "set_lcd_" + name)(pin))
        cg.add(var.set_lcd_half_period_us(lcd["spi_half_period_us"]))
        cg.add(var.set_lcd_boot_test(lcd["boot_test_pattern"]))
    zephyr_add_prj_conf("BT", True)
    zephyr_add_prj_conf("BT_PERIPHERAL", True)
    zephyr_add_prj_conf("BT_MAX_CONN", 1)
    zephyr_add_prj_conf("BT_DEVICE_NAME", '"GTag BLE Test"')
    tx_option = {0: "BT_CTLR_TX_PWR_0", 4: "BT_CTLR_TX_PWR_PLUS_4", 8: "BT_CTLR_TX_PWR_PLUS_8"}
    zephyr_add_prj_conf(tx_option[config[CONF_TX_POWER]], True)
