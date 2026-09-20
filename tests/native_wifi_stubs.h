#pragma once
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

#define USE_ESP32
#define USE_GTAG_WIFI
#define ESP_LOGCONFIG(tag, ...) ((void)tag)
#define LOG_PIN(label, pin) ((void)pin)

namespace wifi_sim {
inline uint64_t now_us = 0;
inline bool levels[4]{};
inline uint16_t word = 0;
inline unsigned bits = 0;
inline std::vector<uint16_t> words;
using Decode = int (*)(unsigned char *, size_t, size_t *, const unsigned char *, size_t);
inline Decode decode;
}
inline int mbedtls_base64_decode(unsigned char *out, size_t size, size_t *length, const unsigned char *in, size_t in_size) {
  return wifi_sim::decode(out, size, length, in, in_size);
}
namespace esphome {
inline uint32_t millis() { return wifi_sim::now_us / 1000; }
inline void delay_microseconds_safe(uint32_t us) { wifi_sim::now_us += us; }
class Component {
 public:
  virtual ~Component() = default;
  virtual void setup() {}
  virtual void loop() {}
  virtual void dump_config() {}
};
class GPIOPin {
 public:
  explicit GPIOPin(unsigned index) : index_(index) {}
  void setup() {}
  void digital_write(bool level) {
    using namespace wifi_sim;
    if (index_ == 2 && !level) { word = 0; bits = 0; }
    if (index_ == 1 && level && !levels[1] && !levels[2]) { word = (word << 1) | levels[0]; ++bits; }
    if (index_ == 2 && level && !levels[2] && bits) { assert(bits == 9); words.push_back(word); }
    levels[index_] = level;
  }
 private:
  unsigned index_;
};
}
