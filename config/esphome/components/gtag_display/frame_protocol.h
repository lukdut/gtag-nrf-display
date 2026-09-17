#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "frame_codec.h"

namespace esphome {
namespace gtag_display {
namespace frame {

static constexpr uint8_t PROTOCOL_VERSION = 1;

enum class State : uint8_t {
  IDLE = 0,
  RECEIVING = 1,
  COMPLETE = 2,
  ERROR = 3,
};

enum class Error : uint8_t {
  NONE = 0,
  NOT_RECEIVING = 1,
  WRONG_OFFSET = 2,
  OUT_OF_BOUNDS = 3,
  INCOMPLETE = 4,
  CRC_MISMATCH = 5,
  CONFLICTING_DUPLICATE = 6,
  UNSUPPORTED_PROTOCOL = 7,
  UNSUPPORTED_CODEC = 8,
  DECODE_ERROR = 9,
  ENCODED_SIZE = 10,
};

enum class BeginResult : uint8_t {
  NEW_SESSION = 0,
  RESUMED = 1,
  REJECTED = 2,
};

struct Descriptor {
  uint8_t version{PROTOCOL_VERSION};
  Codec codec{Codec::RAW};
  uint16_t encoded_size{RAW_FRAME_SIZE};
  uint32_t frame_id{0};
  uint32_t raw_crc32{0};

  bool operator==(const Descriptor &other) const {
    return version == other.version &&
           codec == other.codec &&
           encoded_size == other.encoded_size &&
           frame_id == other.frame_id &&
           raw_crc32 == other.raw_crc32;
  }
};

class Receiver {
 public:
  BeginResult begin(const Descriptor &descriptor) {
    if (descriptor.version != PROTOCOL_VERSION) {
      reset_error_(Error::UNSUPPORTED_PROTOCOL);
      return BeginResult::REJECTED;
    }

    if (!codec_supported(descriptor.codec)) {
      reset_error_(Error::UNSUPPORTED_CODEC);
      return BeginResult::REJECTED;
    }

    if (descriptor.encoded_size == 0 ||
        descriptor.encoded_size > MAX_ENCODED_SIZE ||
        (descriptor.codec == Codec::RAW &&
         descriptor.encoded_size != RAW_FRAME_SIZE)) {
      reset_error_(Error::ENCODED_SIZE);
      return BeginResult::REJECTED;
    }

    if (has_descriptor_ &&
        descriptor == descriptor_ &&
        (state_ == State::RECEIVING || state_ == State::COMPLETE)) {
      error_ = Error::NONE;
      return BeginResult::RESUMED;
    }

    descriptor_ = descriptor;
    has_descriptor_ = true;
    received_ = 0;
    actual_raw_crc32_ = 0;
    state_ = State::RECEIVING;
    error_ = Error::NONE;
    return BeginResult::NEW_SESSION;
  }

  bool write(uint16_t offset, const uint8_t *data, size_t len) {
    if (state_ == State::ERROR)
      return false;

    if (state_ != State::RECEIVING && state_ != State::COMPLETE) {
      error_ = Error::NOT_RECEIVING;
      return false;
    }

    if (!has_descriptor_ || data == nullptr || len == 0 ||
        offset > descriptor_.encoded_size ||
        len > size_t(descriptor_.encoded_size - offset)) {
      error_ = Error::OUT_OF_BOUNDS;
      return false;
    }

    // Safe idempotent retransmit of an already accepted prefix.
    if (offset < received_) {
      if (size_t(offset) + len <= received_ &&
          std::memcmp(encoded_.data() + offset, data, len) == 0) {
        error_ = Error::NONE;
        return true;
      }

      error_ = Error::CONFLICTING_DUPLICATE;
      state_ = State::ERROR;
      return false;
    }

    if (offset != received_) {
      error_ = Error::WRONG_OFFSET;
      return false;
    }

    if (state_ != State::RECEIVING) {
      error_ = Error::NOT_RECEIVING;
      return false;
    }

    std::memcpy(encoded_.data() + offset, data, len);
    received_ += len;
    error_ = Error::NONE;
    return true;
  }

  bool commit(uint8_t *raw_out, size_t raw_size) {
    if (state_ == State::ERROR)
      return false;

    if (state_ == State::COMPLETE) {
      error_ = Error::NONE;
      return true;
    }

    if (!has_descriptor_ || state_ != State::RECEIVING ||
        received_ != descriptor_.encoded_size) {
      error_ = Error::INCOMPLETE;
      return false;
    }

    const DecodeResult decoded = decode_frame(
        descriptor_.codec,
        encoded_.data(),
        descriptor_.encoded_size,
        raw_out,
        raw_size);

    if (!decoded.ok) {
      error_ = Error::DECODE_ERROR;
      state_ = State::ERROR;
      return false;
    }

    actual_raw_crc32_ = crc32(raw_out, raw_size);

    if (actual_raw_crc32_ != descriptor_.raw_crc32) {
      error_ = Error::CRC_MISMATCH;
      state_ = State::ERROR;
      return false;
    }

    error_ = Error::NONE;
    state_ = State::COMPLETE;
    return true;
  }

  State state() const { return state_; }
  Error error() const { return error_; }
  uint16_t received() const { return received_; }
  uint16_t encoded_size() const {
    return has_descriptor_ ? descriptor_.encoded_size : 0;
  }
  Codec codec() const {
    return has_descriptor_ ? descriptor_.codec : Codec::RAW;
  }
  uint32_t actual_raw_crc32() const { return actual_raw_crc32_; }
  const Descriptor &descriptor() const { return descriptor_; }

  // Stable 8-byte status format, intentionally transport-neutral:
  //
  // [0]     state
  // [1..2]  number of ENCODED bytes accepted, little-endian
  // [3..6]  CRC32 of DECODED 4096-byte framebuffer after successful commit
  // [7]     error
  //
  // encoded_size/codec live in BEGIN and therefore need not be repeated here.
  void status(uint8_t out[8]) const {
    out[0] = static_cast<uint8_t>(state_);
    out[1] = received_ & 0xFF;
    out[2] = (received_ >> 8) & 0xFF;

    for (unsigned i = 0; i < 4; ++i) {
      out[3 + i] = (actual_raw_crc32_ >> (8U * i)) & 0xFF;
    }

    out[7] = static_cast<uint8_t>(error_);
  }

 private:
  void reset_error_(Error error) {
    received_ = 0;
    actual_raw_crc32_ = 0;
    state_ = State::ERROR;
    error_ = error;
    has_descriptor_ = false;
  }

  std::array<uint8_t, MAX_ENCODED_SIZE> encoded_{};

  Descriptor descriptor_{};
  uint16_t received_{0};
  uint32_t actual_raw_crc32_{0};

  State state_{State::IDLE};
  Error error_{Error::NONE};
  bool has_descriptor_{false};
};

}  // namespace frame
}  // namespace gtag_display
}  // namespace esphome
