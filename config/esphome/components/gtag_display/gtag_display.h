#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <zephyr/kernel.h>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "frame_protocol.h"
#include "zigbee_protocol.h"
#ifdef USE_GTAG_BATTERY
#include "battery_bar.h"
#include "battery_guard.h"
#endif

namespace esphome {
namespace gtag_display {

// Keep diagnostic values stable: the Zigbee control maps 0..3 to WHITE..STRIPES.
enum class BootPattern : uint8_t { NONE, WHITE, BLACK, CHECKERBOARD, STRIPES, LOGO, LOW_BATTERY, BATTERY_ERROR };

class GTagDisplay : public Component {
 public:
  void set_advertising_interval(uint32_t ms) { advertising_interval_ms_ = ms; }
  void set_boot_pattern(BootPattern pattern) { boot_pattern_ = pattern; }
  void set_dio_pin(uint8_t pin) { lcd_pins_[0] = pin; }
  void set_clk_pin(uint8_t pin) { lcd_pins_[1] = pin; }
  void set_cs_pin(uint8_t pin) { lcd_pins_[2] = pin; }
  void set_reset_pin(uint8_t pin) { lcd_pins_[3] = pin; }
  void setup() override;
  void loop() override;
  void dump_config() override;

  bool radio_ready() const {
#ifdef USE_GTAG_BATTERY
    return !battery_protection_ || battery_guard_.state() == battery_guard::State::RUNNING;
#else
    return true;
#endif
  }

  // GATT callbacks
  void connection_changed(bool connected);
  void set_virtual_led(bool state);
  bool virtual_led() const { return virtual_led_.load(); }

  frame::BeginResult begin_frame(const frame::Descriptor &descriptor);
  bool write_frame_chunk(uint16_t offset, const uint8_t *data, size_t len);
  bool commit_frame();
  bool renew_freshness(uint32_t id, uint32_t crc, uint32_t sequence);
  void get_status(uint8_t out[8]) const { receiver_.status(out); }
  // Millivolts, or 0xFFFF when disabled, not yet sampled, or ADC failed.
  uint16_t battery_mv() const { return battery_mv_.load(); }
#ifdef USE_GTAG_ZIGBEE
  // Queue a diagnostic command; LCD GPIO is only touched by the main loop.
  // 0 white, 1 black, 2 checkerboard, 3 stripes.
  bool request_test_pattern(uint8_t pattern);
  uint32_t rendered_frames() const { return frames_; }
  // Called only from the ESPHome main loop, never directly by ZBOSS.
  void process_zigbee_packet(const uint8_t *data, size_t len, uint8_t reply[zigbee_frame::REPLY_SIZE]);
#endif
#ifdef USE_GTAG_BATTERY
  void set_battery_protection(uint16_t cutoff, uint16_t recovery) {
    battery_protection_ = true;
    battery_guard_.configure(cutoff, recovery);
  }
  void set_battery_calibration(float value) { battery_calibration_ = value; }
  void set_battery_pin(uint8_t pin) { battery_pin_ = pin; }
  void set_battery_indicator(bool enabled) { battery_indicator_ = enabled; }
  void set_battery_voltage_range(uint16_t empty_mv, uint16_t full_mv) {
    battery_bar_.set_voltage_range(empty_mv, full_mv);
  }
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
  void start_radio_();
  bool bluetooth_started_{false};
  void queue_pattern_(BootPattern pattern);

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
  void service_freshness_();

#ifdef USE_GTAG_BATTERY
  bool battery_protection_{true};
  battery_guard::Guard battery_guard_;
  void setup_battery_();
  void sample_battery_();
  float battery_calibration_{1.0f};
  uint8_t battery_pin_{31};
  bool battery_adc_ready_{false};
  bool battery_indicator_{true};
  std::atomic<bool> battery_overlay_allowed_{false};
  battery_bar::Gauge battery_bar_;
  int shown_battery_pixels_{-1};
#endif
  std::atomic<uint16_t> battery_mv_{0xFFFF};

  frame::Receiver receiver_;
  struct k_mutex freshness_mutex_;
  freshness::Lease freshness_;
  bool stale_overlay_{false};
  bool shown_stale_overlay_{false};
  // Decode separately so a rejected frame cannot corrupt a queued good frame.
  std::array<uint8_t, frame::RAW_FRAME_SIZE> decoded_frame_{};
  std::array<uint8_t, frame::RAW_FRAME_SIZE> display_frame_{};

  std::atomic<bool> frame_pending_{false};
  std::atomic<bool> connected_{false};
  std::atomic<bool> restart_advertising_{false};
  std::atomic<bool> virtual_led_{false};
#ifdef USE_GTAG_ZIGBEE
  std::atomic<uint8_t> pending_pattern_{0};  // 0: none, otherwise pattern + 1.
  uint32_t queued_frame_id_{0};
  uint32_t rendered_frame_id_{0};
  bool queued_verified_{false};
  bool rendered_verified_{false};
#endif

  Stage stage_{Stage::BOOT_WAIT};
  uint32_t next_ms_{0};
  uint32_t advertising_interval_ms_{1000};
  BootPattern boot_pattern_{BootPattern::LOGO};
  uint32_t words_{0};
  uint32_t frames_{0};
  uint32_t resets_{0};

  bool pins_configured_{false};
  std::array<uint8_t, 4> lcd_pins_{{11, 36, 38, 45}};
};

}  // namespace gtag_display
}  // namespace esphome
