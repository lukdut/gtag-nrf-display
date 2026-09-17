"""G-Tag display with BLE frame transfer and System ON idle power saving.

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
CONF_ADVERTISING_INTERVAL = "advertising_interval"
CONF_BOOT_TEST_PATTERN = "boot_test_pattern"
CONF_BATTERY_VOLTAGE = "battery_voltage"
CONF_CALIBRATION = "calibration"

ns = cg.esphome_ns.namespace("gtag_display")
GTagDisplay = ns.class_("GTagDisplay", cg.Component)
BootPattern = ns.enum("BootPattern", is_class=True)
BOOT_PATTERNS = {
    "none": BootPattern.NONE,
    "white": BootPattern.WHITE,
    "black": BootPattern.BLACK,
    "checkerboard": BootPattern.CHECKERBOARD,
    "stripes": BootPattern.STRIPES,
}


def advertising_interval(value):
    interval = cv.positive_time_period_milliseconds(value)
    if not 100 <= interval.total_milliseconds <= 10240:
        raise cv.Invalid("advertising_interval must be between 100ms and 10240ms")
    return interval

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(GTagDisplay),
    cv.Optional(CONF_TX_POWER, default=0): cv.one_of(0, 4, 8, int=True),
    cv.Optional(CONF_ADVERTISING_INTERVAL, default="1s"): advertising_interval,
    cv.Optional(CONF_BOOT_TEST_PATTERN, default="none"): cv.enum(BOOT_PATTERNS, lower=True),
    # Fixed hardware: B+ -- 1M -- P0.31/AIN7 -- 1M -- GND; 100nF to GND.
    cv.Optional(CONF_BATTERY_VOLTAGE): cv.Schema({
        cv.Optional(CONF_CALIBRATION, default=1.0): cv.float_range(min=0.8, max=1.2),
    }),
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
    # The surrogate board enables CDC in its devicetree even without logger.
    # Remove that device too, so the battery build has no USB/UART activity.
    if "logger" not in CORE.config:
        zephyr_add_overlay("""
            &usbd { status = "disabled"; };
            &cdc_acm_uart0 { status = "disabled"; };
            / {
                chosen {
                    /delete-property/ zephyr,console;
                    /delete-property/ zephyr,shell-uart;
                    /delete-property/ zephyr,uart-mcumgr;
                    /delete-property/ zephyr,bt-mon-uart;
                    /delete-property/ zephyr,bt-c2h-uart;
                };
            };
        """)
        zephyr_add_prj_conf("USB_DEVICE_STACK", False)
        zephyr_add_prj_conf("USB_CDC_ACM", False)
        zephyr_add_prj_conf("SERIAL", False)

    # nRF52's normal idle path supports System ON sleep without CONFIG_PM.
    # Keep the kernel tickless; do not power off the BLE controller or LCD GPIO.
    zephyr_add_prj_conf("TICKLESS_KERNEL", True)

    zephyr_add_prj_conf("BT", True)
    zephyr_add_prj_conf("BT_PERIPHERAL", True)
    zephyr_add_prj_conf("BT_MAX_CONN", 1)
    zephyr_add_prj_conf("BT_DEVICE_NAME", "GTag Display")

    tx_option = {
        0: "BT_CTLR_TX_PWR_0",
        4: "BT_CTLR_TX_PWR_PLUS_4",
        8: "BT_CTLR_TX_PWR_PLUS_8",
    }
    zephyr_add_prj_conf(tx_option[config[CONF_TX_POWER]], True)

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_advertising_interval(config[CONF_ADVERTISING_INTERVAL].total_milliseconds))
    cg.add(var.set_boot_pattern(config[CONF_BOOT_TEST_PATTERN]))
    if CONF_BATTERY_VOLTAGE in config:
        cg.add_define("USE_GTAG_BATTERY")
        zephyr_add_overlay('&adc { status = "okay"; };')
        zephyr_add_prj_conf("ADC", True)
        cg.add(var.set_battery_calibration(config[CONF_BATTERY_VOLTAGE][CONF_CALIBRATION]))
