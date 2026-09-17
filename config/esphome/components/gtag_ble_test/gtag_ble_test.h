#pragma once

#include <atomic>
#include <cstdint>
#include "esphome/core/component.h"
#include "esphome/core/gpio.h"
#include "frame_receiver.h"
#include "ota5901_nrf.h"

namespace esphome {
namespace gtag_ble_test {

class GTagBLETest : public Component {
 public:
  void set_led_pin(GPIOPin *pin) { led_pin_ = pin; }
  void set_lcd_dio_pin(GPIOPin *pin) { lcd_.set_dio_pin(pin); }
  void set_lcd_clk_pin(GPIOPin *pin) { lcd_.set_clk_pin(pin); }
  void set_lcd_cs_pin(GPIOPin *pin) { lcd_.set_cs_pin(pin); }
  void set_lcd_reset_pin(GPIOPin *pin) { lcd_.set_reset_pin(pin); }
  void set_lcd_half_period_us(uint8_t us) { lcd_.set_half_period_us(us); }
  void set_lcd_boot_test(bool value) { lcd_.set_boot_test(value); }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void queue_led_state(bool state);
  bool led_state() const { return led_state_.load(); }
  void connection_changed(bool connected);
  void begin_frame(bool tagged, uint32_t id, uint32_t crc);
  bool write_frame_chunk(uint16_t offset, const uint8_t *data, size_t len);
  bool commit_frame();
  void get_status(uint8_t out[8]) const { receiver_.status(out); }

 protected:
  bool start_advertising_();
  void apply_led_state_(bool state);
  GPIOPin *led_pin_{nullptr};
  FrameReceiver receiver_;
  OTA5901NRF lcd_;
  std::atomic<bool> led_state_{false}, pending_led_state_{false};
  std::atomic<bool> led_update_pending_{false};
  std::atomic<bool> connected_{false}, restart_advertising_{false};
  uint32_t next_advertising_attempt_{0};
};

}  // namespace gtag_ble_test
}  // namespace esphome
