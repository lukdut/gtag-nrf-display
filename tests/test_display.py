"""Run with python3 -m unittest discover -s tests -v (Python >= 3.11 and g++).

Compiles the complete firmware component on the host. Only Zephyr/ESPHome and
HA/Bleak boundaries are simulated; the sender, codecs, GATT parser, receiver,
LCD state machine and GPIO serializer are the production implementations.
"""
import asyncio
import ctypes
import importlib
import logging
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zlib

from saleae_reference import reference_words

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "config/esphome/components/gtag_display"


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


# External runtime boundaries only: tests do not need a running Home Assistant.
class HomeAssistantError(Exception):
    pass


class BleakError(Exception):
    pass


class BleakCharacteristicNotFoundError(BleakError):
    pass


class Entity:
    def async_write_ha_state(self):
        pass


module("bleak", BleakClient=object)
module("bleak.exc", BleakError=BleakError,
       BleakCharacteristicNotFoundError=BleakCharacteristicNotFoundError)
module("bleak_retry_connector", establish_connection=None)
module("homeassistant")
module("homeassistant.components")
module("homeassistant.components.bluetooth")
module("homeassistant.components.switch", SwitchEntity=Entity)
module("homeassistant.config_entries", ConfigEntry=object)
module("homeassistant.const", CONF_ADDRESS="address")
module("homeassistant.core", HomeAssistant=object)
module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
module("homeassistant.helpers")
module("homeassistant.helpers.device_registry",
       CONNECTION_BLUETOOTH="bluetooth", DeviceInfo=dict)
module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
module("gtag_under_test", __path__=[str(ROOT / "custom_components/gtag_ble_test")])
transport = importlib.import_module("gtag_under_test.transport")
codec = importlib.import_module("gtag_under_test.frame_codec")
switch = importlib.import_module("gtag_under_test.switch")
logging.getLogger(transport.__name__).setLevel(logging.CRITICAL)


def setUpModule():
    global build, fw
    build = tempfile.TemporaryDirectory(prefix="gtag-tests-")
    path = Path(build.name)
    # Redirect platform includes to the host model, without editing the driver.
    headers = [
        "esphome/core/component.h", "esphome/core/application.h",
        "esphome/core/hal.h", "esphome/core/log.h",
        "zephyr/bluetooth/bluetooth.h", "zephyr/bluetooth/conn.h",
        "zephyr/bluetooth/gatt.h", "zephyr/bluetooth/uuid.h", "zephyr/device.h",
        "zephyr/devicetree.h", "zephyr/drivers/gpio.h",
        "zephyr/dt-bindings/gpio/nordic-nrf-gpio.h", "zephyr/kernel.h", "hal/nrf_gpio.h",
    ]
    for header in headers:
        target = path / header
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('#include "native_stubs.h"\n')
    library = path / "driver.so"
    subprocess.run([
        "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
        "-I", str(path), "-I", str(ROOT / "tests"), "-I", str(COMPONENT),
        str(ROOT / "tests/native_driver.cpp"), "-o", str(library),
    ], check=True)
    fw = ctypes.CDLL(str(library))
    for name in ("control", "data", "led"):
        getattr(fw, f"firmware_{name}").argtypes = [ctypes.c_char_p, ctypes.c_uint16]
    fw.firmware_status.argtypes = [ctypes.c_void_p]
    for name in ("word_time", "reset_low", "reset_high"):
        getattr(fw, f"firmware_{name}").restype = ctypes.c_uint64


def tearDownModule():
    build.cleanup()


def start():
    fw.firmware_create(0, 1000)
    fw.firmware_run(4000)


def status():
    out = ctypes.create_string_buffer(8)
    fw.firmware_status(out)
    return transport.Status.parse(out.raw)


def words():
    return [fw.firmware_word(i) for i in range(fw.firmware_words())]


def begin(raw, frame_id=1, bad_crc=False):
    encoded = codec.encode_best(raw)
    packet = struct.pack("<BBB HII", 1, 1, encoded.codec, len(encoded.payload),
                         frame_id, encoded.raw_crc32 ^ int(bad_crc))
    assert fw.firmware_control(packet, len(packet)) == len(packet)
    for offset in range(0, len(encoded.payload), 18):
        data = struct.pack("<H", offset) + encoded.payload[offset:offset + 18]
        assert fw.firmware_data(data, len(data)) == len(data)
    return packet


def commit():
    return fw.firmware_control(b"\x02", 1)


class DisplayTests(unittest.TestCase):
    def setUp(self):
        start()

    def test_idle_has_no_component_polling(self):
        before = fw.firmware_loops()
        fw.firmware_run(60_000)
        self.assertEqual(fw.firmware_loops(), before)
        self.assertTrue(fw.firmware_advertising())
        self.assertEqual(fw.firmware_adv_interval(), 1600)

    def test_startup_deadlines_and_reset_50ms(self):
        fw.firmware_create(0, 1000)
        fw.firmware_run(2999)
        self.assertEqual(fw.firmware_words(), 0)
        self.assertFalse(fw.firmware_advertising())
        fw.firmware_run(1)
        self.assertEqual(fw.firmware_reset_high() - fw.firmware_reset_low(), 50)
        fw.firmware_run(49)
        self.assertEqual(fw.firmware_words(), 0)
        fw.firmware_run(1000)
        gap = fw.firmware_word_time(0) - fw.firmware_reset_high()
        self.assertTrue(49_000 <= gap <= 50_000, gap)
        self.assertTrue(fw.firmware_advertising())

    def test_immediate_disconnect_restarts_advertising(self):
        fw.firmware_connect()
        fw.firmware_run(0)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertTrue(fw.firmware_advertising())
        self.assertEqual(fw.firmware_adv_attempts(), 2)

    def test_advertising_failure_has_scheduled_retry(self):
        fw.firmware_create(0, 1000)
        fw.firmware_fail_advertising(1)
        fw.firmware_run(4000)
        self.assertFalse(fw.firmware_advertising())
        self.assertEqual(fw.firmware_adv_attempts(), 1)
        fw.firmware_run(1000)
        self.assertTrue(fw.firmware_advertising())
        self.assertEqual(fw.firmware_adv_attempts(), 2)

    def test_diagnostics_render_once_then_idle(self):
        for pattern in range(1, 5):
            with self.subTest(pattern=pattern):
                fw.firmware_create(pattern, 500)
                fw.firmware_run(4000)
                expected = [
                    255 if pattern == 1 else 0 if pattern == 2 else
                    (0 if ((i // 32) // 8 + i % 32) & 1 else 255) if pattern == 3 else 15
                    for i in range(4096)
                ]
                self.assertEqual(words()[-4096:], [256 | b for b in expected])
                self.assertEqual(fw.firmware_frames(), 1)
                self.assertEqual(status().state, 0)
                self.assertTrue(fw.firmware_advertising())
                self.assertEqual(fw.firmware_adv_interval(), 800)
                before = fw.firmware_loops()
                fw.firmware_run(60_000)
                self.assertEqual(fw.firmware_loops(), before)
                self.assertEqual(fw.firmware_frames(), 1)

    def test_bad_frame_does_not_corrupt_pending_good_frame(self):
        fw.firmware_connect()
        begin(b"\xff" * 4096)
        self.assertEqual(commit(), 1)
        begin(b"\x00" * 4096, frame_id=2, bad_crc=True)
        self.assertLess(commit(), 0)
        fw.firmware_run(0)
        self.assertEqual(fw.firmware_frames(), 0)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertEqual(fw.firmware_frames(), 1)
        self.assertEqual(words()[-4096:], [0x1ff] * 4096)

    def test_duplicate_commit_does_not_redraw(self):
        fw.firmware_connect()
        packet = begin(b"\xff" * 4096)
        self.assertEqual(commit(), 1)
        self.assertEqual(commit(), 1)
        fw.firmware_run(0)
        self.assertEqual(fw.firmware_frames(), 0)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        fw.firmware_connect()
        self.assertEqual(fw.firmware_control(packet, len(packet)), len(packet))
        self.assertEqual(commit(), 1)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertEqual(fw.firmware_frames(), 1)

    def test_disconnect_during_loop_disable_keeps_wake(self):
        fw.firmware_connect()
        begin(b"\xff" * 4096)
        self.assertEqual(commit(), 1)
        fw.firmware_race_disconnect()
        fw.firmware_run(0)
        self.assertEqual(fw.firmware_frames(), 1)
        self.assertTrue(fw.firmware_advertising())

    def test_all_lcd_words_match_esp32_capture(self):
        reference = reference_words(ROOT / "esp32-diagram/ESP32-gtag.sal")
        raw = bytes(word & 255 for word in reference[-4096:])
        fw.firmware_connect()
        begin(raw)
        self.assertEqual(commit(), 1)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertEqual(words(), reference)


class Client:
    def __init__(self, fault=None, wwr=True):
        self.fault, self.wwr = fault, wwr
        self.is_connected = False
        self.services = types.SimpleNamespace(get_characteristic=lambda _:
            types.SimpleNamespace(properties=["write-without-response"] if wwr else [],
                                  max_write_without_response_size=20))

    async def connect(self):
        fw.firmware_connect()
        fw.firmware_run(0)
        self.is_connected = True
        return self

    async def disconnect(self):
        if self.is_connected:
            fw.firmware_disconnect()
            fw.firmware_run(0)
            self.is_connected = False

    async def read_gatt_char(self, _):
        out = ctypes.create_string_buffer(8)
        fw.firmware_status(out)
        return out.raw

    async def write_gatt_char(self, uuid, data, *, response):
        if not self.is_connected:
            raise BleakError("disconnected")
        if uuid == transport.FRAME_CHAR_UUID:
            if self.fault == "drop_wwr" and not response:
                return
            if self.fault == "disconnect" and int.from_bytes(data[:2], "little") >= 36:
                self.fault = None
                await self.disconnect()
                raise BleakError("link lost")
            result = fw.firmware_data(data, len(data))
        elif uuid == transport.CONTROL_CHAR_UUID:
            result = fw.firmware_control(data, len(data))
            if data == b"\x02" and self.fault == "lost_commit_reply":
                self.fault = None
                raise TimeoutError("COMMIT accepted, ATT reply lost")
        else:
            result = fw.firmware_led(data, len(data))
        if result < 0 and response:
            raise BleakError(f"ATT error {result}")


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        start()

    async def test_sender_matches_cpp_receiver_with_recovery(self):
        # Incompressible and white frames exercise both codecs with real GATT packets.
        raw_frames = (b"\xff" * 4096, bytes(range(256)) * 16)
        for raw in raw_frames:
            for mode, fault, wwr in (
                ("windowed", None, True), ("acknowledged", None, True),
                ("windowed", "drop_wwr", True), ("windowed", None, False),
                ("windowed", "disconnect", True), ("windowed", "lost_commit_reply", True),
            ):
                with self.subTest(codec=codec.encode_best(raw).codec, mode=mode, fault=fault, wwr=wwr):
                    start()
                    client = Client(fault, wwr)
                    sender = transport.FrameSender(client.connect, "test", mode=mode)
                    report = await sender.send(raw)
                    self.assertEqual(status().crc, zlib.crc32(raw))
                    self.assertEqual(status().state, 2)
                    self.assertEqual(fw.firmware_frames(), 1)
                    self.assertEqual(words()[-4096:], [256 | b for b in raw])
                    self.assertTrue(fw.firmware_advertising())
                    if fault == "drop_wwr" or not wwr:
                        self.assertTrue(report.fallback_to_response)

    async def test_switch_respects_frame_transfer_lock(self):
        hass = types.SimpleNamespace(data={})
        entity = switch.GTagLED(hass, "test", "aa:bb")
        lock = transport.get_operation_lock(hass, "AA:BB")
        async with lock:
            with self.assertRaisesRegex(HomeAssistantError, "operation is in progress"):
                await entity.async_turn_on()
        client = Client()
        async def connect_client(*_):
            return await client.connect()
        with patch.object(transport, "connect", side_effect=connect_client):
            await entity.async_turn_on()
        self.assertTrue(entity._attr_is_on)
        self.assertFalse(client.is_connected)
        self.assertTrue(fw.firmware_advertising())
