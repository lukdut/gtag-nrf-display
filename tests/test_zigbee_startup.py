"""Exercise the production startup hook at the SDK's NVRAM/commissioning boundary."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "config/esphome/components/gtag_display/gtag_zigbee_startup.cpp"


class ZigbeeStartupTests(unittest.TestCase):
    def test_saved_radio_mode_does_not_override_yaml_before_commissioning(self):
        with tempfile.TemporaryDirectory(prefix="gtag-zigbee-startup-") as directory:
            folder = Path(directory)
            (folder / "esphome/core").mkdir(parents=True)
            (folder / "esphome/core/defines.h").write_text("")
            (folder / "zigbee").mkdir()
            (folder / "zigbee/zigbee_app_utils.h").write_text("""
#pragma once
#include <stdint.h>
typedef uint8_t zb_bufid_t;
typedef int16_t zb_ret_t;
#define ZB_ZDO_SIGNAL_SKIP_STARTUP 1
#define RET_OK 0
#define ZB_FALSE 0
#define ZB_TRUE 1
unsigned zb_get_app_signal(zb_bufid_t, void *);
zb_ret_t test_signal_status(zb_bufid_t);
#define ZB_GET_APP_SIGNAL_STATUS(b) test_signal_status(b)
void zb_set_rx_on_when_idle(uint8_t);
""")
            harness = folder / "main.cpp"
            harness.write_text("""
#include <cassert>
#include <zigbee/zigbee_app_utils.h>
static unsigned signal;
static zb_ret_t status;
static bool rx, expected_rx;
static unsigned forwards, changes;
extern "C" {
unsigned zb_get_app_signal(zb_bufid_t b, void *) { assert(b == 7); return signal; }
zb_ret_t test_signal_status(zb_bufid_t b) { assert(b == 7); return status; }
void zb_set_rx_on_when_idle(uint8_t value) { rx = value; changes++; }
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
                                    "-I", str(folder), str(SOURCE), str(harness), "-o", str(executable)], check=True)
                    subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
