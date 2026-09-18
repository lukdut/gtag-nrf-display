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
  void setup_battery_();
  void sample_battery_();
  float battery_calibration_{1.0f};
  bool battery_adc_ready_{false};
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
  BootPattern boot_pattern_{BootPattern::NONE};
  uint32_t words_{0};
  uint32_t frames_{0};
  uint32_t resets_{0};

  bool pins_configured_{false};
};

}  // namespace gtag_display
}  // namespace esphome
