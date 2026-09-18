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
module("bleak_retry_connector", establish_connection=None, BleakClientWithServiceCache=object)
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
    global build, fw, fw_without_battery, fw_zigbee
    build = tempfile.TemporaryDirectory(prefix="gtag-tests-")
    path = Path(build.name)
    # Redirect platform includes to the host model, without editing the driver.
    headers = [
        "esphome/core/component.h", "esphome/core/application.h",
        "esphome/core/hal.h", "esphome/core/log.h", "esphome/core/defines.h",
        "zephyr/bluetooth/bluetooth.h", "zephyr/bluetooth/conn.h",
        "zephyr/bluetooth/gatt.h", "zephyr/bluetooth/uuid.h", "zephyr/device.h",
        "zephyr/devicetree.h", "zephyr/drivers/gpio.h",
        "zephyr/dt-bindings/gpio/nordic-nrf-gpio.h", "zephyr/kernel.h", "hal/nrf_gpio.h",
        "zephyr/drivers/adc.h", "zephyr/dt-bindings/adc/nrf-saadc.h",
    ]
    for header in headers:
        target = path / header
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('#include "native_stubs.h"\n')
    library = path / "driver.so"
    command = [
        "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
        "-I", str(path), "-I", str(ROOT / "tests"), "-I", str(COMPONENT),
        str(ROOT / "tests/native_driver.cpp"),
    ]
    subprocess.run([*command, "-DUSE_GTAG_BATTERY", "-o", str(library)], check=True)
    disabled_library = path / "driver_without_battery.so"
    subprocess.run([*command, "-o", str(disabled_library)], check=True)
    zigbee_library = path / "driver_zigbee.so"
    subprocess.run([*command, "-DUSE_GTAG_ZIGBEE", "-DUSE_GTAG_BATTERY",
                    "-o", str(zigbee_library)], check=True)
    fw = ctypes.CDLL(str(library))
    fw_without_battery = ctypes.CDLL(str(disabled_library))
    fw_zigbee = ctypes.CDLL(str(zigbee_library))
    fw_zigbee.firmware_packet.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    fw.firmware_calibration.argtypes = [ctypes.c_float]
    for variant in (fw, fw_without_battery):
        variant.firmware_battery.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        variant.firmware_set_time.argtypes = [ctypes.c_uint32]
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


def begin(raw, frame_id=1, bad_crc=False, timeout=0):
    encoded = codec.encode_best(raw)
    packet = struct.pack("<BBB HII", 1, 1, encoded.codec, len(encoded.payload),
                         frame_id, encoded.raw_crc32 ^ int(bad_crc))
    packet += struct.pack('<I', timeout)
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

    def test_freshness_icon_inverts_only_glyph_and_restores_exact_frame(self):
        for fill in (0x00, 0xff, 0x55):
            with self.subTest(fill=fill):
                start()
                raw = bytes([fill]) * 4096
                fw.firmware_connect()
                begin(raw, timeout=60)
                commit()
                fw.firmware_disconnect()
                fw.firmware_run(0)
                self.assertEqual(bytes(v & 255 for v in words()[-4096:]), raw)
                before = fw.firmware_frames()
                fw.firmware_run(60_000)
                overlay = bytes(v & 255 for v in words()[-4096:])
                changed = [i for i, (a, b) in enumerate(zip(raw, overlay)) if a != b]
                self.assertTrue(changed)
                self.assertTrue(all(i // 32 < 24 and i % 32 >= 29 for i in changed))
                self.assertEqual(status().crc, zlib.crc32(raw))
                self.assertEqual(fw.firmware_frames(), before + 1)
                fw.firmware_run(60_000)
                self.assertEqual(fw.firmware_frames(), before + 1, 'No repeated expiry redraws')
                fw.firmware_connect()
                packet = struct.pack('<BIII', 3, 1, zlib.crc32(raw), 1)
                self.assertEqual(fw.firmware_control(packet, len(packet)), len(packet))
                fw.firmware_disconnect()
                fw.firmware_run(0)
                self.assertEqual(bytes(v & 255 for v in words()[-4096:]), raw)

    def test_freshness_retries_wrong_sessions_and_disabled_timeout(self):
        raw = b'\xff' * 4096
        fw.firmware_connect()
        begin(raw, timeout=60)
        commit()
        fw.firmware_disconnect()
        fw.firmware_run(30_000)
        fw.firmware_connect()
        for frame_id, crc, seq in ((2, zlib.crc32(raw), 1), (1, 1234, 1), (1, zlib.crc32(raw), 0)):
            packet = struct.pack('<BIII', 3, frame_id, crc, seq)
            self.assertLess(fw.firmware_control(packet, len(packet)), 0)
        packet = struct.pack('<BIII', 3, 1, zlib.crc32(raw), 2)
        self.assertEqual(fw.firmware_control(packet, len(packet)), len(packet))
        fw.firmware_disconnect()
        fw.firmware_run(30_000)
        fw.firmware_connect()
        self.assertEqual(fw.firmware_control(packet, len(packet)), len(packet))
        old = struct.pack('<BIII', 3, 1, zlib.crc32(raw), 1)
        self.assertLess(fw.firmware_control(old, len(old)), 0)
        commit()  # Duplicate COMMIT also cannot extend the deadline.
        fw.firmware_disconnect()
        fw.firmware_run(31_000)
        self.assertNotEqual(bytes(v & 255 for v in words()[-4096:]), raw)
        fw.firmware_connect()
        begin(raw, frame_id=2, timeout=0)
        commit()
        fw.firmware_disconnect()
        fw.firmware_run(300_000)
        self.assertEqual(bytes(v & 255 for v in words()[-4096:]), raw)

    def test_expired_icon_stays_after_an_entire_uptime_wrap(self):
        raw = b'\xff' * 4096
        fw.firmware_connect()
        begin(raw, timeout=60)
        commit()
        fw.firmware_disconnect()
        fw.firmware_run(60_000)
        expired = bytes(v & 255 for v in words()[-4096:])
        self.assertNotEqual(expired, raw)
        fw.firmware_clock_wrap(31_000)  # Uptime now looks younger than the lease.
        fw.firmware_connect()
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertEqual(bytes(v & 255 for v in words()[-4096:]), expired)

    def test_freshness_deadline_crosses_uptime_wrap(self):
        raw = b'\xff' * 4096
        fw.firmware_set_time(0xfffff000)
        fw.firmware_connect()
        begin(raw, timeout=60)
        commit()
        fw.firmware_disconnect()
        fw.firmware_run(30_000)
        self.assertEqual(bytes(v & 255 for v in words()[-4096:]), raw)
        fw.firmware_run(31_000)
        self.assertNotEqual(bytes(v & 255 for v in words()[-4096:]), raw)

    def test_battery_cache_sampling_errors_and_recovery(self):
        fw.firmware_create(0, 1000)
        out = ctypes.create_string_buffer(2)
        self.assertEqual(fw.firmware_battery(out, 2, 0), 2)
        self.assertEqual(out.raw, b"\xff\xff")
        fw.firmware_run(999)
        self.assertEqual(fw.firmware_adc_reads(), 0)
        fw.firmware_run(1)
        fw.firmware_battery(out, 2, 0)
        self.assertEqual(int.from_bytes(out.raw, "little"), 4200)
        for _ in range(100):
            fw.firmware_battery(out, 2, 0)
        self.assertEqual(fw.firmware_adc_reads(), 1)
        fw.firmware_adc_value(3072, 0)  # 1.8V at the input.
        fw.firmware_run(299_999)
        self.assertEqual(fw.firmware_adc_reads(), 1)
        fw.firmware_run(1)
        fw.firmware_battery(out, 2, 0)
        self.assertEqual(int.from_bytes(out.raw, "little"), 3600)
        fw.firmware_adc_value(3072, -5)
        fw.firmware_run(300_000)
        fw.firmware_battery(out, 2, 0)
        self.assertEqual(out.raw, b"\xff\xff")
        fw.firmware_adc_value(3072, 0)
        fw.firmware_calibration(1.01)
        fw.firmware_run(300_000)
        fw.firmware_battery(out, 2, 0)
        self.assertEqual(int.from_bytes(out.raw, "little"), 3636)
        fw.firmware_adc_value(4095, 0)
        fw.firmware_run(300_000)
        fw.firmware_battery(out, 2, 0)
        self.assertEqual(out.raw, b"\xff\xff")
        self.assertTrue(fw.firmware_advertising())

    def test_battery_disabled_build_never_samples(self):
        fw_without_battery.firmware_create(0, 1000)
        fw_without_battery.firmware_run(900_000)
        out = ctypes.create_string_buffer(2)
        fw_without_battery.firmware_battery(out, 2, 0)
        self.assertEqual(out.raw, b"\xff\xff")
        self.assertEqual(fw_without_battery.firmware_adc_reads(), 0)

    def test_battery_gatt_offsets_and_reads_during_connection(self):
        fw.firmware_connect()
        out = ctypes.create_string_buffer(1)
        self.assertEqual(fw.firmware_battery(out, 1, 0), 1)
        self.assertEqual(out.raw, (4200).to_bytes(2, "little")[:1])
        self.assertEqual(fw.firmware_battery(out, 1, 1), 1)
        self.assertEqual(out.raw, (4200).to_bytes(2, "little")[1:])
        self.assertEqual(fw.firmware_battery(out, 1, 2), 0)
        self.assertLess(fw.firmware_battery(out, 1, 3), 0)
        before = fw.firmware_words()
        fw.firmware_run(300_000)
        self.assertEqual(fw.firmware_words(), before)
        self.assertEqual(fw.firmware_adc_reads(), 2)
        fw.firmware_disconnect()
        fw.firmware_run(0)
        self.assertTrue(fw.firmware_advertising())

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

    def test_remapped_gpio_preserves_lcd_protocol_on_both_ports(self):
        reference = reference_words(ROOT / "esp32-diagram/ESP32-gtag.sal")
        raw = bytes(word & 255 for word in reference[-4096:])
        for assignment in ((2, 3, 4, 5, 28), (32, 47, 34, 37, 31), (36, 11, 45, 38, 2)):
            with self.subTest(assignment=assignment):
                fw.firmware_create_with_pins(0, 1000, *assignment)
                fw.firmware_run(4000)
                fw.firmware_connect()
                begin(raw)
                self.assertEqual(commit(), 1)
                fw.firmware_disconnect()
                fw.firmware_run(0)
                self.assertEqual(words(), reference)
                self.assertEqual(fw.firmware_reset_high() - fw.firmware_reset_low(), 50)
                self.assertEqual(fw.firmware_battery_mv(), 4200)

    def test_all_external_adc_inputs_and_remapping_without_battery(self):
        for pin in (2, 3, 4, 5, 28, 29, 30, 31):
            with self.subTest(pin=pin):
                fw.firmware_create_with_pins(0, 1000, 11, 36, 38, 45, pin)
                fw.firmware_run(1100)
                self.assertEqual(fw.firmware_adc_reads(), 1)
                self.assertEqual(fw.firmware_battery_mv(), 4200)
        # A pin normally used for ADC can drive the LCD when measurement is off.
        fw_without_battery.firmware_create_with_pins(1, 1000, 31, 30, 29, 28, 31)
        fw_without_battery.firmware_run(4000)
        self.assertEqual(fw_without_battery.firmware_adc_reads(), 0)
        self.assertEqual(fw_without_battery.firmware_frames(), 1)


class ZigbeeDisplayTests(unittest.TestCase):
    """Exercise the shared driver without BLE; radio/ZCL need hardware testing."""

    def setUp(self):
        fw_zigbee.firmware_create(3, 1000)  # Boot checkerboard.

    def packet(self, data):
        reply = ctypes.create_string_buffer(20)
        fw_zigbee.firmware_packet(data, len(data), reply)
        return reply.raw

    def send_frame(self, raw, session=123, bad_crc=False):
        encoded = codec.encode_best(raw)
        begin = struct.pack('<BBBHII', 1, 1, encoded.codec, len(encoded.payload), session,
                            encoded.raw_crc32 ^ int(bad_crc))
        self.assertEqual(self.packet(begin)[2], 0)
        for offset in range(0, len(encoded.payload), 32):
            data = struct.pack('<BIH', 2, session, offset) + encoded.payload[offset:offset + 32]
            self.assertEqual(self.packet(data)[2], 0)
        return self.packet(struct.pack('<BI', 3, session))

    def test_remapped_zigbee_pins_deliver_the_same_lcd_bytes(self):
        fw_zigbee.firmware_create_with_pins(0, 1000, 6, 8, 15, 17, 4)
        fw_zigbee.firmware_run(4000)
        raw = bytes(range(256)) * 16
        self.send_frame(raw)
        fw_zigbee.firmware_run(0)
        shown = bytes(fw_zigbee.firmware_word(i) & 255 for i in
                      range(fw_zigbee.firmware_words() - 4096, fw_zigbee.firmware_words()))
        self.assertEqual(shown, raw)
        self.assertEqual(fw_zigbee.firmware_battery_mv(), 4200)
        self.assertEqual(fw_zigbee.firmware_advertising(), 0)

    def test_full_frame_crc_and_lcd_confirmation_are_separate(self):
        fw_zigbee.firmware_run(4000)
        reply = self.send_frame(bytes(range(256)) * 16)
        self.assertEqual(reply[7], 2)
        self.assertTrue(reply[15] & 1)  # Pending LCD write.
        self.assertEqual(fw_zigbee.firmware_frames(), 1)
        fw_zigbee.firmware_run(0)
        reply = self.packet(struct.pack('<BI', 4, 123))
        self.assertEqual(reply[15], 2)
        self.assertEqual(int.from_bytes(reply[16:20], 'little'), 123)
        self.assertEqual(fw_zigbee.firmware_frames(), 2)
        self.packet(struct.pack('<BI', 3, 123))
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_frames(), 2)

    def test_old_session_packets_and_bad_crc_do_not_render(self):
        fw_zigbee.firmware_run(4000)
        self.packet(struct.pack('<BBBHII', 1, 1, 0, 4096, 567, 0))
        before = fw_zigbee.firmware_frames()
        for command in (struct.pack('<BIH', 2, 123, 0) + b'bad', struct.pack('<BI', 3, 123)):
            reply = self.packet(command)
            self.assertEqual(reply[2], 3)
            self.assertEqual(reply[8:10], b'\0\0')
        reply = self.send_frame(b'\xff' * 4096, session=567, bad_crc=True)
        self.assertEqual(reply[2], 4)
        self.assertEqual(reply[14], 5)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_frames(), before)

    def test_malformed_requests_leave_receiver_unchanged(self):
        fw_zigbee.firmware_run(4000)
        for packet in (b'', b'\x01', b'\x02\0\0\0\0\0\0', b'\x03', b'\x04\0',
                       b'\x05' + bytes(4), b'\x02' + bytes(39), bytes(300)):
            self.assertEqual(self.packet(packet)[2], 2)
        self.assertEqual(self.packet(struct.pack('<BI', 4, 0))[7], 0)

    def test_pending_verified_frame_cannot_be_replaced_by_new_begin_or_pattern(self):
        fw_zigbee.firmware_run(4000)
        self.send_frame(b'\x00' * 4096)
        reply = self.packet(struct.pack('<BBBHII', 1, 1, 0, 4096, 999, 0))
        self.assertEqual(reply[2], 1)
        fw_zigbee.firmware_pattern(0)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_word(fw_zigbee.firmware_words() - 1), 0x100)
        self.assertEqual(int.from_bytes(self.packet(struct.pack('<BI', 4, 0))[16:20], 'little'), 123)

    def test_retry_same_frame_after_diagnostic_restores_image(self):
        fw_zigbee.firmware_run(4000)
        self.send_frame(b'\x00' * 4096)
        fw_zigbee.firmware_run(0)
        fw_zigbee.firmware_pattern(0)
        fw_zigbee.firmware_run(0)
        before = fw_zigbee.firmware_frames()
        self.assertFalse(self.packet(struct.pack('<BI', 4, 0))[15] & 2)
        self.send_frame(b'\x00' * 4096)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_frames(), before + 1)
        self.assertEqual(fw_zigbee.firmware_word(fw_zigbee.firmware_words() - 1), 0x100)

    def test_pattern_is_queued_then_rendered_and_driver_returns_to_idle(self):
        fw_zigbee.firmware_run(4000)
        self.assertEqual(fw_zigbee.firmware_frames(), 1)
        before = fw_zigbee.firmware_words()
        self.assertEqual(fw_zigbee.firmware_pattern(0), 1)
        self.assertEqual(fw_zigbee.firmware_words(), before)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_frames(), 2)
        rendered = [fw_zigbee.firmware_word(i) for i in
                    range(fw_zigbee.firmware_words() - 4096, fw_zigbee.firmware_words())]
        self.assertEqual(rendered, [0x1FF] * 4096)
        before = fw_zigbee.firmware_loops()
        fw_zigbee.firmware_run(60_000)
        self.assertEqual(fw_zigbee.firmware_loops(), before)
        self.assertEqual(fw_zigbee.firmware_adv_attempts(), 0)

    def test_latest_command_before_lcd_ready_wins(self):
        fw_zigbee.firmware_pattern(0)
        fw_zigbee.firmware_pattern(1)
        fw_zigbee.firmware_run(100)
        self.assertEqual(fw_zigbee.firmware_words(), 0)
        fw_zigbee.firmware_run(4000)
        self.assertEqual(fw_zigbee.firmware_frames(), 1)
        rendered = [fw_zigbee.firmware_word(i) for i in
                    range(fw_zigbee.firmware_words() - 4096, fw_zigbee.firmware_words())]
        self.assertEqual(rendered, [0x100] * 4096)

    def test_invalid_command_and_command_during_loop_disable(self):
        fw_zigbee.firmware_run(4000)
        before = fw_zigbee.firmware_loops()
        for pattern in (4, 255, 256):
            self.assertEqual(fw_zigbee.firmware_pattern(pattern), 0)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_loops(), before)
        fw_zigbee.firmware_race_pattern(3)
        fw_zigbee.firmware_run(0)
        self.assertEqual(fw_zigbee.firmware_frames(), 2)
        self.assertEqual(fw_zigbee.firmware_word(fw_zigbee.firmware_words() - 1), 0x10F)
        self.assertEqual(fw_zigbee.firmware_adv_attempts(), 0)

    def test_battery_sampling_works_without_bluetooth(self):
        self.assertEqual(fw_zigbee.firmware_battery_mv(), 0xFFFF)
        fw_zigbee.firmware_run(1000)
        self.assertEqual(fw_zigbee.firmware_battery_mv(), 4200)
        for _ in range(20):
            fw_zigbee.firmware_battery_mv()
        self.assertEqual(fw_zigbee.firmware_adc_reads(), 1)
        fw_zigbee.firmware_adc_value(3072, 0)
        fw_zigbee.firmware_run(300_000)
        self.assertEqual(fw_zigbee.firmware_battery_mv(), 3600)
        self.assertEqual(fw_zigbee.firmware_adc_reads(), 2)
        self.assertEqual(fw_zigbee.firmware_adv_attempts(), 0)


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
