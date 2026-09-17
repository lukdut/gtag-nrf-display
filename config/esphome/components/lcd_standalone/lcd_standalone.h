#pragma once

#include <cstddef>
#include <cstdint>
#include "esphome/core/component.h"

namespace esphome {
namespace lcd_standalone {

class LCDStandalone : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

 protected:
  enum class Stage : uint8_t {
    BOOT_WAIT, AFTER_RESET, AFTER_CLEAR, AFTER_4C, AFTER_4D,
    AFTER_4E, SHOW_NEXT, FAULT
  };
  enum class Signal : uint8_t { DIO, SCLK, CS, RESET };

  bool configure_pins_();
  bool write_(Signal signal, bool high);
  bool check_(Signal signal, bool high, const char *operation);
  bool check_spi_pins_();
  bool reset_pulse_();
  bool word_(bool data, uint8_t byte);
  bool address_(uint16_t address);
  bool clear_ram_();
  bool send_pattern_();
  void fail_(Signal signal, const char *operation, int expected, int actual, int rc);
  void wait_(Stage next, uint32_t delay_ms);

  Stage stage_{Stage::BOOT_WAIT};
  uint32_t next_ms_{0};
  uint32_t heartbeat_ms_{0};
  uint32_t words_{0};
  uint32_t resets_{0};
  uint32_t frames_{0};
  uint8_t pattern_{0};
  bool pins_configured_{false};
  char last_error_[180]{};
};

}  // namespace lcd_standalone
}  // namespace esphome
