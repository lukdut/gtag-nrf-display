#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace esphome {
namespace gtag_display {
namespace frame {

static constexpr size_t RAW_FRAME_SIZE = 4096;
static constexpr size_t MAX_ENCODED_SIZE = RAW_FRAME_SIZE;

enum class Codec : uint8_t {
  RAW = 0x00,
  WHITE_RLE_V1 = 0x01,
};

enum class DecodeError : uint8_t {
  NONE = 0,
  UNSUPPORTED_CODEC,
  INPUT_SIZE,
  INPUT_TRUNCATED,
  OUTPUT_OVERFLOW,
  OUTPUT_SIZE,
};

struct DecodeResult {
  bool ok;
  DecodeError error;
  size_t input_used;
  size_t output_written;
};

inline bool codec_supported(Codec codec) {
  return codec == Codec::RAW || codec == Codec::WHITE_RLE_V1;
}

inline DecodeResult decode_frame(
    Codec codec,
    const uint8_t *encoded,
    size_t encoded_size,
    uint8_t *raw,
    size_t raw_size) {

  if (encoded == nullptr || raw == nullptr || raw_size != RAW_FRAME_SIZE) {
    return {false, DecodeError::OUTPUT_SIZE, 0, 0};
  }

  if (codec == Codec::RAW) {
    if (encoded_size != RAW_FRAME_SIZE) {
      return {false, DecodeError::INPUT_SIZE, 0, 0};
    }

    std::memcpy(raw, encoded, RAW_FRAME_SIZE);
    return {true, DecodeError::NONE, RAW_FRAME_SIZE, RAW_FRAME_SIZE};
  }

  if (codec != Codec::WHITE_RLE_V1) {
    return {false, DecodeError::UNSUPPORTED_CODEC, 0, 0};
  }

  size_t ip = 0;
  size_t op = 0;

  // WHITE_RLE_V1 token format:
  //
  // 0xxxxxxx : literal block, length=(token & 0x7F)+1,
  //             followed by exactly that many literal bytes.
  //
  // 1xxxxxxx : run of 0xFF, length=(token & 0x7F)+1.
  //
  // Maximum block/run length is 128 bytes.
  while (ip < encoded_size) {
    const uint8_t token = encoded[ip++];
    const size_t count = size_t(token & 0x7FU) + 1U;

    if (op + count > RAW_FRAME_SIZE) {
      return {false, DecodeError::OUTPUT_OVERFLOW, ip, op};
    }

    if ((token & 0x80U) != 0) {
      std::memset(raw + op, 0xFF, count);
      op += count;
      continue;
    }

    if (ip + count > encoded_size) {
      return {false, DecodeError::INPUT_TRUNCATED, ip, op};
    }

    std::memcpy(raw + op, encoded + ip, count);
    ip += count;
    op += count;
  }

  if (op != RAW_FRAME_SIZE) {
    return {false, DecodeError::OUTPUT_SIZE, ip, op};
  }

  return {true, DecodeError::NONE, ip, op};
}

inline uint32_t crc32(const uint8_t *data, size_t len) {
  uint32_t crc = 0xFFFFFFFFU;

  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];

    for (unsigned bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ ((crc & 1U) ? 0xEDB88320U : 0U);
    }
  }

  return ~crc;
}

}  // namespace frame
}  // namespace gtag_display
}  // namespace esphome
