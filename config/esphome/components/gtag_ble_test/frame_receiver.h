#pragma once

// Hardware-independent receiver. Only serialized GATT callbacks use this object.
// No GATT database changes: the existing 8-byte status is preserved.
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace esphome {
namespace gtag_ble_test {

class FrameReceiver {
 public:
  static constexpr size_t SIZE = 4096;
  enum State : uint8_t { IDLE = 0, RECEIVING = 1, COMPLETE = 2, ERROR = 3 };
  enum Error : uint8_t {
    NONE = 0, NOT_RECEIVING = 1, WRONG_OFFSET = 2, OUT_OF_BOUNDS = 3,
    INCOMPLETE = 4, CRC_MISMATCH = 5, CONFLICTING_DUPLICATE = 6
  };

  // BEGIN(frame_id, crc) is idempotent: retransmissions/reconnects keep progress.
  // A new ID starts a new frame; a reboot naturally loses the in-RAM session.
  bool begin(uint32_t id, uint32_t crc) {
    if (has_session_ && id == session_id_ && crc == expected_crc_ &&
        (state_ == RECEIVING || state_ == COMPLETE)) {
      return false;  // resumed, not reset
    }
    reset_();
    has_session_ = true;
    session_id_ = id;
    expected_crc_ = crc;
    check_crc_ = true;
    return true;
  }

  // Compatibility with the old single-byte BEGIN command. Always starts over.
  void begin_legacy() {
    reset_();
    has_session_ = false;
    check_crc_ = false;
  }

  bool write(uint16_t offset, const uint8_t *data, size_t len) {
    if (state_ == ERROR) return false;  // Preserve the original failure reason.
    if (state_ != RECEIVING && state_ != COMPLETE) {
      error_ = NOT_RECEIVING;
      return false;
    }
    if (data == nullptr || len == 0 || offset > SIZE || len > SIZE - offset) {
      error_ = OUT_OF_BOUNDS;
      return false;
    }
    // A write can have succeeded even if its response was lost upstream.
    // Repeating identical, fully received bytes must not fail the transfer.
    if (offset < received_) {
      if (static_cast<size_t>(offset) + len <= received_ &&
          std::memcmp(frame_.data() + offset, data, len) == 0) {
        error_ = NONE;
        return true;
      }
      error_ = CONFLICTING_DUPLICATE;
      state_ = ERROR;  // Never silently accept different bytes at the same offset.
      return false;
    }
    if (offset != received_) {
      error_ = WRONG_OFFSET;
      return false;  // Retain the valid prefix so the client can resume.
    }
    if (state_ != RECEIVING) {
      error_ = NOT_RECEIVING;
      return false;
    }
    std::memcpy(frame_.data() + offset, data, len);
    received_ += len;
    error_ = NONE;
    return true;
  }

  bool commit() {
    if (state_ == ERROR) return false;  // Do not replace CRC/conflict with INCOMPLETE.
    if (state_ == COMPLETE && received_ == SIZE) {
      error_ = NONE;
      return true;  // Idempotent COMMIT, including after reconnection.
    }
    if (state_ != RECEIVING || received_ != SIZE) {
      error_ = INCOMPLETE;
      return false;  // An incomplete prefix can still be continued.
    }
    crc_ = crc32(frame_.data(), SIZE);
    if (check_crc_ && crc_ != expected_crc_) {
      error_ = CRC_MISMATCH;
      state_ = ERROR;
      return false;
    }
    error_ = NONE;
    state_ = COMPLETE;
    return true;
  }

  // Read only in the serialized GATT callback after successful COMMIT.
  const uint8_t *data() const { return frame_.data(); }

  uint8_t state() const { return state_; }
  uint8_t error() const { return error_; }
  uint16_t received() const { return static_cast<uint16_t>(received_); }
  uint32_t crc() const { return crc_; }
  uint32_t session_id() const { return session_id_; }

  void status(uint8_t out[8]) const {
    out[0] = state_;
    out[1] = received_ & 0xff;
    out[2] = (received_ >> 8) & 0xff;
    for (unsigned i = 0; i < 4; ++i) out[3 + i] = (crc_ >> (8 * i)) & 0xff;
    out[7] = error_;
  }

  static uint32_t crc32(const uint8_t *data, size_t len) {
    uint32_t crc = 0xffffffffu;
    for (size_t i = 0; i < len; ++i) {
      crc ^= data[i];
      for (unsigned bit = 0; bit < 8; ++bit) {
        crc = (crc >> 1) ^ ((crc & 1u) ? 0xedb88320u : 0u);
      }
    }
    return ~crc;
  }

 private:
  void reset_() {
    received_ = 0;
    crc_ = 0;
    state_ = RECEIVING;
    error_ = NONE;
  }
  std::array<uint8_t, SIZE> frame_{};
  size_t received_{0};
  uint32_t crc_{0}, expected_crc_{0}, session_id_{0};
  uint8_t state_{IDLE}, error_{NONE};
  bool has_session_{false}, check_crc_{false};
};

}  // namespace gtag_ble_test
}  // namespace esphome
