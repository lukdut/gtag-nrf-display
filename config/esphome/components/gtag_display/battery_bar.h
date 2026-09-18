#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome::gtag_display::battery_bar {

// An approximate voltage scale, not a calibrated state-of-charge estimate.
// Empty at 3.3 V, full at 4.2 V; ignore <20 mV changes between accepted samples.
class Gauge {
 public:
  void update(uint16_t mv) {
    if (mv == 0xFFFF) {
      accepted_mv_ = -1;
      pixels_ = -1;
      return;
    }
    const int delta = int(mv) - accepted_mv_;
    if (accepted_mv_ >= 0 && delta > -20 && delta < 20)
      return;
    accepted_mv_ = mv;
    pixels_ = mv <= 3300 ? 0 : mv >= 4200 ? 256 : (int(mv) - 3300) * 256 / 900;
  }
  int pixels() const { return pixels_; }

 private:
  int accepted_mv_{-1};
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
