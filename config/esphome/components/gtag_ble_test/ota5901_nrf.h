#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <zephyr/kernel.h>
#include "esphome/core/gpio.h"

namespace esphome {
namespace gtag_ble_test {

// Board already supplies the LCD's continuous ~29 kHz DisplayCLK.
// This class has deliberately NO XCLK pin/generator and NO TE dependency.
// loop() alone touches LCD GPIO. BLE callbacks only copy verified frames.
class OTA5901NRF {
 public:
  static constexpr size_t FRAME_SIZE = 4096;
  void set_dio_pin(GPIOPin *pin) { dio_ = pin; }
  void set_clk_pin(GPIOPin *pin) { clk_ = pin; }
  void set_cs_pin(GPIOPin *pin) { cs_ = pin; }
  void set_reset_pin(GPIOPin *pin) { reset_ = pin; }
  void set_half_period_us(uint8_t us) { half_us_ = us; }
  void set_boot_test(bool value) { boot_test_ = value; }
  bool configured() const { return dio_ && clk_ && cs_ && reset_; }
  void setup();
  void loop();
  void dump_config();

  // Copies 4096 bytes under a SHORT mutex; does not initialize or write LCD.
  // Active LCD transfer has its own immutable snapshot. If several frames arrive
  // before it finishes, the newest pending complete frame replaces the older one.
  bool queue_frame(const uint8_t *data, size_t len, uint32_t frame_id, uint32_t crc);

 protected:
  enum class Phase : uint8_t {
    IDLE, RESET_SETTLE, RESET_RELEASE, RESET_RECOVER, CLEAR_RAM, CONFIG_4C, CONFIG_4D,
    CONFIG_4E, READY_WAIT, WRITE_RAM
  };
  void write9_(bool data, uint8_t value);
  void command_(uint8_t value) { write9_(false, value); }
  void data_(uint8_t value) { write9_(true, value); }
  void command_data_(uint8_t command, const uint8_t *data, size_t len);
  void address_(uint16_t address);
  bool take_pending_();
  void start_frame_();
  void make_boot_pattern_();

  GPIOPin *dio_{nullptr}, *clk_{nullptr}, *cs_{nullptr}, *reset_{nullptr};
  uint8_t half_us_{1};
  bool enabled_{false}, boot_test_{true}, initialized_{false};
  bool loop_seen_{false};
  uint32_t reset_asserted_at_{0};
  // The pending mutex is never held during GPIO operations or delays.
  struct k_mutex pending_mutex_{};
  bool pending_ready_{false};
  std::array<uint8_t, FRAME_SIZE> pending_{}, active_{};
  uint32_t pending_id_{0}, pending_crc_{0}, active_id_{0}, active_crc_{0};
  Phase phase_{Phase::IDLE};
  uint32_t deadline_{0}, write_started_{0};
  size_t position_{0};
};

}  // namespace gtag_ble_test
}  // namespace esphome
