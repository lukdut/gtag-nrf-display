#pragma once

#include "esphome/core/defines.h"
#ifdef USE_ESP32

#include <array>
#include <string>
#include "esphome/core/component.h"
#include "esphome/core/gpio.h"
#include "frame_protocol.h"

namespace esphome::gtag_display {

enum class BootPattern : uint8_t { NONE, WHITE, BLACK, CHECKERBOARD, STRIPES, LOGO, LOW_BATTERY, BATTERY_ERROR };

// USB-powered ESP32: all operations run on the ESPHome main loop. LCD writes
// are sliced so API, OTA, sensors and addressable-light effects keep running.
class GTagDisplay : public Component {
 public:
  void set_dio_pin(GPIOPin *pin) { pins_[0] = pin; }
  void set_clk_pin(GPIOPin *pin) { pins_[1] = pin; }
  void set_cs_pin(GPIOPin *pin) { pins_[2] = pin; }
  void set_reset_pin(GPIOPin *pin) { pins_[3] = pin; }
  void set_boot_pattern(BootPattern pattern) { boot_pattern_ = pattern; }
  void setup() override;
  void loop() override;
  void dump_config() override;

  bool submit(const std::string &payload, int version, int codec, const std::string &id,
              const std::string &crc, int timeout);
  bool confirm(const std::string &id, const std::string &crc, const std::string &sequence);
  bool rendered(const std::string &id, const std::string &crc) const;
  bool busy() const { return stage_ != Stage::READY || transmitting_; }
  const std::string &error() const { return error_; }
  std::string info_hex() const;

 protected:
  enum class Stage : uint8_t { RESET_WAIT, CLEAR, AFTER_CLEAR, AFTER_4C, AFTER_4D, AFTER_4E, READY };
  void word_(bool data, uint8_t value);
  void address_(uint16_t address);
  void wait_(Stage next, uint32_t delay);
  void start_frame_(bool verified);
  void transfer_slice_();
  bool reject_(const char *message) { error_ = message; return false; }

  std::array<GPIOPin *, 4> pins_{};
  std::array<uint8_t, frame::RAW_FRAME_SIZE> display_frame_{};
  std::array<uint8_t, frame::RAW_FRAME_SIZE> decoded_frame_{};
  std::array<uint8_t, frame::MAX_ENCODED_SIZE> encoded_frame_{};
  freshness::Lease freshness_;
  Stage stage_{Stage::RESET_WAIT};
  BootPattern boot_pattern_{BootPattern::LOGO};
  uint32_t deadline_{0};
  size_t offset_{0};
  bool transmitting_{false}, transfer_verified_{false}, verified_{false};
  bool shown_stale_{false}, transfer_stale_{false};
  uint32_t frame_id_{0}, crc_{0}, timeout_{0};
  std::string error_;
};

}  // namespace esphome::gtag_display
#endif  // USE_ESP32
