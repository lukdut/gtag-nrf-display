#include "esphome/core/defines.h"
#ifdef USE_ESP32
#include "gtag_display_esp32.h"
#include "boot_logo.h"
#include "firmware_info.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include <mbedtls/base64.h>
#include <algorithm>

namespace esphome::gtag_display {
namespace {
constexpr size_t SLICE = 64;
constexpr const char *TAG = "gtag_display";
bool hex32(const std::string &text, uint32_t &value) {
  if (text.size() != 8) return false;
  value = 0;
  for (const char c : text) {
    const int digit = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
    if (digit < 0) return false;
    value = (value << 4) | digit;
  }
  return true;
}
}  // namespace

void GTagDisplay::setup() {
  for (auto *pin : pins_) pin->setup();
  pins_[0]->digital_write(false);
  pins_[1]->digital_write(false);
  pins_[2]->digital_write(true);
  pins_[3]->digital_write(false);
  delay_microseconds_safe(50);
  pins_[3]->digital_write(true);
  display_frame_.fill(0xFF);
  if (boot_pattern_ == BootPattern::LOGO) display_frame_ = boot_logo::FRAME;
  else if (boot_pattern_ == BootPattern::BLACK) display_frame_.fill(0);
  else if (boot_pattern_ == BootPattern::CHECKERBOARD || boot_pattern_ == BootPattern::STRIPES) {
    for (size_t i = 0; i < display_frame_.size(); ++i)
      display_frame_[i] = ((i / 2 + (boot_pattern_ == BootPattern::CHECKERBOARD ? i / 512 : 0)) & 1) ? 0 : 0xFF;
  }
  wait_(Stage::RESET_WAIT, 50);
}

void GTagDisplay::word_(bool data, uint8_t byte) {
  pins_[1]->digital_write(false);
  pins_[2]->digital_write(false);
  delay_microseconds_safe(2);
  const uint16_t value = (data ? 0x100 : 0) | byte;
  for (int bit = 8; bit >= 0; --bit) {
    pins_[0]->digital_write((value >> bit) & 1);
    delay_microseconds_safe(2);
    pins_[1]->digital_write(true);
    delay_microseconds_safe(2);
    pins_[1]->digital_write(false);
  }
  delay_microseconds_safe(2);
  pins_[2]->digital_write(true);
  delay_microseconds_safe(2);
}

void GTagDisplay::address_(uint16_t address) {
  word_(false, 0x2A); word_(true, address >> 8); word_(true, address); word_(false, 0x2C);
}

void GTagDisplay::wait_(Stage next, uint32_t delay) { stage_ = next; deadline_ = millis() + delay; }

void GTagDisplay::start_frame_(bool verified) {
  transmitting_ = true;
  transfer_verified_ = verified;
  transfer_stale_ = !verified && freshness_.expired(millis());
  offset_ = 0;
  address_(0);
}

void GTagDisplay::transfer_slice_() {
  const size_t end = std::min(offset_ + SLICE, display_frame_.size());
  for (; offset_ < end; ++offset_)
    word_(true, display_frame_[offset_] ^ (transfer_stale_ ? freshness::mask(offset_) : 0));
  if (offset_ != display_frame_.size()) return;
  pins_[0]->digital_write(false);
  transmitting_ = false;
  shown_stale_ = transfer_stale_;
  if (transfer_verified_) {
    verified_ = true;
    freshness_.frame(frame_id_, crc_, timeout_, millis());
  }
}

void GTagDisplay::loop() {
  if (static_cast<int32_t>(millis() - deadline_) < 0) return;
  switch (stage_) {
    case Stage::RESET_WAIT:
      word_(false, 0x11); offset_ = 0; stage_ = Stage::CLEAR; return;
    case Stage::CLEAR: {
      if (offset_ % 256 == 0) address_(offset_);
      for (size_t i = 0; i < SLICE; ++i) word_(true, 0xFF);
      offset_ += SLICE;
      if (offset_ == display_frame_.size()) wait_(Stage::AFTER_CLEAR, 10);
      return;
    }
    case Stage::AFTER_CLEAR:
      word_(false, 0x4C); word_(true, 0x0C);
      for (int i = 0; i < 3; ++i) word_(true, 0);
      wait_(Stage::AFTER_4C, 4); return;
    case Stage::AFTER_4C:
      word_(false, 0x4D); word_(true, 0xFF); word_(true, 0); word_(true, 0x7F);
      wait_(Stage::AFTER_4D, 1); return;
    case Stage::AFTER_4D:
      word_(false, 0x4E); word_(true, 0x60); wait_(Stage::AFTER_4E, 500); return;
    case Stage::AFTER_4E:
      stage_ = Stage::READY;
      if (boot_pattern_ != BootPattern::NONE) start_frame_(false);
      return;
    case Stage::READY: break;
  }
  if (!transmitting_ && shown_stale_ != freshness_.expired(millis())) start_frame_(false);
  if (transmitting_) transfer_slice_();
  // Refresh the deadline in READY, keeping uptime wrap comparisons bounded.
  deadline_ = millis();
}

bool GTagDisplay::submit(const std::string &payload, int version, int codec, const std::string &id,
                         const std::string &crc, int timeout) {
  if (busy()) return reject_("display_busy");
  uint32_t frame_id, checksum;
  if (version != frame::PROTOCOL_VERSION || !hex32(id, frame_id) || !hex32(crc, checksum) ||
      timeout < 0 || timeout > static_cast<int>(freshness::MAX_TIMEOUT_S)) return reject_("invalid_descriptor");
  if (codec < 0 || codec > 255 || !frame::codec_supported(static_cast<frame::Codec>(codec)))
    return reject_("unsupported_codec");
  if (payload.empty() || payload.size() > ((frame::MAX_ENCODED_SIZE + 2) / 3) * 4)
    return reject_("invalid_frame_size");
  size_t size = 0;
  if (mbedtls_base64_decode(encoded_frame_.data(), encoded_frame_.size(), &size,
      reinterpret_cast<const unsigned char *>(payload.data()), payload.size()) != 0 || !size)
    return reject_("invalid_base64");
  const auto decoded = frame::decode_frame(static_cast<frame::Codec>(codec), encoded_frame_.data(), size,
                                           decoded_frame_.data(), decoded_frame_.size());
  if (!decoded.ok) return reject_("decode_error");
  if (frame::crc32(decoded_frame_.data(), decoded_frame_.size()) != checksum) return reject_("crc_mismatch");
  // Only a fully validated image can replace the last displayed frame.
  display_frame_ = decoded_frame_;
  frame_id_ = frame_id; crc_ = checksum; timeout_ = timeout;
  verified_ = false;
  error_.clear();
  start_frame_(true);
  return true;
}

bool GTagDisplay::rendered(const std::string &id, const std::string &crc) const {
  uint32_t frame_id, checksum;
  return verified_ && hex32(id, frame_id) && hex32(crc, checksum) && frame_id == frame_id_ && checksum == crc_;
}

bool GTagDisplay::confirm(const std::string &id, const std::string &crc, const std::string &sequence) {
  uint32_t frame_id, checksum, seq;
  return verified_ && hex32(id, frame_id) && hex32(crc, checksum) && hex32(sequence, seq) &&
      freshness_.renew(frame_id, checksum, seq, millis());
}

std::string GTagDisplay::info_hex() const {
  uint8_t bytes[firmware_info::SIZE];
  firmware_info::make(bytes);
  constexpr char HEX[] = "0123456789abcdef";
  std::string out;
  out.reserve(sizeof(bytes) * 2);
  for (auto byte : bytes) { out += HEX[byte >> 4]; out += HEX[byte & 15]; }
  return out;
}

void GTagDisplay::dump_config() {
  ESP_LOGCONFIG(TAG, "GTag 256x128, ESP32 Wi-Fi / native API, USB power");
  LOG_PIN("  DIO: ", pins_[0]); LOG_PIN("  CLK: ", pins_[1]);
  LOG_PIN("  CS: ", pins_[2]); LOG_PIN("  RESET: ", pins_[3]);
}
}  // namespace esphome::gtag_display
#endif
