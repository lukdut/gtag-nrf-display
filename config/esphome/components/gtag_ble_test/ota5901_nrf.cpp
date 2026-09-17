#include "ota5901_nrf.h"

#include <algorithm>
#include <cstring>
#include "frame_receiver.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace gtag_ble_test {
static const char *const TAG_LCD = "gtag_lcd";
// Keep initialization clearing unchanged from v0.5.2.
// Only the final display-frame write is made continuous in this diagnostic.
static constexpr size_t BYTES_PER_LOOP = 256;
// Diagnostic timing only: do NOT busy-wait while RESET is low. The normal
// ESPHome loop remains available to the BLE component throughout these waits.
static constexpr uint32_t DIAG_PRE_RESET_MS = 3000;
static constexpr uint32_t DIAG_RESET_LOW_MS = 200;
static constexpr uint32_t RESET_RECOVERY_MS = 50;
static const char *const DIAG_BUILD = "LCD-DIAG-053-BURST";

void OTA5901NRF::setup() {
  ESP_LOGI(TAG_LCD, "%s: setup entered", DIAG_BUILD);
  if (!configured()) {
    ESP_LOGW(TAG_LCD, "%s: LCD disabled: missing pin configuration (DIO=%s CLK=%s CS=%s RESET=%s)",
             DIAG_BUILD, dio_ ? "set" : "missing", clk_ ? "set" : "missing",
             cs_ ? "set" : "missing", reset_ ? "set" : "missing");
    return;
  }
  // Log the actual pin objects injected by generated main.cpp, not hard-coded
  // expected GPIO numbers. This distinguishes wrong mapping from state logic.
  char pin_name[64] = {};
  reset_->dump_summary(pin_name, sizeof(pin_name));
  ESP_LOGI(TAG_LCD, "%s: RESET mapping=%s flags=0x%02X", DIAG_BUILD,
           pin_name, unsigned(reset_->get_flags()));
  ESP_LOGI(TAG_LCD, "%s: boot_test=%s", DIAG_BUILD, boot_test_ ? "yes" : "no");
  k_mutex_init(&pending_mutex_);
  // Prepare inactive values before/after enabling outputs. No reset at setup;
  // the reset pulse is issued by the first queued frame's initialization.
  cs_->digital_write(true);
  reset_->digital_write(true);
  clk_->digital_write(false);
  dio_->digital_write(false);
  cs_->setup(); reset_->setup(); clk_->setup(); dio_->setup();
  cs_->digital_write(true); reset_->digital_write(true);
  clk_->digital_write(false); dio_->digital_write(false);
  enabled_ = true;
  ESP_LOGI(TAG_LCD, "%s: pins setup; RESET commanded HIGH (voltage not read back)", DIAG_BUILD);
  ESP_LOGI(TAG_LCD, "LCD output enabled; EXTERNAL stock DisplayCLK required; no XCLK output");
  if (boot_test_) make_boot_pattern_();
}

void OTA5901NRF::make_boot_pattern_() {
  // Called before BLE is enabled. Boot test does NOT report a received BLE frame.
  pending_.fill(0xFF);
  for (int y = 0; y < 128; ++y) {
    for (int x = 0; x < 256; ++x) {
      if (((x / 8) + (y / 8)) & 1)
        pending_[size_t(y) * 32 + size_t(x / 8)] &= uint8_t(~(1u << (x & 7)));
    }
  }
  pending_id_ = 0;
  pending_crc_ = FrameReceiver::crc32(pending_.data(), FRAME_SIZE);
  pending_ready_ = true;
  ESP_LOGI(TAG_LCD, "Local boot checkerboard queued (not a BLE frame)");
}

bool OTA5901NRF::queue_frame(const uint8_t *data, size_t len,
                            uint32_t frame_id, uint32_t crc) {
  if (!enabled_ || data == nullptr || len != FRAME_SIZE) return false;
  k_mutex_lock(&pending_mutex_, K_FOREVER);
  std::memcpy(pending_.data(), data, FRAME_SIZE);
  pending_id_ = frame_id;
  pending_crc_ = crc;
  pending_ready_ = true;
  k_mutex_unlock(&pending_mutex_);
  return true;
}

bool OTA5901NRF::take_pending_() {
  k_mutex_lock(&pending_mutex_, K_FOREVER);
  const bool ready = pending_ready_;
  if (ready) {
    std::memcpy(active_.data(), pending_.data(), FRAME_SIZE);
    active_id_ = pending_id_;
    active_crc_ = pending_crc_;
    pending_ready_ = false;
  }
  k_mutex_unlock(&pending_mutex_);
  return ready;
}

void OTA5901NRF::write9_(bool is_data, uint8_t value) {
  // 3-wire, 9 bits per CS transaction: D/C then 8 data bits, MSB first.
  // No irq_lock(): the Bluetooth controller must retain interrupt service.
  clk_->digital_write(false);
  cs_->digital_write(false);
  k_busy_wait(half_us_);
  const uint16_t word = (is_data ? 0x100u : 0u) | value;
  for (int bit = 8; bit >= 0; --bit) {
    dio_->digital_write((word & (1u << bit)) != 0);
    k_busy_wait(half_us_);
    clk_->digital_write(true);
    k_busy_wait(half_us_);
    clk_->digital_write(false);
  }
  k_busy_wait(half_us_);
  cs_->digital_write(true);
  k_busy_wait(half_us_);
}

void OTA5901NRF::command_data_(uint8_t command, const uint8_t *data, size_t len) {
  command_(command);
  for (size_t i = 0; i < len; ++i) data_(data[i]);
}
void OTA5901NRF::address_(uint16_t address) {
  const uint8_t data[] = {uint8_t(address >> 8), uint8_t(address)};
  command_data_(0x2A, data, sizeof(data));
}
void OTA5901NRF::start_frame_() {
  // This method runs ONLY in the normal ESPHome loop, never the BLE RX callback.
  // In v0.5.2 the address/0x2C and the data were spread across different loop()
  // calls. This diagnostic sends address, command, and all 4096 data bytes in
  // one call, with no intentional scheduler pauses between 256-byte pieces.
  // CS still goes high between 9-bit words, exactly as in write9_().
  // Interrupts remain enabled; this is NOT a hard real-time or atomic transfer.
  // BLE callbacks may queue the NEXT frame in pending_, never mutate active_.
  ESP_LOGI(TAG_LCD, "%s: LCD burst begin id=%08X crc=%08X; 4096 bytes in one loop call",
           DIAG_BUILD, unsigned(active_id_), unsigned(active_crc_));
  write_started_ = millis();
  address_(0);
  command_(0x2C);
  for (position_ = 0; position_ < FRAME_SIZE; ++position_)
    data_(active_[position_]);
  cs_->digital_write(true);
  clk_->digital_write(false);
  dio_->digital_write(false);
  phase_ = Phase::IDLE;
  deadline_ = 0;
  ESP_LOGI(TAG_LCD, "%s: LCD burst done bytes=4096 id=%08X crc=%08X duration=%u ms; GPIO transmission only",
           DIAG_BUILD, unsigned(active_id_), unsigned(active_crc_),
           unsigned(millis() - write_started_));
}

void OTA5901NRF::loop() {
  if (!enabled_) return;
  if (!loop_seen_) {
    loop_seen_ = true;
    ESP_LOGI(TAG_LCD, "%s: LCD loop entered", DIAG_BUILD);
  }
  const uint32_t now = millis();
  if (phase_ != Phase::IDLE && int32_t(now - deadline_) < 0) return;
  switch (phase_) {
    case Phase::IDLE:
      if (!take_pending_()) return;
      if (initialized_) { start_frame_(); return; }
      cs_->digital_write(true); clk_->digital_write(false);
      dio_->digital_write(false); reset_->digital_write(true);
      deadline_ = millis() + DIAG_PRE_RESET_MS;
      phase_ = Phase::RESET_SETTLE;
      ESP_LOGI(TAG_LCD, "%s: INIT requested; wait %u ms before RESET LOW", DIAG_BUILD,
               unsigned(DIAG_PRE_RESET_MS));
      ESP_LOGI(TAG_LCD, "OTA5901 init begin; stock 29 kHz clock is not generated/verified here");
      return;
    case Phase::RESET_SETTLE:
      ESP_LOGI(TAG_LCD, "%s: entering RESET LOW stage", DIAG_BUILD);
      reset_->digital_write(false);
      reset_asserted_at_ = millis();
      deadline_ = reset_asserted_at_ + DIAG_RESET_LOW_MS;
      phase_ = Phase::RESET_RELEASE;
      ESP_LOGI(TAG_LCD, "%s: RESET commanded LOW at %u ms; hold >=%u ms", DIAG_BUILD,
               unsigned(reset_asserted_at_), unsigned(DIAG_RESET_LOW_MS));
      return;
    case Phase::RESET_RELEASE:
      reset_->digital_write(true);
      deadline_ = millis() + RESET_RECOVERY_MS;
      phase_ = Phase::RESET_RECOVER;
      ESP_LOGI(TAG_LCD, "%s: RESET commanded HIGH after %u ms; recovery %u ms", DIAG_BUILD,
               unsigned(millis() - reset_asserted_at_), unsigned(RESET_RECOVERY_MS));
      return;
    case Phase::RESET_RECOVER:
      ESP_LOGI(TAG_LCD, "%s: reset stages complete; sending LCD command 0x11", DIAG_BUILD);
      command_(0x11);
      position_ = 0;
      phase_ = Phase::CLEAR_RAM;
      return;
    case Phase::CLEAR_RAM: {
      // Match the known working initialization: white RAM in 256-byte blocks.
      address_(static_cast<uint16_t>(position_));
      command_(0x2C);
      const size_t count = std::min(BYTES_PER_LOOP, FRAME_SIZE - position_);
      for (size_t i = 0; i < count; ++i) data_(0xFF);
      position_ += count;
      if (position_ == FRAME_SIZE) {
        phase_ = Phase::CONFIG_4C;
        deadline_ = millis() + 10;
      }
      return;
    }
    case Phase::CONFIG_4C: {
      // Four bytes, not three (as in the working Caldin-Maldin driver).
      const uint8_t p[] = {0x0C, 0x00, 0x00, 0x00};
      command_data_(0x4C, p, sizeof(p));
      phase_ = Phase::CONFIG_4D; deadline_ = millis() + 4; return;
    }
    case Phase::CONFIG_4D: {
      const uint8_t p[] = {0xFF, 0x00, 0x7F};
      command_data_(0x4D, p, sizeof(p));
      phase_ = Phase::CONFIG_4E; deadline_ = millis() + 1; return;
    }
    case Phase::CONFIG_4E: {
      const uint8_t p[] = {0x60};
      command_data_(0x4E, p, sizeof(p));
      phase_ = Phase::READY_WAIT; deadline_ = millis() + 501; return;
    }
    case Phase::READY_WAIT:
      initialized_ = true;
      ESP_LOGI(TAG_LCD, "OTA5901 init sequence sent (no LCD acknowledgement)");
      start_frame_();
      return;
    case Phase::WRITE_RAM:
      // Retained for compatibility with the unmodified v0.5.2 header enum.
      // The normal path now completes the full transfer inside start_frame_().
      start_frame_();
      return;
  }
}
void OTA5901NRF::dump_config() {
  if (!configured()) {
    ESP_LOGCONFIG(TAG_LCD, "LCD not configured: BLE-only mode");
    return;
  }
  ESP_LOGCONFIG(TAG_LCD, "%s: diagnostic 200 ms RESET; 3000 ms pre-reset wait", DIAG_BUILD);
  ESP_LOGCONFIG(TAG_LCD, "  Frame TX: continuous 4096-byte burst in one loop call; IRQs NOT disabled");
  ESP_LOGCONFIG(TAG_LCD, "OTA5901 nRF output; 256x128; row-lsb; white=1; external DisplayCLK; TE unused");
  // LOG_PIN implicitly expects a variable named TAG. This file uses TAG_LCD,
  // so pass the tag explicitly instead of relying on the macro.
  log_pin(TAG_LCD, "  DIO: ", dio_);
  log_pin(TAG_LCD, "  CLK: ", clk_);
  log_pin(TAG_LCD, "  CS: ", cs_);
  log_pin(TAG_LCD, "  RESET: ", reset_);
  ESP_LOGCONFIG(TAG_LCD, "  SPI half-period=%u us; boot pattern=%s", unsigned(half_us_), boot_test_ ? "yes" : "no");
}
}  // namespace gtag_ble_test
}  // namespace esphome
