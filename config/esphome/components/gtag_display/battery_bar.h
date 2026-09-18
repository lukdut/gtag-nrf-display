#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome::gtag_display::battery_bar {

// Generic LiPo OCV curve, not calibrated for a particular battery.
// Zephyr BATTERY_OCV_CURVE_LITHIUM_ION_POLYMER_DEFAULT (Analog Devices AN4189),
// rounded to mV; the requested full-charge endpoint is changed to 4190 mV.
// https://docs.zephyrproject.org/latest/doxygen/html/group__devicetree-battery.html
// Points correspond to 0, 10, ... 100 percent. Keep this table independent of
// the SDK version used for building the firmware.
constexpr uint16_t OCV_MV[] = {3306, 3687, 3741, 3775, 3793, 3821,
                               3884, 3945, 4008, 4086, 4190};

// Return hundredths of a percent, or -1 when measurement is unavailable.
inline int charge_bp(uint16_t mv, uint16_t empty_mv = 3306, uint16_t full_mv = 4190) {
  if (mv == 0xFFFF || empty_mv >= full_mv) return -1;
  if (mv <= empty_mv) return 0;
  if (mv >= full_mv) return 10000;
  // Scale the voltage axis to configured endpoints; retain sub-mV precision
  // during interpolation so changing the range does not introduce extra steps.
  const int voltage = OCV_MV[0] * 1000 +
      int(int64_t(mv - empty_mv) * (OCV_MV[10] - OCV_MV[0]) * 1000 / (full_mv - empty_mv));
  for (size_t i = 1; i < 11; ++i) {
    if (voltage <= OCV_MV[i] * 1000)
      return int(i - 1) * 1000 + (voltage - OCV_MV[i - 1] * 1000) /
          (OCV_MV[i] - OCV_MV[i - 1]);
  }
  return 10000;
}

class Gauge {
 public:
  void set_voltage_range(uint16_t empty_mv, uint16_t full_mv) {
    empty_mv_ = empty_mv;
    full_mv_ = full_mv;
    accepted_bp_ = pixels_ = -1;
  }
  void update(uint16_t mv) {
    const int charge = charge_bp(mv, empty_mv_, full_mv_);
    if (charge < 0) {
      accepted_bp_ = -1;
      pixels_ = -1;
      return;
    }
    const int delta = charge - accepted_bp_;
    // Suppress changes below 2 percentage points. Crossings of empty/full
    // bypass the filter, so the configured full threshold applies immediately.
    const bool endpoint_changed = (charge == 0) != (accepted_bp_ == 0) ||
        (charge == 10000) != (accepted_bp_ == 10000);
    if (accepted_bp_ >= 0 && !endpoint_changed && delta > -200 && delta < 200)
      return;
    accepted_bp_ = charge;
    pixels_ = charge * 256 / 10000;
  }
  int pixels() const { return pixels_; }

 private:
  uint16_t empty_mv_{3306}, full_mv_{4190};
  int accepted_bp_{-1};
  int pixels_{-1};
};

// Reserve exactly the bottom three rows: black filled portion, white remainder.
// Preserve all source bytes when measurement is unavailable/disabled.
inline uint8_t overlay(size_t offset, uint8_t source, int pixels) {
  if (pixels < 0 || offset < 125 * 32)
    return source;
  const int filled = pixels - int(offset % 32) * 8;
  if (filled <= 0) return 0xFF;
  if (filled >= 8) return 0x00;
  return uint8_t(0xFFU << filled);
}

}  // namespace esphome::gtag_display::battery_bar
