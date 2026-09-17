#include "lcd_standalone.h"

#include <cstdio>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/dt-bindings/gpio/nordic-nrf-gpio.h>
#include <zephyr/kernel.h>
#include <hal/nrf_gpio.h>
#include "esphome/core/application.h"
#include "esphome/core/log.h"

// Drop-in replacement for the existing v3 lcd_standalone.h and __init__.py.
// Diagnostic standalone LCD driver.
// Pins are configured through Zephyr once, but timing-critical writes use
// direct nRF OUTSET/OUTCLR registers to avoid Zephyr GPIO call overhead.
namespace esphome {
namespace lcd_standalone {
namespace {

static const char *const TAG = "lcd3";
constexpr uint32_t HALF_US = 2;  // ~250 kHz nominal SCLK before GPIO overhead; chosen to match working ESP32 capture.
constexpr size_t FRAME_BYTES = 4096;

struct Pin {
  const struct device *port;
  gpio_pin_t bit;
  const char *name;
};

// Exactly the existing wiring. No external-clock GPIO is driven here.
const Pin PINS[] = {
    {DEVICE_DT_GET(DT_NODELABEL(gpio0)), 11, "DIO P0.11"},
    {DEVICE_DT_GET(DT_NODELABEL(gpio1)),  4, "CLK P1.04"},
    {DEVICE_DT_GET(DT_NODELABEL(gpio1)),  6, "CS P1.06"},
    {DEVICE_DT_GET(DT_NODELABEL(gpio1)), 13, "RESET P1.13"},
};
const char *const PATTERN_NAMES[] = {"WHITE", "BLACK", "CHECKERBOARD", "STRIPES"};

uint8_t pattern_byte(uint8_t pattern, size_t i) {
  switch (pattern) {
    case 0: return 0xFF;
    case 1: return 0x00;
    case 2: return ((((i / 32) / 8) + (i % 32)) & 1U) ? 0x00 : 0xFF;
    case 3: return 0x0F;
    default: return 0xFF;
  }
}

}  // namespace

void LCDStandalone::wait_(Stage next, uint32_t delay_ms) {
  if (this->stage_ == Stage::FAULT) return;
  this->stage_ = next;
  this->next_ms_ = k_uptime_get_32() + delay_ms;
}

void LCDStandalone::fail_(Signal signal, const char *operation,
                          int expected, int actual, int rc) {
  if (this->stage_ == Stage::FAULT) return;
  const auto &pin = PINS[static_cast<unsigned>(signal)];
  this->stage_ = Stage::FAULT;
  this->heartbeat_ms_ = k_uptime_get_32();
  // Only called for an API/device error, NEVER for GPIO input readback.
  if (this->pins_configured_) {
    const auto &cs = PINS[static_cast<unsigned>(Signal::CS)];
    (void) gpio_pin_set_raw(cs.port, cs.bit, 1);
    (void) gpio_pin_configure(pin.port, pin.bit, GPIO_INPUT);
  }
  (void) actual;  // No input measurement is made in this diagnostic version.
  std::snprintf(this->last_error_, sizeof(this->last_error_),
                "%s %s requested=%d rc=%d words=%u", operation, pin.name,
                expected, rc, static_cast<unsigned>(this->words_));
  ESP_LOGE(TAG, "LCD3-DIRECT-01: API STOP: %s", this->last_error_);
}

bool LCDStandalone::configure_pins_() {
  for (unsigned i = 0; i < 4; ++i) {
    if (!device_is_ready(PINS[i].port)) {
      this->fail_(static_cast<Signal>(i), "DEVICE_NOT_READY", -1, -1, -1);
      return false;
    }
  }
  const Signal order[] = {Signal::CS, Signal::RESET, Signal::SCLK, Signal::DIO};
  for (const Signal signal : order) {
    const auto &pin = PINS[static_cast<unsigned>(signal)];
    const bool high = signal == Signal::CS || signal == Signal::RESET;
    // Keep the electrical configuration of v3.3 unchanged, including input
    // buffer connection. This version simply never reads that input buffer.
    gpio_flags_t flags = GPIO_INPUT | (high ? GPIO_OUTPUT_HIGH : GPIO_OUTPUT_LOW);
    if (signal == Signal::SCLK || signal == Signal::DIO) {
      flags |= NRF_GPIO_DRIVE_S0H1;  // Same drive as the user's latest test.
    }
    const int rc = gpio_pin_configure(pin.port, pin.bit, flags);
    if (rc != 0) {
      this->fail_(signal, "CONFIGURE", high ? 1 : 0, -1, rc);
      return false;
    }
    ESP_LOGI(TAG, "LCD3-DIRECT-01: CONFIG %s rc=0; no readback", pin.name);
  }
  this->pins_configured_ = true;
  return true;
}

bool LCDStandalone::write_(Signal signal, bool high) {
  if (this->stage_ == Stage::FAULT) return false;

  // Direct, atomic port writes. gpio_pin_set_raw() is deliberately not used
  // here because its Zephyr/nrfx driver path adds substantial per-edge
  // overhead. Configuration (direction/drive) is still done once through
  // gpio_pin_configure() in configure_pins_().
  switch (signal) {
    case Signal::DIO:
      if (high) {
        NRF_P0->OUTSET = BIT(11);
      } else {
        NRF_P0->OUTCLR = BIT(11);
      }
      break;

    case Signal::SCLK:
      if (high) {
        NRF_P1->OUTSET = BIT(4);
      } else {
        NRF_P1->OUTCLR = BIT(4);
      }
      break;

    case Signal::CS:
      if (high) {
        NRF_P1->OUTSET = BIT(6);
      } else {
        NRF_P1->OUTCLR = BIT(6);
      }
      break;

    case Signal::RESET:
      if (high) {
        NRF_P1->OUTSET = BIT(13);
      } else {
        NRF_P1->OUTCLR = BIT(13);
      }
      break;
  }

  return true;
}

// check_() and check_spi_pins_() remain DECLARED in the old header for
// compatibility, but are not defined or used in this implementation.

bool LCDStandalone::reset_pulse_() {
  ++this->resets_;
  if (!this->write_(Signal::CS, true) ||
      !this->write_(Signal::SCLK, false) ||
      !this->write_(Signal::DIO, false)) return false;
  if (!this->write_(Signal::RESET, false)) return false;
  k_busy_wait(50);
  if (!this->write_(Signal::RESET, true)) return false;
  ESP_LOGI(TAG, "LCD3-DIRECT-01: RESET #%u commanded LOW 50us -> HIGH; no readback",
           static_cast<unsigned>(this->resets_));
  return true;
}

bool LCDStandalone::word_(bool is_data, uint8_t byte) {
  // CS surrounds exactly nine rising SCLK edges: D/C, then eight MSB-first bits.
  if (!this->write_(Signal::SCLK, false) || !this->write_(Signal::CS, false)) return false;
  k_busy_wait(HALF_US);
  const uint16_t value = uint16_t(is_data ? 0x100U : 0U) | byte;
  for (int bit = 8; bit >= 0; --bit) {
    if (!this->write_(Signal::DIO, ((value >> bit) & 1U) != 0)) return false;
    k_busy_wait(HALF_US);  // Data setup and clock LOW time.
    if (!this->write_(Signal::SCLK, true)) return false;
    k_busy_wait(HALF_US);  // Clock HIGH time; no level sampling.
    if (!this->write_(Signal::SCLK, false)) return false;
  }
  k_busy_wait(HALF_US);
  if (!this->write_(Signal::CS, true)) return false;
  k_busy_wait(HALF_US);
  ++this->words_;
  return true;
}

bool LCDStandalone::address_(uint16_t address) {
  return this->word_(false, 0x2A) &&
         this->word_(true, uint8_t(address >> 8)) &&
         this->word_(true, uint8_t(address));
}

bool LCDStandalone::clear_ram_() {
  if (!this->word_(false, 0x11)) return false;
  for (uint16_t base = 0; base < FRAME_BYTES; base += 256) {
    if (!this->address_(base) || !this->word_(false, 0x2C)) return false;
    for (unsigned j = 0; j < 256; ++j) {
      if (!this->word_(true, 0xFF)) return false;
    }
    App.feed_wdt();
  }
  return this->write_(Signal::DIO, false);
}

bool LCDStandalone::send_pattern_() {
  ESP_LOGI(TAG, "LCD3-DIRECT-01: FRAME %s begin", PATTERN_NAMES[this->pattern_]);
  const uint32_t started = k_uptime_get_32();
  const uint32_t old_words = this->words_;
  if (!this->address_(0) || !this->word_(false, 0x2C)) return false;
  // Deliberately one burst, not separate loop() slices. Watchdog is fed, but
  // interrupts are NOT disabled. This is a slow, standalone diagnostic build.
  for (size_t i = 0; i < FRAME_BYTES; ++i) {
    if (!this->word_(true, pattern_byte(this->pattern_, i))) return false;
    if ((i & 0xFFU) == 0xFFU) App.feed_wdt();
  }
  if (!this->write_(Signal::DIO, false)) return false;
  ++this->frames_;
  ESP_LOGI(TAG, "LCD3-DIRECT-01: FRAME %s TX_DONE bytes=4096 words=%u ms=%u; no readback, no LCD acknowledgement",
           PATTERN_NAMES[this->pattern_], static_cast<unsigned>(this->words_ - old_words),
           static_cast<unsigned>(k_uptime_get_32() - started));
  this->pattern_ = (this->pattern_ + 1U) % 4U;
  return true;
}

void LCDStandalone::setup() {
  ESP_LOGI(TAG, "LCD3-DIRECT-01: direct OUTSET/OUTCLR; HALF_US=2; target SCLK~250kHz; GPIO level checks OFF");
  ESP_LOGI(TAG, "LCD3-DIRECT-01: CLK/DIO=S0H1, CS/RESET=S0S1 unchanged; no BLE, no XCLK output");
  if (!this->configure_pins_()) return;
  this->wait_(Stage::BOOT_WAIT, 3000);
}

void LCDStandalone::loop() {
  const uint32_t now = k_uptime_get_32();
  if (this->stage_ == Stage::FAULT) {
    if (uint32_t(now - this->heartbeat_ms_) >= 5000U) {
      this->heartbeat_ms_ = now;
      ESP_LOGE(TAG, "LCD3-DIRECT-01: API STOP persists: %s", this->last_error_);
    }
    return;
  }
  if (int32_t(now - this->next_ms_) < 0) return;
  switch (this->stage_) {
    case Stage::BOOT_WAIT:
      if (!this->reset_pulse_()) return;
      ESP_LOGI(TAG, "LCD3-DIRECT-01: wait 2000ms AFTER RESET release; stock XCLK not measured");
      this->wait_(Stage::AFTER_RESET, 2000);
      return;
    case Stage::AFTER_RESET:
      ESP_LOGI(TAG, "LCD3-DIRECT-01: INIT 0x11 + RAM clear (no GPIO self-test)");
      if (!this->clear_ram_()) return;
      this->wait_(Stage::AFTER_CLEAR, 10);
      return;
    case Stage::AFTER_CLEAR:
      if (!(this->word_(false, 0x4C) && this->word_(true, 0x0C) &&
            this->word_(true, 0) && this->word_(true, 0) && this->word_(true, 0))) return;
      this->wait_(Stage::AFTER_4C, 4);
      return;
    case Stage::AFTER_4C:
      if (!(this->word_(false, 0x4D) && this->word_(true, 0xFF) &&
            this->word_(true, 0) && this->word_(true, 0x7F))) return;
      this->wait_(Stage::AFTER_4D, 1);
      return;
    case Stage::AFTER_4D:
      if (!(this->word_(false, 0x4E) && this->word_(true, 0x60))) return;
      ESP_LOGI(TAG, "LCD3-DIRECT-01: INIT_SENT; wait 500ms; no LCD acknowledgement");
      this->wait_(Stage::AFTER_4E, 500);
      return;
    case Stage::AFTER_4E:
    case Stage::SHOW_NEXT:
      if (!this->send_pattern_()) return;
      this->wait_(Stage::SHOW_NEXT, 5000);
      return;
    case Stage::FAULT:
      return;
  }
}

void LCDStandalone::dump_config() {
  ESP_LOGCONFIG(TAG, "LCD3-DIRECT-01: DIO=P0.11 CLK=P1.04 CS=P1.06 RESET=P1.13");
  ESP_LOGCONFIG(TAG, "  direct nRF OUTSET/OUTCLR; SPI pauses=%u us per half-cycle; input checks disabled", unsigned(HALF_US));
}

}  // namespace lcd_standalone
}  // namespace esphome
