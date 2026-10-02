#pragma once
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

#ifndef USE_ESP8266
#define USE_ESP32
#endif
#define USE_GTAG_WIFI
#define ESP_LOGCONFIG(tag, ...) ((void)tag)
#define LOG_PIN(label, pin) ((void)pin)

namespace wifi_sim {
inline uint64_t now_us = 0;
inline bool levels[4]{};
inline uint16_t word = 0;
inline unsigned bits = 0;
inline std::vector<uint16_t> words;
inline unsigned flash_reads = 0, yields = 0;
}
#define PROGMEM __attribute__((section("gtag_flash"), aligned(4)))
inline uint8_t pgm_read_byte(const void *p) {
  ++wifi_sim::flash_reads;
  return *static_cast<const uint8_t *>(p);
}
inline void *memcpy_P(void *out, const void *in, size_t size) {
  for (size_t i = 0; i < size; ++i)
    static_cast<uint8_t *>(out)[i] = pgm_read_byte(static_cast<const uint8_t *>(in) + i);
  return out;
}
namespace esphome {
inline void yield() { ++wifi_sim::yields; }
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
