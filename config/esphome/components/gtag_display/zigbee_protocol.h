#pragma once
#include <cstddef>
#include <cstdint>

namespace esphome::gtag_display::zigbee_frame {

constexpr uint16_t CLUSTER_ID = 0xFC11;
constexpr uint8_t COMMAND_PACKET = 0x00;
constexpr uint8_t COMMAND_REPLY = 0x80;
constexpr uint8_t VERSION = 1;
constexpr size_t CHUNK_SIZE = 32;
constexpr size_t MAX_PACKET_SIZE = 7 + CHUNK_SIZE;
constexpr size_t REPLY_SIZE = 20;

enum class Command : uint8_t { BEGIN = 1, DATA = 2, COMMIT = 3, STATUS = 4 };
enum class Result : uint8_t { OK = 0, BUSY = 1, INVALID = 2, SESSION = 3, RECEIVER = 4 };
constexpr uint8_t FLAG_PENDING = 1;
constexpr uint8_t FLAG_RENDERED = 2;

inline uint16_t read16(const uint8_t *p) {
  return uint16_t(p[0]) | (uint16_t(p[1]) << 8);
}
inline uint32_t read32(const uint8_t *p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}
inline void write32(uint8_t *p, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) p[i] = value >> (8 * i);
}

// ZCL commands carry one OCTET_STRING. BEGIN uses the BLE v1 descriptor;
// DATA and COMMIT also carry the session id to reject delayed old packets.
// Reply: version, command, result, session:u32, receiver status[8], flags,
// last successfully rendered frame id:u32. All integers are little endian.
}  // namespace esphome::gtag_display::zigbee_frame
