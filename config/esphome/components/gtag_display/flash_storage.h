#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

// Also usable by the standalone native codec tests, without ESPHome headers.
#ifdef USE_ESP8266
#include <pgmspace.h>
#define GTAG_PROGMEM PROGMEM
#else
#define GTAG_PROGMEM
#endif

namespace esphome::gtag_display::flash_storage {
inline uint8_t byte(const uint8_t *p) {
#ifdef USE_ESP8266
  return pgm_read_byte(p);
#else
  return *p;
#endif
}
inline uint16_t u16(const uint8_t *p) { return uint16_t(byte(p)) | (uint16_t(byte(p + 1)) << 8); }
inline uint32_t u32(const uint8_t *p) { return u16(p) | (uint32_t(u16(p + 2)) << 16); }
inline void copy(void *out, const void *in, size_t size) {
#ifdef USE_ESP8266
  memcpy_P(out, in, size);
#else
  std::memcpy(out, in, size);
#endif
}
}  // namespace esphome::gtag_display::flash_storage
