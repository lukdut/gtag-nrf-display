#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "frame_protocol.h"

namespace esphome {
namespace gtag_display {

enum class BootPattern : uint8_t { NONE, WHITE, BLACK, CHECKERBOARD, STRIPES };

class GTagDisplay : public Component {
 public:
  void set_advertising_interval(uint32_t ms) { advertising_interval_ms_ = ms; }
  void set_boot_pattern(BootPattern pattern) { boot_pattern_ = pattern; }
  void setup() override;
  void loop() override;
  void dump_config() override;

  // GATT callbacks
  void connection_changed(bool connected);
  void set_virtual_led(bool state);
  bool virtual_led() const { return virtual_led_.load(); }

  frame::BeginResult begin_frame(const frame::Descriptor &descriptor);
  bool write_frame_chunk(uint16_t offset, const uint8_t *data, size_t len);
  bool commit_frame();
  void get_status(uint8_t out[8]) const { receiver_.status(out); }
  // Millivolts, or 0xFFFF when disabled, not yet sampled, or ADC failed.
  uint16_t battery_mv() const { return battery_mv_.load(); }
#ifdef USE_GTAG_BATTERY
  void set_battery_calibration(float value) { battery_calibration_ = value; }
#endif

 protected:
  enum class Stage : uint8_t {
    BOOT_WAIT,
    AFTER_RESET,
    AFTER_CLEAR,
    AFTER_4C,
    AFTER_4D,
    AFTER_4E,
    READY,
  };

  enum class Signal : uint8_t { DIO, SCLK, CS, RESET };

  // BLE
  bool start_advertising_();
  void queue_boot_pattern_();

  // LCD: copied from the proven LCD3-DIRECT-01/v36 behavior.
  bool configure_pins_();
  bool write_(Signal signal, bool high);
  bool reset_pulse_();
  bool word_(bool data, uint8_t byte);
  bool address_(uint16_t address);
  bool clear_ram_();
  bool send_frame_(const uint8_t *frame);
  void wait_(Stage next, uint32_t delay_ms);
  void service_lcd_();

#ifdef USE_GTAG_BATTERY
  void setup_battery_();
  void sample_battery_();
  float battery_calibration_{1.0f};
  bool battery_adc_ready_{false};
#endif
  std::atomic<uint16_t> battery_mv_{0xFFFF};

  frame::Receiver receiver_;
  // Decode separately so a rejected frame cannot corrupt a queued good frame.
  std::array<uint8_t, frame::RAW_FRAME_SIZE> decoded_frame_{};
  std::array<uint8_t, frame::RAW_FRAME_SIZE> display_frame_{};

  std::atomic<bool> frame_pending_{false};
  std::atomic<bool> connected_{false};
  std::atomic<bool> restart_advertising_{false};
  std::atomic<bool> virtual_led_{false};

  Stage stage_{Stage::BOOT_WAIT};
  uint32_t next_ms_{0};
  uint32_t advertising_interval_ms_{1000};
  BootPattern boot_pattern_{BootPattern::NONE};
  uint32_t words_{0};
  uint32_t frames_{0};
  uint32_t resets_{0};

  bool pins_configured_{false};
};

}  // namespace gtag_display
}  // namespace esphome
