#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome::gtag_display::freshness {

constexpr uint32_t MAX_TIMEOUT_S = 86400;

// Callers serialize access. Relative uptime works across the uint32 wrap;
// deadlines are at most one day, well below the half-wrap interval.
class Lease {
 public:
  void frame(uint32_t id, uint32_t crc, uint32_t timeout_s, uint32_t now) {
    id_ = id;
    crc_ = crc;
    timeout_ms_ = timeout_s * 1000;
    confirmed_ = now;
    sequence_ = 0;
    valid_ = true;
    expired_ = false;
  }
  void clear() { valid_ = false; }
  bool renew(uint32_t id, uint32_t crc, uint32_t sequence, uint32_t now) {
    if (!valid_ || id != id_ || crc != crc_ || sequence == 0 || sequence < sequence_)
      return false;
    // A retry of an acknowledged command must not extend the deadline again.
    if (sequence > sequence_) {
      sequence_ = sequence;
      confirmed_ = now;
      expired_ = false;
    }
    return true;
  }
  uint32_t remaining(uint32_t now) {
    if (!valid_ || timeout_ms_ == 0) return 0;
    if (expired_) return 0;
    const uint32_t age = now - confirmed_;
    if (age >= timeout_ms_) {
      expired_ = true;
      return 0;
    }
    return timeout_ms_ - age;
  }
  bool expired(uint32_t now) {
    return valid_ && timeout_ms_ != 0 && remaining(now) == 0;
  }

 private:
  uint32_t id_{0}, crc_{0}, timeout_ms_{0}, confirmed_{0}, sequence_{0};
  bool valid_{false};
  bool expired_{false};
};

// 24x24 crossed-out radio symbol, top-right (x=232..255, y=0..23).
// XOR only the glyph, preserving the original framebuffer and its CRC.
constexpr uint32_t ICON[] = {
    0x000000, 0x000000, 0x01ff00, 0x0fffd0,
    0x3e00b8, 0x780074, 0xe000ee, 0x8001c3,
    0x00fb81, 0x03f700, 0x0f0ee0, 0x1c1c70,
    0x183830, 0x007000, 0x00ec00, 0x01de00,
    0x038300, 0x070000, 0x0e0000, 0x1c1000,
    0x383800, 0x701000, 0x200000, 0x000000,
};
inline uint8_t mask(size_t offset) {
  const size_t y = offset / 32, x_byte = offset % 32;
  return y < 24 && x_byte >= 29 ? uint8_t(ICON[y] >> ((x_byte - 29) * 8)) : 0;
}

}  // namespace esphome::gtag_display::freshness
