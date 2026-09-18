"""Verify recovery thresholds and the actual linker wrapper independent of Zephyr."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'config/esphome/components/gtag_display'


class BatteryGuardTests(unittest.TestCase):
    def test_thresholds_and_linker_gate(self):
        with tempfile.TemporaryDirectory(prefix='gtag-guard-') as temporary:
            folder = Path(temporary)
            (folder / 'esphome/core').mkdir(parents=True)
            (folder / 'esphome/core/defines.h').write_text('')
            # Separate TU is important: --wrap rewrites external symbol references.
            (folder / 'radio.cpp').write_text('extern int starts; extern "C" void zigbee_enable() { ++starts; }')
            (folder / 'test.cpp').write_text(r'''
#include <cassert>
#include "battery_guard.h"
#include "gtag_radio_gate.h"
extern "C" void zigbee_enable();
int starts = 0;
using namespace esphome::gtag_display;
int main() {
  using namespace battery_guard;
  Guard guard;
  guard.configure(3306, 3450);
  assert(guard.update(0xFFFF) == State::WAITING);
  assert(guard.update(3400) == State::LOW);
  assert(guard.update(3449) == State::LOW);
  assert(guard.update(3450) == State::RUNNING);
  assert(guard.update(0xFFFF) == State::RUNNING);
  assert(guard.update(3306) == State::RUNNING);
  assert(guard.update(3305) == State::REBOOT);
  assert(guard.update(4000) == State::REBOOT);
  zigbee_enable();
  assert(starts == 0);
  zigbee_radio_gate.permit();
  assert(starts == 1);
  zigbee_radio_gate.permit(); zigbee_enable();
  assert(starts == 1);
  zigbee_radio_gate = {};
  zigbee_radio_gate.permit();
  assert(starts == 1);
  zigbee_enable();
  assert(starts == 2);
}
''')
            executable = folder / 'test'
            subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-DUSE_GTAG_BATTERY', '-DUSE_GTAG_ZIGBEE',
                            '-I', str(folder), '-I', str(COMPONENT),
                            str(folder / 'test.cpp'), str(folder / 'radio.cpp'),
                            str(COMPONENT / 'gtag_radio_gate.cpp'), '-Wl,--wrap=zigbee_enable',
                            '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
