#include "gtag_display.h"
#include "boot_logo.h"

#include <cerrno>
#include <cstring>

#ifndef USE_GTAG_ZIGBEE
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#endif
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/dt-bindings/gpio/nordic-nrf-gpio.h>
#include <zephyr/kernel.h>

#include <hal/nrf_gpio.h>
#ifdef USE_GTAG_BATTERY
#include <zephyr/drivers/adc.h>
#include <zephyr/dt-bindings/adc/nrf-saadc.h>
#endif

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace gtag_display {

namespace {

static const char *const TAG = "gtag_display";

constexpr uint32_t HALF_US = 2;
constexpr size_t FRAME_BYTES = 4096;
static_assert(boot_logo::FRAME.size() == FRAME_BYTES, "Boot logo must fill the LCD framebuffer");

const char *const PIN_NAMES[] = {"DIO", "CLK", "CS", "RESET"};
#ifdef USE_GTAG_BATTERY
constexpr uint8_t ADC_PINS[] = {2, 3, 4, 5, 28, 29, 30, 31};
#endif

#ifndef USE_GTAG_ZIGBEE
GTagDisplay *instance = nullptr;

// Preserve the already proven UUIDs so the existing HA integration works
// without any changes.
#define BT_UUID_GTAG_SERVICE_VAL \
  BT_UUID_128_ENCODE(0x7a1e0011, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)
#define BT_UUID_GTAG_LED_VAL \
  BT_UUID_128_ENCODE(0x7a1e0012, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)
#define BT_UUID_GTAG_FRAME_VAL \
  BT_UUID_128_ENCODE(0x7a1e0013, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)
#define BT_UUID_GTAG_STATUS_VAL \
  BT_UUID_128_ENCODE(0x7a1e0014, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)
#define BT_UUID_GTAG_CONTROL_VAL \
  BT_UUID_128_ENCODE(0x7a1e0015, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)
#define BT_UUID_GTAG_BATTERY_VAL \
  BT_UUID_128_ENCODE(0x7a1e0016, 0x6b5b, 0x4f6d, 0x8d6e, 0x0f4f47544147)

#define BT_UUID_GTAG_SERVICE BT_UUID_DECLARE_128(BT_UUID_GTAG_SERVICE_VAL)
#define BT_UUID_GTAG_LED BT_UUID_DECLARE_128(BT_UUID_GTAG_LED_VAL)
#define BT_UUID_GTAG_FRAME BT_UUID_DECLARE_128(BT_UUID_GTAG_FRAME_VAL)
#define BT_UUID_GTAG_STATUS BT_UUID_DECLARE_128(BT_UUID_GTAG_STATUS_VAL)
#define BT_UUID_GTAG_CONTROL BT_UUID_DECLARE_128(BT_UUID_GTAG_CONTROL_VAL)
#define BT_UUID_GTAG_BATTERY BT_UUID_DECLARE_128(BT_UUID_GTAG_BATTERY_VAL)

ssize_t read_battery(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                    void *buf, uint16_t len, uint16_t offset) {
  // GATT reads the cache only: no ADC work on the Bluetooth thread.
  const uint16_t mv = instance != nullptr ? instance->battery_mv() : 0xFFFF;
  const uint8_t value[] = {uint8_t(mv), uint8_t(mv >> 8)};
  return bt_gatt_attr_read(conn, attr, buf, len, offset, value, sizeof(value));
}

uint16_t read_le16(const uint8_t *p) {
  return uint16_t(p[0]) | (uint16_t(p[1]) << 8);
}

uint32_t read_le32(const uint8_t *p) {
  return uint32_t(p[0]) |
         (uint32_t(p[1]) << 8) |
         (uint32_t(p[2]) << 16) |
         (uint32_t(p[3]) << 24);
}

ssize_t read_led(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                 void *buf, uint16_t len, uint16_t offset) {
  const uint8_t value =
      (instance != nullptr && instance->virtual_led()) ? 1 : 0;

  return bt_gatt_attr_read(
      conn, attr, buf, len, offset, &value, sizeof(value));
}

ssize_t write_led(struct bt_conn *, const struct bt_gatt_attr *,
                  const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);

  if (len != 1)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);

  if (instance == nullptr)
    return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);

  instance->set_virtual_led(
      static_cast<const uint8_t *>(buf)[0] != 0);

  return len;
}

ssize_t write_frame(struct bt_conn *, const struct bt_gatt_attr *,
                    const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);

  if (len < 3)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);

  const auto *p = static_cast<const uint8_t *>(buf);
  const uint16_t pos =
      uint16_t(p[0]) | (uint16_t(p[1]) << 8);

  if (instance == nullptr ||
      !instance->write_frame_chunk(pos, p + 2, len - 2)) {
    return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  }

  return len;
}

ssize_t read_status(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                    void *buf, uint16_t len, uint16_t offset) {
  uint8_t status[8] = {};

  if (instance != nullptr)
    instance->get_status(status);

  return bt_gatt_attr_read(
      conn, attr, buf, len, offset, status, sizeof(status));
}

ssize_t write_control(struct bt_conn *, const struct bt_gatt_attr *,
                      const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);

  if (len == 0)
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);

  if (instance == nullptr)
    return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);

  const auto *p = static_cast<const uint8_t *>(buf);

  if (p[0] == 0x01) {
    frame::Descriptor descriptor{};

    // Protocol-v1 transport-neutral BEGIN descriptor:
    //
    // byte 0     : command = 0x01 (BEGIN)
    // byte 1     : protocol version = 1
    // byte 2     : codec id (0 RAW, 1 WHITE_RLE_V1)
    // bytes 3..4 : encoded payload size, little-endian
    // bytes 5..8 : frame/session id
    // bytes 9..12: CRC32 of the DECODED 4096-byte framebuffer
    //
    // This descriptor is not Bluetooth-specific; BLE is only one adapter.
    if (len == 13 || len == 17) {
      descriptor.version = p[1];
      descriptor.codec = static_cast<frame::Codec>(p[2]);
      descriptor.encoded_size = read_le16(p + 3);
      descriptor.frame_id = read_le32(p + 5);
      descriptor.raw_crc32 = read_le32(p + 9);
      descriptor.freshness_timeout_s = len == 17 ? read_le32(p + 13) : 0;
    } else if (len == 9) {
      // Backward compatibility with the previous reliable BLE sender:
      // 0x01 | frame_id:u32 | raw_crc32:u32
      descriptor.version = frame::PROTOCOL_VERSION;
      descriptor.codec = frame::Codec::RAW;
      descriptor.encoded_size = frame::RAW_FRAME_SIZE;
      descriptor.frame_id = read_le32(p + 1);
      descriptor.raw_crc32 = read_le32(p + 5);
    } else {
      return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }

    const auto result = instance->begin_frame(descriptor);

    return result == frame::BeginResult::REJECTED
        ? BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED)
        : len;
  }

  if (p[0] == 0x02) {
    if (len != 1)
      return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);

    return instance->commit_frame()
        ? len
        : BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  }

  if (p[0] == 0x03 && len == 13) {
    return instance->renew_freshness(read_le32(p + 1), read_le32(p + 5), read_le32(p + 9))
        ? len : BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
  }

  return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
}

BT_GATT_SERVICE_DEFINE(
    gtag_service,

    BT_GATT_PRIMARY_SERVICE(BT_UUID_GTAG_SERVICE),

    BT_GATT_CHARACTERISTIC(
        BT_UUID_GTAG_LED,
        BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
        BT_GATT_PERM_READ | BT_GATT_PERM_WRITE,
        read_led,
        write_led,
        nullptr),

    BT_GATT_CHARACTERISTIC(
        BT_UUID_GTAG_FRAME,
        BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
        BT_GATT_PERM_WRITE,
        nullptr,
        write_frame,
        nullptr),

    BT_GATT_CHARACTERISTIC(
        BT_UUID_GTAG_STATUS,
        BT_GATT_CHRC_READ,
        BT_GATT_PERM_READ,
        read_status,
        nullptr,
        nullptr),

    BT_GATT_CHARACTERISTIC(
        BT_UUID_GTAG_CONTROL,
        BT_GATT_CHRC_WRITE,
        BT_GATT_PERM_WRITE,
        nullptr,
        write_control,
        nullptr),

    BT_GATT_CHARACTERISTIC(
        BT_UUID_GTAG_BATTERY,
        BT_GATT_CHRC_READ,
        BT_GATT_PERM_READ,
        read_battery,
        nullptr,
        nullptr));

const struct bt_data ad[] = {
    BT_DATA_BYTES(
        BT_DATA_FLAGS,
        BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),

    BT_DATA_BYTES(
        BT_DATA_UUID128_ALL,
        BT_UUID_GTAG_SERVICE_VAL),
};

const struct bt_data sd[] = {
    BT_DATA(
        BT_DATA_NAME_COMPLETE,
        CONFIG_BT_DEVICE_NAME,
        sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

void connected_cb(struct bt_conn *, uint8_t err) {
  ESP_LOGI(TAG, "BLE connection result: 0x%02X", err);

  if (instance != nullptr)
    instance->connection_changed(err == 0);
}

void disconnected_cb(struct bt_conn *, uint8_t reason) {
  ESP_LOGI(TAG, "BLE disconnected: reason=0x%02X", reason);

  if (instance != nullptr)
    instance->connection_changed(false);
}

struct bt_conn_cb conn_callbacks = {};
#endif

}  // namespace


// ---------------------------------------------------------------------------
// BLE receiver
// ---------------------------------------------------------------------------

void GTagDisplay::connection_changed(bool connected) {
  this->connected_.store(connected);
  this->restart_advertising_.store(!connected);
  // GATT runs on a Zephyr thread. Wake the ESPHome loop without calling its
  // scheduler or touching LCD GPIO from that thread.
  this->enable_loop_soon_any_context();
}

void GTagDisplay::set_virtual_led(bool state) {
  this->virtual_led_.store(state);
  ESP_LOGI(TAG, "Virtual test LED = %s", state ? "ON" : "OFF");
}

frame::BeginResult GTagDisplay::begin_frame(
    const frame::Descriptor &descriptor) {

  const auto result = this->receiver_.begin(descriptor);

  ESP_LOGI(
      TAG,
      "BEGIN %s: proto=%u codec=%u encoded=%u id=%08X received=%u raw_crc=%08X",
      result == frame::BeginResult::NEW_SESSION
          ? "new"
          : result == frame::BeginResult::RESUMED
                ? "resume"
                : "rejected",
      unsigned(descriptor.version),
      unsigned(static_cast<uint8_t>(descriptor.codec)),
      unsigned(descriptor.encoded_size),
      unsigned(descriptor.frame_id),
      unsigned(this->receiver_.received()),
      unsigned(descriptor.raw_crc32));

  return result;
}

bool GTagDisplay::write_frame_chunk(
    uint16_t offset,
    const uint8_t *data,
    size_t len) {

  const auto before = this->receiver_.received();
  const bool ok = this->receiver_.write(offset, data, len);

  if (!ok) {
    ESP_LOGW(
        TAG,
        "DATA rejected: offset=%u len=%u expected=%u encoded_total=%u state=%u error=%u",
        unsigned(offset),
        unsigned(len),
        unsigned(before),
        unsigned(this->receiver_.encoded_size()),
        unsigned(static_cast<uint8_t>(this->receiver_.state())),
        unsigned(static_cast<uint8_t>(this->receiver_.error())));
  }

  return ok;
}

bool GTagDisplay::commit_frame() {
  bool first_commit = this->receiver_.state() != frame::State::COMPLETE;
#ifdef USE_GTAG_ZIGBEE
  // Restore a verified frame after a diagnostic pattern without redrawing
  // duplicate COMMITs while that frame is pending or already displayed.
  first_commit |= (!this->frame_pending_.load() || !this->queued_verified_) &&
      (!this->rendered_verified_ || this->rendered_frame_id_ != this->receiver_.descriptor().frame_id);
#endif
  if (!this->receiver_.commit(
          this->decoded_frame_.data(),
          this->decoded_frame_.size())) {

    ESP_LOGW(
        TAG,
        "COMMIT rejected: received=%u/%u codec=%u state=%u error=%u raw_crc=%08X",
        unsigned(this->receiver_.received()),
        unsigned(this->receiver_.encoded_size()),
        unsigned(static_cast<uint8_t>(this->receiver_.codec())),
        unsigned(static_cast<uint8_t>(this->receiver_.state())),
        unsigned(static_cast<uint8_t>(this->receiver_.error())),
        unsigned(this->receiver_.actual_raw_crc32()));

    return false;
  }

  if (first_commit) {
    std::memcpy(this->display_frame_.data(), this->decoded_frame_.data(), FRAME_BYTES);
    const auto &descriptor = this->receiver_.descriptor();
    k_mutex_lock(&this->freshness_mutex_, K_FOREVER);
    this->freshness_.frame(descriptor.frame_id, descriptor.raw_crc32,
                           descriptor.freshness_timeout_s, k_uptime_get_32());
    k_mutex_unlock(&this->freshness_mutex_);
#ifdef USE_GTAG_ZIGBEE
    this->queued_frame_id_ = this->receiver_.descriptor().frame_id;
    this->queued_verified_ = true;
#endif
    this->frame_pending_.store(true);
    this->enable_loop_soon_any_context();
  }

  ESP_LOGI(
      TAG,
      "Frame verified: encoded=%u raw=4096 codec=%u CRC32=%08X; new=%u",
      unsigned(this->receiver_.encoded_size()),
      unsigned(static_cast<uint8_t>(this->receiver_.codec())),
      unsigned(this->receiver_.actual_raw_crc32()), unsigned(first_commit));

  return true;
}

bool GTagDisplay::renew_freshness(uint32_t id, uint32_t crc, uint32_t sequence) {
  if (this->receiver_.state() != frame::State::COMPLETE ||
      this->receiver_.descriptor().frame_id != id || this->receiver_.actual_raw_crc32() != crc)
    return false;
  k_mutex_lock(&this->freshness_mutex_, K_FOREVER);
  const bool accepted = this->freshness_.renew(id, crc, sequence, k_uptime_get_32());
  k_mutex_unlock(&this->freshness_mutex_);
  if (accepted) this->enable_loop_soon_any_context();
  return accepted;
}

void GTagDisplay::service_freshness_() {
  const uint32_t now = k_uptime_get_32();
  k_mutex_lock(&this->freshness_mutex_, K_FOREVER);
  this->stale_overlay_ = this->freshness_.expired(now);
  const uint32_t remaining = this->freshness_.remaining(now);
  k_mutex_unlock(&this->freshness_mutex_);
  if (remaining != 0) {
    this->set_timeout("freshness", remaining, [this]() { this->enable_loop_soon_any_context(); });
  } else {
    this->cancel_timeout("freshness");
  }
  if (this->stale_overlay_ != this->shown_stale_overlay_)
    this->frame_pending_.store(true);
}

#ifndef USE_GTAG_ZIGBEE
bool GTagDisplay::start_advertising_() {
  // BLE uses 0.625 ms units. Round up to avoid advertising more frequently
  // than configured. ONE_TIME leaves restart after disconnect to loop(), so
  // no connection can race the LCD burst or replace its framebuffer.
  const uint32_t interval = (this->advertising_interval_ms_ * 8U + 4U) / 5U;
  const struct bt_le_adv_param params = BT_LE_ADV_PARAM_INIT(
      BT_LE_ADV_OPT_CONNECTABLE | BT_LE_ADV_OPT_ONE_TIME,
      interval, interval, nullptr);
  const int err = bt_le_adv_start(
      &params,
      ad,
      ARRAY_SIZE(ad),
      sd,
      ARRAY_SIZE(sd));

  if (err == 0 || err == -EALREADY) {
    ESP_LOGI(TAG, "BLE advertising ready; interval=%u ms", unsigned(this->advertising_interval_ms_));
    return true;
  }

  ESP_LOGW(TAG, "Advertising failed: %d; retry in 1s", err);
  return false;
}
#endif

#ifdef USE_GTAG_ZIGBEE
bool GTagDisplay::request_test_pattern(uint8_t pattern) {
  if (pattern > 3)
    return false;
  this->pending_pattern_.store(pattern + 1);
  this->enable_loop_soon_any_context();
  return true;
}

void GTagDisplay::process_zigbee_packet(const uint8_t *data, size_t len,
                                      uint8_t reply[zigbee_frame::REPLY_SIZE]) {
  using namespace zigbee_frame;
  Result result = Result::INVALID;
  const uint8_t command = data != nullptr && len > 0 ? data[0] : 0;
  if (data != nullptr && len > 0 && len <= MAX_PACKET_SIZE && !this->is_failed()) {
    if (command == uint8_t(Command::BEGIN) && (len == 13 || len == 17)) {
      frame::Descriptor descriptor{};
      descriptor.version = data[1];
      descriptor.codec = static_cast<frame::Codec>(data[2]);
      descriptor.encoded_size = read16(data + 3);
      descriptor.frame_id = read32(data + 5);
      descriptor.raw_crc32 = read32(data + 9);
      descriptor.freshness_timeout_s = len == 17 ? read32(data + 13) : 0;
      if (this->frame_pending_.load() && !(descriptor == this->receiver_.descriptor())) {
        result = Result::BUSY;
      } else {
        result = this->begin_frame(descriptor) == frame::BeginResult::REJECTED ? Result::RECEIVER : Result::OK;
      }
    } else if (command == uint8_t(Command::FRESHNESS) && len == 13) {
      result = this->renew_freshness(read32(data + 1), read32(data + 5), read32(data + 9))
          ? Result::OK : Result::SESSION;
    } else if ((command == uint8_t(Command::DATA) && len >= 8) ||
               ((command == uint8_t(Command::COMMIT) || command == uint8_t(Command::STATUS)) && len == 5)) {
      const uint32_t session = read32(data + 1);
      if (command == uint8_t(Command::STATUS)) {
        result = Result::OK;
      } else if (this->receiver_.state() == frame::State::IDLE ||
                 session != this->receiver_.descriptor().frame_id) {
        result = Result::SESSION;
      } else if (command == uint8_t(Command::DATA)) {
        result = this->write_frame_chunk(read16(data + 5), data + 7, len - 7) ? Result::OK : Result::RECEIVER;
      } else {
        result = this->commit_frame() ? Result::OK : Result::RECEIVER;
      }
    }
  }
  std::memset(reply, 0, REPLY_SIZE);
  reply[0] = VERSION;
  reply[1] = command;
  reply[2] = uint8_t(result);
  write32(reply + 3, this->receiver_.descriptor().frame_id);
  this->get_status(reply + 7);
  reply[15] = (this->frame_pending_.load() ? FLAG_PENDING : 0) |
              (this->rendered_verified_ ? FLAG_RENDERED : 0) |
              (this->shown_stale_overlay_ ? FLAG_STALE : 0);
  write32(reply + 16, this->rendered_frame_id_);
}
#endif

void GTagDisplay::queue_pattern_(BootPattern pattern) {
  if (pattern == BootPattern::NONE)
    return;
  k_mutex_lock(&this->freshness_mutex_, K_FOREVER);
  this->freshness_.clear();
  k_mutex_unlock(&this->freshness_mutex_);
#ifdef USE_GTAG_ZIGBEE
  this->queued_verified_ = false;
#endif

  for (size_t i = 0; i < FRAME_BYTES; ++i) {
    uint8_t value = 0xFF;
    switch (pattern) {
      case BootPattern::LOGO: value = boot_logo::FRAME[i]; break;
      case BootPattern::BLACK: value = 0x00; break;
      case BootPattern::CHECKERBOARD:
        value = ((((i / 32) / 8) + (i % 32)) & 1U) ? 0x00 : 0xFF;
        break;
      case BootPattern::STRIPES: value = 0x0F; break;
      case BootPattern::NONE:
      case BootPattern::WHITE: break;
    }
    this->display_frame_[i] = value;
  }
  // The local logo/diagnostic is not reported as a received remote frame.
  this->frame_pending_.store(true);
}


// ---------------------------------------------------------------------------
// LCD — intentionally kept equivalent to proven LCD3-DIRECT-01/v36.
// ---------------------------------------------------------------------------

void GTagDisplay::wait_(Stage next, uint32_t delay_ms) {
  this->stage_ = next;
  this->next_ms_ = k_uptime_get_32() + delay_ms;
  // Also request a component phase immediately: enable_loop() alone can wait
  // for the application's longer idle loop interval after the timer fires.
  this->set_timeout("lcd_step", delay_ms, [this]() { this->enable_loop_soon_any_context(); });
}

bool GTagDisplay::configure_pins_() {
  for (unsigned i = 0; i < 4; ++i) {
    const auto pin = this->lcd_pins_[i];
    if (pin > 47) return false;
    for (unsigned j = 0; j < i; ++j)
      if (pin == this->lcd_pins_[j]) return false;
#ifdef USE_GTAG_BATTERY
    if (pin == this->battery_pin_) return false;
#endif
    const auto *port = pin < 32 ? DEVICE_DT_GET(DT_NODELABEL(gpio0)) : DEVICE_DT_GET(DT_NODELABEL(gpio1));
    if (!device_is_ready(port)) {
      ESP_LOGE(TAG, "LCD GPIO device not ready: %s", PIN_NAMES[i]);
      return false;
    }
  }

  const Signal order[] = {
      Signal::CS,
      Signal::RESET,
      Signal::SCLK,
      Signal::DIO,
  };

  for (const Signal signal : order) {
    const auto index = static_cast<unsigned>(signal);
    const auto pin = this->lcd_pins_[index];
    const auto *port = pin < 32 ? DEVICE_DT_GET(DT_NODELABEL(gpio0)) : DEVICE_DT_GET(DT_NODELABEL(gpio1));
    const bool high =
        signal == Signal::CS || signal == Signal::RESET;

    gpio_flags_t flags =
        GPIO_INPUT |
        (high ? GPIO_OUTPUT_HIGH : GPIO_OUTPUT_LOW);

    if (signal == Signal::SCLK || signal == Signal::DIO)
      flags |= NRF_GPIO_DRIVE_S0H1;

    const int rc =
        gpio_pin_configure(port, pin % 32, flags);

    if (rc != 0) {
      ESP_LOGE(
          TAG,
          "LCD CONFIG %s failed rc=%d",
          PIN_NAMES[index],
          rc);

      return false;
    }

    ESP_LOGI(TAG, "LCD CONFIG %s P%u.%02u rc=0", PIN_NAMES[index], unsigned(pin / 32), unsigned(pin % 32));
  }

  this->pins_configured_ = true;
  return true;
}

bool GTagDisplay::write_(Signal signal, bool high) {
  const auto pin = this->lcd_pins_[static_cast<unsigned>(signal)];
  auto *port = pin < 32 ? NRF_P0 : NRF_P1;
  if (high)
    port->OUTSET = BIT(pin % 32);
  else
    port->OUTCLR = BIT(pin % 32);
  return true;
}

bool GTagDisplay::reset_pulse_() {
  ++this->resets_;

  this->write_(Signal::CS, true);
  this->write_(Signal::SCLK, false);
  this->write_(Signal::DIO, false);

  this->write_(Signal::RESET, false);
  k_busy_wait(50);
  this->write_(Signal::RESET, true);

  ESP_LOGI(
      TAG,
      "LCD RESET #%u LOW 50us -> HIGH",
      static_cast<unsigned>(this->resets_));

  return true;
}

bool GTagDisplay::word_(bool is_data, uint8_t byte) {
  this->write_(Signal::SCLK, false);
  this->write_(Signal::CS, false);
  k_busy_wait(HALF_US);

  const uint16_t value =
      uint16_t(is_data ? 0x100U : 0U) | byte;

  for (int bit = 8; bit >= 0; --bit) {
    this->write_(
        Signal::DIO,
        ((value >> bit) & 1U) != 0);

    k_busy_wait(HALF_US);

    this->write_(Signal::SCLK, true);
    k_busy_wait(HALF_US);
    this->write_(Signal::SCLK, false);
  }

  k_busy_wait(HALF_US);
  this->write_(Signal::CS, true);
  k_busy_wait(HALF_US);

  ++this->words_;
  return true;
}

bool GTagDisplay::address_(uint16_t address) {
  return
      this->word_(false, 0x2A) &&
      this->word_(true, uint8_t(address >> 8)) &&
      this->word_(true, uint8_t(address));
}

bool GTagDisplay::clear_ram_() {
  if (!this->word_(false, 0x11))
    return false;

  for (uint16_t base = 0; base < FRAME_BYTES; base += 256) {
    if (!this->address_(base) ||
        !this->word_(false, 0x2C)) {
      return false;
    }

    for (unsigned j = 0; j < 256; ++j) {
      if (!this->word_(true, 0xFF))
        return false;
    }

    App.feed_wdt();
  }

  return this->write_(Signal::DIO, false);
}

bool GTagDisplay::send_frame_(const uint8_t *frame) {
  if (frame == nullptr)
    return false;

  ESP_LOGI(TAG, "LCD FRAME begin");

  const uint32_t started = k_uptime_get_32();
  const uint32_t old_words = this->words_;

  if (!this->address_(0) ||
      !this->word_(false, 0x2C)) {
    return false;
  }

  for (size_t i = 0; i < FRAME_BYTES; ++i) {
    const uint8_t value = frame[i] ^ (this->stale_overlay_ ? freshness::mask(i) : 0);
    if (!this->word_(true, value))
      return false;

    if ((i & 0xFFU) == 0xFFU)
      App.feed_wdt();
  }

  this->write_(Signal::DIO, false);

  ++this->frames_;

  ESP_LOGI(
      TAG,
      "LCD FRAME TX_DONE bytes=4096 words=%u ms=%u frame_no=%u",
      static_cast<unsigned>(this->words_ - old_words),
      static_cast<unsigned>(k_uptime_get_32() - started),
      static_cast<unsigned>(this->frames_));

  return true;
}

void GTagDisplay::service_lcd_() {
  const uint32_t now = k_uptime_get_32();

  if (this->stage_ != Stage::READY) {
    if (int32_t(now - this->next_ms_) < 0)
      return;

    switch (this->stage_) {
      case Stage::BOOT_WAIT:
        this->reset_pulse_();
        ESP_LOGI(
            TAG,
            "LCD wait 50ms AFTER RESET; stock XCLK");
        this->wait_(Stage::AFTER_RESET, 50);
        return;

      case Stage::AFTER_RESET:
        ESP_LOGI(TAG, "LCD INIT 0x11 + RAM clear");

        if (!this->clear_ram_())
          return;

        this->wait_(Stage::AFTER_CLEAR, 10);
        return;

      case Stage::AFTER_CLEAR:
        this->word_(false, 0x4C);
        this->word_(true, 0x0C);
        this->word_(true, 0x00);
        this->word_(true, 0x00);
        this->word_(true, 0x00);

        this->wait_(Stage::AFTER_4C, 4);
        return;

      case Stage::AFTER_4C:
        this->word_(false, 0x4D);
        this->word_(true, 0xFF);
        this->word_(true, 0x00);
        this->word_(true, 0x7F);

        this->wait_(Stage::AFTER_4D, 1);
        return;

      case Stage::AFTER_4D:
        this->word_(false, 0x4E);
        this->word_(true, 0x60);

        ESP_LOGI(TAG, "LCD INIT_SENT; wait 500ms");
        this->wait_(Stage::AFTER_4E, 500);
        return;

      case Stage::AFTER_4E:
        this->stage_ = Stage::READY;
        ESP_LOGI(
            TAG,
            "LCD READY");
        break;

      case Stage::READY:
        break;
    }
  }

  // A local expiry can request a redraw while BLE is advertising. Stop it
  // first, then recheck the connection flag before touching LCD/frame data.
#ifndef USE_GTAG_ZIGBEE
  if (this->stage_ == Stage::READY && this->frame_pending_.load() && !this->connected_.load()) {
    const int err = bt_le_adv_stop();
    if (err != 0 && err != -EALREADY) {
      this->set_timeout("lcd_retry", 1000, [this]() { this->enable_loop_soon_any_context(); });
      return;
    }
  }
#endif
  // HA verifies STATUS and disconnects. ONE_TIME advertising prevents the
  // stack from accepting another connection until this burst has finished.
  if (this->stage_ == Stage::READY &&
      !this->connected_.load() &&
      this->frame_pending_.exchange(false)) {

#ifndef USE_GTAG_ZIGBEE
    this->restart_advertising_.store(false);
#endif

    if (!this->send_frame_(this->display_frame_.data())) {
      ESP_LOGE(TAG, "LCD frame render failed");
      this->frame_pending_.store(true);
    } else {
      this->shown_stale_overlay_ = this->stale_overlay_;
#ifdef USE_GTAG_ZIGBEE
      this->rendered_verified_ = this->queued_verified_;
      this->rendered_frame_id_ = this->queued_frame_id_;
#endif
      ESP_LOGI(TAG, "Frame rendered on LCD");
    }

    // Advertise again only after the LCD burst is complete.
#ifndef USE_GTAG_ZIGBEE
    this->restart_advertising_.store(true);
#endif
  }
}


// ---------------------------------------------------------------------------
// Component lifecycle
// ---------------------------------------------------------------------------

#ifdef USE_GTAG_BATTERY
void GTagDisplay::setup_battery_() {
  const auto *adc_dev = DEVICE_DT_GET(DT_NODELABEL(adc));
  if (!device_is_ready(adc_dev)) {
    ESP_LOGW(TAG, "Battery ADC unavailable");
    return;
  }
  unsigned input = 0;
  for (unsigned i = 0; i < 8; ++i)
    if (ADC_PINS[i] == this->battery_pin_) input = NRF_SAADC_AIN0 + i;
  if (input == 0) {
    ESP_LOGW(TAG, "Battery pin has no SAADC input");
    return;
  }
  // Disconnect the digital input buffer and pulls; SAADC still reads the pin.
  int err = gpio_pin_configure(DEVICE_DT_GET(DT_NODELABEL(gpio0)), this->battery_pin_, GPIO_DISCONNECTED);
  struct adc_channel_cfg channel = {};
  channel.gain = ADC_GAIN_1_4;  // Internal 0.6V reference / gain = 2.4V full scale.
  channel.reference = ADC_REF_INTERNAL;
  // The 1M/1M divider has a 500k source resistance: use the longest acquisition.
  channel.acquisition_time = ADC_ACQ_TIME(ADC_ACQ_TIME_MICROSECONDS, 40);
  channel.channel_id = 0;
  channel.input_positive = input;
  if (err == 0)
    err = adc_channel_setup(adc_dev, &channel);
  this->battery_adc_ready_ = err == 0;
  if (err != 0)
    ESP_LOGW(TAG, "Battery ADC setup failed: %d", err);
}

void GTagDisplay::sample_battery_() {
  if (!this->battery_adc_ready_)
    this->setup_battery_();
  int16_t raw = 0;
  struct adc_sequence sequence = {};
  sequence.channels = BIT(0);
  sequence.buffer = &raw;
  sequence.buffer_size = sizeof(raw);
  sequence.resolution = 12;
  sequence.oversampling = 4;  // Average 16 conversions in one burst.
  sequence.calibrate = true;
  const int err = this->battery_adc_ready_
      ? adc_read(DEVICE_DT_GET(DT_NODELABEL(adc)), &sequence) : -ENODEV;
  if (err != 0 || raw >= 4095) {
    this->battery_mv_.store(0xFFFF);
    ESP_LOGW(TAG, "Battery sample invalid: ADC error=%d raw=%d", err, int(raw));
  } else {
    // 0.6V reference, gain 1/4, 12 bits, external divider x2.
    const float mv = (raw > 0 ? raw : 0) * (4800.0f / 4096.0f) * this->battery_calibration_;
    this->battery_mv_.store(static_cast<uint16_t>(mv + 0.5f));
    ESP_LOGI(TAG, "Battery: %u mV", unsigned(this->battery_mv()));
  }
  // Zephyr's nRF SAADC driver stops and disables the ADC after adc_read().
  // This timer wakes the scheduler once per five minutes, with no polling loop.
  this->set_timeout("battery_sample", 300000, [this]() { this->sample_battery_(); });
}
#endif

void GTagDisplay::setup() {
  k_mutex_init(&this->freshness_mutex_);
#ifndef USE_GTAG_ZIGBEE
  instance = this;
#endif

  ESP_LOGI(
      TAG,
      "GTag Display: protocol v1, System ON idle, event-driven LCD");

  if (!this->configure_pins_()) {
    this->mark_failed();
    return;
  }

  this->queue_pattern_(this->boot_pattern_);

#ifndef USE_GTAG_ZIGBEE
  const int err = bt_enable(nullptr);

  if (err != 0 && err != -EALREADY) {
    ESP_LOGE(TAG, "bt_enable failed: %d", err);
    this->mark_failed();
    return;
  }

  conn_callbacks.connected = connected_cb;
  conn_callbacks.disconnected = disconnected_cb;
  bt_conn_cb_register(&conn_callbacks);

  this->restart_advertising_.store(true);
#endif

  // Preserve the initial v36 boot wait before RESET.
  this->wait_(Stage::BOOT_WAIT, 3000);
#ifdef USE_GTAG_BATTERY
  // 1M || 1M with 100nF settles in ~250ms (5 tau); allow 1s after boot.
  this->set_timeout("battery_sample", 1000, [this]() { this->sample_battery_(); });
#endif

  ESP_LOGI(TAG, "LCD startup scheduled");
}

void GTagDisplay::loop() {
  if (this->is_failed())
    return;

  // No polling while idle. BLE callbacks queue an enable via the thread-safe
  // API; timed startup/retry stages enable us through the ESPHome scheduler.
  // Disable BEFORE checking work, so a concurrent callback cannot lose a wake.
  // Zephyr may enter System ON sleep whenever all threads are waiting.
  this->disable_loop();

#ifdef USE_GTAG_ZIGBEE
  const uint8_t requested = this->pending_pattern_.exchange(0);
  // Do not let a UI diagnostic overwrite an active or just-committed frame.
  if (requested != 0 && this->receiver_.state() != frame::State::RECEIVING &&
      !(this->frame_pending_.load() && this->queued_verified_))
    this->queue_pattern_(static_cast<BootPattern>(requested));
#endif

  // If a committed frame is waiting after disconnect, render it before
  // restarting advertising. This keeps LCD SPI isolated from GATT traffic.
  this->service_freshness_();
  this->service_lcd_();

#ifndef USE_GTAG_ZIGBEE
  if (this->restart_advertising_.load() &&
      !this->connected_.load() &&
      !this->frame_pending_.load() &&
      this->stage_ == Stage::READY) {

    this->restart_advertising_.store(false);

    if (!this->start_advertising_()) {
      this->restart_advertising_.store(true);
      this->set_timeout("advertising_retry", 1000, [this]() { this->enable_loop_soon_any_context(); });
    } else {
      this->cancel_timeout("advertising_retry");
    }
  }
#endif
}

void GTagDisplay::dump_config() {
#ifdef USE_GTAG_ZIGBEE
  ESP_LOGCONFIG(TAG, "GTag Display; Zigbee frame transport v1, diagnostics and battery");
#else
  ESP_LOGCONFIG(
      TAG,
      "GTag Display; protocol-v1 codecs RAW/WHITE_RLE_V1; UUIDs 0011..0016");
#endif
#ifdef USE_GTAG_BATTERY
  ESP_LOGCONFIG(TAG, "  Battery: P0.%02u, 1M/1M divider, 5min, calibration=%.4f",
                unsigned(this->battery_pin_), this->battery_calibration_);
#else
  ESP_LOGCONFIG(TAG, "  Battery measurement disabled");
#endif

#ifndef USE_GTAG_ZIGBEE
  ESP_LOGCONFIG(TAG, "  Power saving: System ON idle; BLE remains connectable");
  ESP_LOGCONFIG(TAG, "  Advertising interval: %u ms; boot pattern: %u",
                unsigned(this->advertising_interval_ms_), unsigned(this->boot_pattern_));
#else
  ESP_LOGCONFIG(TAG, "  Zigbee radio/sleep managed by the zigbee component; boot pattern: %u",
                unsigned(this->boot_pattern_));
#endif

  for (unsigned i = 0; i < this->lcd_pins_.size(); ++i)
    ESP_LOGCONFIG(TAG, "  LCD %s: P%u.%02u", PIN_NAMES[i],
                  unsigned(this->lcd_pins_[i] / 32), unsigned(this->lcd_pins_[i] % 32));

  ESP_LOGCONFIG(
      TAG,
      "  LCD timing: HALF_US=%u; direct OUTSET/OUTCLR; stock XCLK",
      unsigned(HALF_US));

#ifdef CONFIG_BT_CTLR_TX_PWR_DBM
  ESP_LOGCONFIG(
      TAG,
      "  Bluetooth controller TX power: %d dBm",
      CONFIG_BT_CTLR_TX_PWR_DBM);
#endif
}

}  // namespace gtag_display
}  // namespace esphome
