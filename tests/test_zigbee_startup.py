"""Exercise the production startup hook at the SDK's NVRAM/commissioning boundary."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "config/esphome/components/gtag_display/gtag_zigbee_startup.cpp"


class ZigbeeStartupTests(unittest.TestCase):
    def test_saved_radio_mode_and_recovery_after_sdk_rejoin_timeout(self):
        with tempfile.TemporaryDirectory(prefix="gtag-zigbee-startup-") as directory:
            folder = Path(directory)
            (folder / "esphome/core").mkdir(parents=True)
            (folder / "esphome/core/defines.h").write_text("")
            (folder / "zigbee").mkdir()
            (folder / "zigbee/zigbee_app_utils.h").write_text("""
#pragma once
#include <stdint.h>
typedef uint8_t zb_bufid_t;
typedef uint8_t zb_uint8_t;
typedef int16_t zb_ret_t;
#define ZB_ZDO_SIGNAL_SKIP_STARTUP 1
#define RET_OK 0
#define ZB_FALSE 0
#define ZB_TRUE 1
unsigned zb_get_app_signal(zb_bufid_t, void *);
zb_ret_t test_signal_status(zb_bufid_t);
#define ZB_GET_APP_SIGNAL_STATUS(b) test_signal_status(b)
void zb_set_rx_on_when_idle(uint8_t);
bool zb_bdb_is_factory_new();
bool zb_zdo_joined();
void user_input_indicate();
typedef void (*zb_callback_t)(zb_uint8_t);
zb_ret_t test_alarm(zb_callback_t, uint8_t, uint32_t);
zb_ret_t test_alarm_cancel(zb_callback_t, uint8_t);
#define ZB_ALARM_ANY_PARAM 255
#define ZB_MILLISECONDS_TO_BEACON_INTERVAL(ms) ((ms) / 16)
#define ZB_SCHEDULE_APP_ALARM(cb, p, t) test_alarm(cb, p, t)
#define ZB_SCHEDULE_APP_ALARM_CANCEL(cb, p) test_alarm_cancel(cb, p)
""")
            harness = folder / "main.cpp"
            harness.write_text("""
#include <cassert>
#include <zigbee/zigbee_app_utils.h>
static unsigned signal;
static zb_ret_t status;
static bool rx, expected_rx;
static unsigned forwards, changes;
static bool factory_new = true, joined = false, waiting_for_input = false, fail_alarm = false;
static unsigned now_ms = 0, due_ms = 0, sdk_deadline = 0, restarted = 0, user_inputs = 0;
static zb_callback_t alarm = nullptr;
extern "C" {
unsigned zb_get_app_signal(zb_bufid_t b, void *) { assert(b == 7); return signal; }
zb_ret_t test_signal_status(zb_bufid_t b) { assert(b == 7); return status; }
void zb_set_rx_on_when_idle(uint8_t value) { rx = value; changes++; }
bool zb_bdb_is_factory_new() { return factory_new; }
bool zb_zdo_joined() { return joined; }
void user_input_indicate() {
  user_inputs++;
  // Model the SDK contract: only a timed-out procedure can be resumed.
  if (waiting_for_input && !joined) {
    waiting_for_input = false;
    sdk_deadline = now_ms + 200000;
    restarted++;
  }
}
zb_ret_t test_alarm(zb_callback_t cb, uint8_t param, uint32_t delay) {
  assert(param == 0 && delay == ZB_MILLISECONDS_TO_BEACON_INTERVAL(60000));
  assert(alarm == nullptr);  // Never queue duplicate recovery alarms.
  if (fail_alarm) return -1;
  alarm = cb;
  due_ms = now_ms + 60000;
  return RET_OK;
}
zb_ret_t test_alarm_cancel(zb_callback_t cb, uint8_t param) {
  assert(cb == alarm && param == ZB_ALARM_ANY_PARAM);
  alarm = nullptr;
  return RET_OK;
}
zb_ret_t __real_zigbee_default_signal_handler(zb_bufid_t b) {
  assert(b == 7);
  // Commissioning must already see the desired capability. An override
  // deferred until after this handler is too late for the parent device.
  assert(rx == expected_rx);
  forwards++;
  return -17;
}
zb_ret_t __wrap_zigbee_default_signal_handler(zb_bufid_t);
}
static void advance(unsigned milliseconds) {
  const unsigned end = now_ms + milliseconds;
  while (alarm != nullptr && due_ms <= end) {
    now_ms = due_ms;
    if (sdk_deadline && now_ms >= sdk_deadline) waiting_for_input = true;
    auto callback = alarm;
    alarm = nullptr;
    callback(0);
  }
  now_ms = end;
}
int main() {
  // Simulate an existing network saved by the opposite firmware profile.
  signal = ZB_ZDO_SIGNAL_SKIP_STARTUP;
  status = RET_OK;
  rx = GTAG_ZIGBEE_SLEEPY;
  expected_rx = !GTAG_ZIGBEE_SLEEPY;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(changes == 1 && forwards == 1);
  // Reboot: NVRAM may still contain the old value. The fix must repeat.
  rx = GTAG_ZIGBEE_SLEEPY;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(changes == 2 && forwards == 2);
  // Unrelated events and failed startup must remain untouched and forwarded.
  signal = 127;
  rx = expected_rx = true;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  signal = ZB_ZDO_SIGNAL_SKIP_STARTUP;
  status = -1;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(changes == 2 && forwards == 4);
  // No automatic search for a new/factory-reset device.
  assert(alarm == nullptr);
  advance(600000);
  assert(user_inputs == 0);

  // Paired and joined: ordinary operation adds no periodic wakeups.
  factory_new = false;
  joined = true;
  signal = 127;
  status = RET_OK;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(alarm == nullptr);

  // Losing the parent starts the SDK's bounded search window. Our alarm
  // must not interfere with ongoing attempts, even under repeated signals.
  joined = false;
  sdk_deadline = now_ms + 200000;
  for (unsigned i = 0; i < 20; i++)
    assert(__wrap_zigbee_default_signal_handler(7) == -17);
  advance(180000);
  assert(restarted == 0 && user_inputs == 3 && alarm != nullptr);
  advance(60000);  // SDK has stopped after 200 s, recovery resumes it at 240 s.
  assert(restarted == 1 && !waiting_for_input && alarm != nullptr);
  advance(240000);  // A prolonged outage must not permanently exhaust retries.
  assert(restarted == 2 && alarm != nullptr);

  // Returning to the network cancels the alarm and stops all retry work.
  joined = true;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(alarm == nullptr);
  unsigned previous_inputs = user_inputs;
  advance(600000);
  assert(user_inputs == previous_inputs);

  // A full scheduler is retried on the next signal, without a stuck flag.
  joined = false;
  fail_alarm = true;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(alarm == nullptr);
  fail_alarm = false;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(alarm != nullptr);
  // Removal/factory reset stops recovery too.
  factory_new = true;
  assert(__wrap_zigbee_default_signal_handler(7) == -17);
  assert(alarm == nullptr);
  advance(600000);
  assert(user_inputs == previous_inputs);
}
""")
            # SDK declarations have C linkage, like their production include.
            harness.write_text(harness.read_text().replace(
                "#include <zigbee/zigbee_app_utils.h>",
                'extern "C" {\n#include <zigbee/zigbee_app_utils.h>\n}'))
            for sleepy in (0, 1):
                with self.subTest(sleepy=sleepy):
                    executable = folder / f"startup-{sleepy}"
                    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                                    "-DUSE_GTAG_ZIGBEE", f"-DGTAG_ZIGBEE_SLEEPY={sleepy}",
                                    "-DCONFIG_ZIGBEE_ROLE_END_DEVICE",
                                    "-I", str(folder), str(SOURCE), str(harness), "-o", str(executable)], check=True)
                    subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
