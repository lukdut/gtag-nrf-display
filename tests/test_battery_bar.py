"""Exercise the production OCV estimator without an ADC or radio."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BatteryCurveTests(unittest.TestCase):
    def test_curve_interpolation_thresholds_noise_and_custom_range(self):
        with tempfile.TemporaryDirectory(prefix="gtag-battery-curve-") as temporary:
            folder = Path(temporary)
            source = folder / "test.cpp"
            source.write_text(r'''
#include <cassert>
#include "battery_bar.h"
using namespace esphome::gtag_display::battery_bar;
int main() {
  assert(charge_bp(0xFFFF) == -1);
  assert(charge_bp(3000) == 0);
  assert(charge_bp(3306) == 0);
  assert(charge_bp(3741) == 2000);
  assert(charge_bp(3750) == 2264);  // Was 50% with the old linear scale.
  assert(charge_bp(3821) == 5000);
  assert(charge_bp(4008) == 8000);
  assert(charge_bp(4189) < 10000);
  assert(charge_bp(4190) == 10000);
  assert(charge_bp(4500) == 10000);
  int previous = 0;
  for (unsigned mv = 2500; mv <= 4500; ++mv) {
    int value = charge_bp(mv);
    assert(value >= previous && value >= 0 && value <= 10000);
    previous = value;
  }
  Gauge gauge;
  gauge.update(3821);
  assert(gauge.pixels() == 128);
  gauge.update(3827);  // Less than 2 percentage points: no redraw.
  assert(gauge.pixels() == 128);
  gauge.update(3834);  // Accumulated change exceeds 2 percentage points.
  assert(gauge.pixels() > 128);
  gauge.update(4189);
  assert(gauge.pixels() == 255);
  gauge.update(4190);  // Must reach full even though the difference is 1 mV.
  assert(gauge.pixels() == 256);
  gauge.update(4189);
  assert(gauge.pixels() == 255);
  gauge.update(3307);
  gauge.update(3306);
  assert(gauge.pixels() == 0);
  gauge.update(0xFFFF);
  assert(gauge.pixels() == -1);
  gauge.update(3821);
  assert(gauge.pixels() == 128);
  // Shift the entire voltage axis down by 106 mV, retaining its shape.
  gauge.set_voltage_range(3200, 4084);
  gauge.update(3715);
  assert(gauge.pixels() == 128);
  gauge.update(4084);
  assert(gauge.pixels() == 256);
  assert(charge_bp(3200, 3200, 4084) == 0);
  assert(charge_bp(4000, 4200, 3300) == -1);
  assert(charge_bp(4000, 4000, 4000) == -1);
  // Stretch the range to twice its width, also checking fractional interpolation.
  assert(charge_bp(4130, 3100, 4868) == 5000);
  assert(charge_bp(3988, 3100, 4868) == 2264);
}
''')
            executable = folder / "test"
            subprocess.run([
                "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "config/esphome/components/gtag_display"),
                str(source), "-o", str(executable),
            ], check=True)
            subprocess.run([str(executable)], check=True)
