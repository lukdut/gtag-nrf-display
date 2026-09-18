"""Shared G-Tag LCD/ADC driver with BLE and experimental Zigbee profiles."""
import re

from esphome import pins
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv

from esphome.const import CONF_ID
from esphome.core import CORE
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_prj_conf
from esphome.components.nrf52.const import AIN_TO_GPIO
from esphome.components.nrf52.gpio import validate_gpio_pin

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
CONF_TRANSPORT = "transport"
LCD_PINS = {"dio_pin": "P0.11", "clk_pin": "P1.04", "cs_pin": "P1.06", "reset_pin": "P1.13"}

ns = cg.esphome_ns.namespace("gtag_display")
GTagDisplay = ns.class_("GTagDisplay", cg.Component)
GTagZigbee = ns.class_("GTagZigbee", cg.Component)
ZigbeeComponent = cg.esphome_ns.namespace("zigbee").class_("ZigbeeComponent", cg.Component)
BootPattern = ns.enum("BootPattern", is_class=True)
BOOT_PATTERNS = {
    "logo": BootPattern.LOGO,
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


def gpio_number(value):
    """Use physical nRF GPIO numbers, not board-dependent D/A aliases."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise cv.Invalid("Use a GPIO number such as P0.11; mode and inversion are fixed by the LCD protocol")
    if isinstance(value, str) and value.startswith("P"):
        match = re.fullmatch(r"P([01])\.(\d{1,2})", value)
        if not match or int(match[2]) >= (32 if match[1] == "0" else 16):
            raise cv.Invalid("Expected P0.00..P0.31 or P1.00..P1.15")
    number = validate_gpio_pin(value)
    if not isinstance(number, int) or not 0 <= number <= 47:
        raise cv.Invalid("Expected an external GPIO on nRF52840 (0..47)")
    if number in (0, 1, 9, 10, 18):
        raise cv.Invalid("P0.00/P0.01 (32 kHz crystal), P0.09/P0.10 (NFC), and P0.18 (RESET) are reserved")
    return number


def battery_pin(value):
    number = gpio_number(value)
    if number not in AIN_TO_GPIO.values():
        raise cv.Invalid("Battery ADC requires P0.02..P0.05 or P0.28..P0.31")
    return pins.internal_gpio_input_pin_number(number)


def validate_pin_assignment(config):
    used = {}
    assignments = [(key, config[key], [key]) for key in LCD_PINS]
    if CONF_BATTERY_VOLTAGE in config:
        assignments.append(("battery_voltage.pin", config[CONF_BATTERY_VOLTAGE]["pin"], [CONF_BATTERY_VOLTAGE, "pin"]))
    for name, number, path in assignments:
        if number in used:
            raise cv.Invalid(f"GPIO P{number // 32}.{number % 32:02d} is already used by {used[number]}", path)
        used[number] = name
    return config


def reserve_frame_endpoint(config):
    if config[CONF_TRANSPORT] == "zigbee":
        if "zigbee_id" not in config:
            raise cv.Invalid("transport: zigbee requires zigbee_id")
        from esphome.components.zigbee import consume_endpoint
        consume_endpoint(config)
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(GTagDisplay),
    cv.GenerateID("zigbee_transport_id"): cv.declare_id(GTagZigbee),
    cv.Optional("zigbee_id"): cv.use_id(ZigbeeComponent),
    cv.Optional(CONF_TRANSPORT, default="ble"): cv.one_of("ble", "zigbee", lower=True),
    cv.Optional("zigbee_power_diagnostics", default=False): cv.boolean,
    cv.Optional(CONF_TX_POWER, default=0): cv.one_of(0, 4, 8, int=True),
    cv.Optional(CONF_ADVERTISING_INTERVAL, default="1s"): advertising_interval,
    cv.Optional(CONF_BOOT_TEST_PATTERN, default="logo"): cv.enum(BOOT_PATTERNS, lower=True),
    **{cv.Optional(key, default=value): cv.All(gpio_number, pins.internal_gpio_output_pin_number)
       for key, value in LCD_PINS.items()},
    # B+ -- 1M -- ADC GPIO -- 1M -- GND; 100nF from ADC GPIO to GND.
    cv.Optional(CONF_BATTERY_VOLTAGE): cv.Schema({
        cv.Optional("pin", default="P0.31"): battery_pin,
        cv.Optional(CONF_CALIBRATION, default=1.0): cv.float_range(min=0.8, max=1.2),
        cv.Optional("indicator", default=True): cv.boolean,
    }),
}).extend(cv.COMPONENT_SCHEMA), validate_pin_assignment, reserve_frame_endpoint)


def validate_transport(config):
    has_zigbee = "zigbee" in fv.full_config.get()
    if config[CONF_TRANSPORT] == "zigbee" and not has_zigbee:
        raise cv.Invalid("transport: zigbee requires the zigbee component")
    if config[CONF_TRANSPORT] == "ble" and has_zigbee:
        raise cv.Invalid("Use transport: zigbee with the zigbee component; BLE and Zigbee are separate profiles")
    if config[CONF_TRANSPORT] == "ble" and "zigbee_id" in config:
        raise cv.Invalid("zigbee_id is only used with transport: zigbee")
    return config


FINAL_VALIDATE_SCHEMA = validate_transport


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

    if config[CONF_TRANSPORT] == "zigbee":
        cg.add_define("USE_GTAG_ZIGBEE")
        zephyr_add_prj_conf("BT", False)
        if config["zigbee_power_diagnostics"]:
            cg.add_define("USE_GTAG_ZIGBEE_POWER_DIAGNOSTICS")
            # RTC-based accounting; no high-frequency debug timer or USB.
            zephyr_add_prj_conf("THREAD_RUNTIME_STATS", True)
            zephyr_add_prj_conf("THREAD_RUNTIME_STATS_USE_TIMING_FUNCTIONS", False)
            zephyr_add_prj_conf("THREAD_MONITOR", True)
            zephyr_add_prj_conf("THREAD_NAME", True)
            cg.add_build_flag("-Wl,--wrap=zb_osif_sleep")
    else:
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
    for key in LCD_PINS:
        cg.add(getattr(var, f"set_{key}")(config[key]))
    if CONF_BATTERY_VOLTAGE in config:
        cg.add_define("USE_GTAG_BATTERY")
        zephyr_add_overlay('&adc { status = "okay"; };')
        zephyr_add_prj_conf("ADC", True)
        cg.add(var.set_battery_pin(config[CONF_BATTERY_VOLTAGE]["pin"]))
        cg.add(var.set_battery_calibration(config[CONF_BATTERY_VOLTAGE][CONF_CALIBRATION]))
        cg.add(var.set_battery_indicator(config[CONF_BATTERY_VOLTAGE]["indicator"]))
    if config[CONF_TRANSPORT] == "zigbee":
        from .zigbee_codegen import add_frame_endpoint
        CORE.add_job(add_frame_endpoint, var, config)
