#pragma once
#include <cstddef>
#include <cstdint>
#include "esphome/core/defines.h"
#include "frame_protocol.h"

namespace esphome::gtag_display::firmware_info {
// Firmware version is independent of the HA integration and ESPHome versions.
// Bump for every published firmware; capabilities, not version comparisons,
// decide which packets a host may send.
#ifdef USE_GTAG_WIFI
inline constexpr char VERSION[] = "1.1.0-beta.2";
#else
inline constexpr char VERSION[] = "0.9.0";
#endif
constexpr uint8_t SCHEMA = 1;
constexpr uint8_t ZIGBEE_OPCODE = 7;
constexpr size_t SIZE = 40;
constexpr size_t ZIGBEE_SIZE = 3 + SIZE;
static_assert(sizeof(VERSION) <= 20);
inline void write16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
inline void make(uint8_t *out) {
  for (size_t i = 0; i < SIZE; ++i) out[i] = 0;
  out[0] = SCHEMA;
  out[1] = out[2] = frame::PROTOCOL_VERSION;  // Inclusive supported range.
  out[3] = 1;           // Pixel format: row-LSB, 1 = white.
  for (uint8_t codec = 0; codec < 32; ++codec)
    if (frame::codec_supported(static_cast<frame::Codec>(codec)))
      out[4 + codec / 8] |= 1U << (codec % 8);
  out[8] = 1;           // Feature bit 0: freshness lease/confirmation.
#ifdef USE_GTAG_BATTERY
  out[8] |= 2 | 4 | 8;  // ADC, battery bar, low-voltage protection support.
#endif
  write16(out + 12, 256);
  write16(out + 14, 128);
  write16(out + 16, frame::MAX_ENCODED_SIZE);
#ifdef USE_GTAG_WIFI
  write16(out + 18, frame::MAX_ENCODED_SIZE);
#elif defined(USE_GTAG_ZIGBEE)
  write16(out + 18, 32);
#else
  write16(out + 18, 18);
#endif
  for (size_t i = 0; i < sizeof(VERSION) - 1; ++i) out[20 + i] = VERSION[i];
}
inline void zigbee_reply(uint8_t *out) {
  out[0] = 1; out[1] = ZIGBEE_OPCODE; out[2] = 0;
  make(out + 3);
}
}  // namespace esphome::gtag_display::firmware_info
