#include "gtag_ble_test.h"

#include <cerrno>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace gtag_ble_test {

static const char *const TAG = "gtag_ble_test";
static GTagBLETest *instance = nullptr;

// Preserve the ALREADY WORKING 0011..0015 UUIDs and GATT attribute order.
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

#define BT_UUID_GTAG_SERVICE BT_UUID_DECLARE_128(BT_UUID_GTAG_SERVICE_VAL)
#define BT_UUID_GTAG_LED BT_UUID_DECLARE_128(BT_UUID_GTAG_LED_VAL)
#define BT_UUID_GTAG_FRAME BT_UUID_DECLARE_128(BT_UUID_GTAG_FRAME_VAL)
#define BT_UUID_GTAG_STATUS BT_UUID_DECLARE_128(BT_UUID_GTAG_STATUS_VAL)
#define BT_UUID_GTAG_CONTROL BT_UUID_DECLARE_128(BT_UUID_GTAG_CONTROL_VAL)

static uint32_t read_le32(const uint8_t *p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8) |
         (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}

static ssize_t read_led(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                        void *buf, uint16_t len, uint16_t offset) {
  const uint8_t value = (instance != nullptr && instance->led_state()) ? 1 : 0;
  return bt_gatt_attr_read(conn, attr, buf, len, offset, &value, sizeof(value));
}

static ssize_t write_led(struct bt_conn *, const struct bt_gatt_attr *,
                         const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0) return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
  if (len != 1) return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
  if (instance == nullptr) return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  instance->queue_led_state(static_cast<const uint8_t *>(buf)[0] != 0);
  return len;
}

static ssize_t write_frame(struct bt_conn *, const struct bt_gatt_attr *,
                           const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0) return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
  if (len < 3) return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
  const auto *p = static_cast<const uint8_t *>(buf);
  const uint16_t pos = uint16_t(p[0]) | (uint16_t(p[1]) << 8);
  if (instance == nullptr || !instance->write_frame_chunk(pos, p + 2, len - 2))
    return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  return len;
}

static ssize_t read_status(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                           void *buf, uint16_t len, uint16_t offset) {
  uint8_t status[8] = {};
  if (instance != nullptr) instance->get_status(status);
  return bt_gatt_attr_read(conn, attr, buf, len, offset, status, sizeof(status));
}

static ssize_t write_control(struct bt_conn *, const struct bt_gatt_attr *,
                             const void *buf, uint16_t len, uint16_t offset, uint8_t) {
  if (offset != 0) return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
  if (len == 0) return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
  if (instance == nullptr) return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  const auto *p = static_cast<const uint8_t *>(buf);
  if (p[0] == 0x01) {
    // v0.3 BEGIN: opcode + uint32 frame_id + uint32 CRC32 (all little-endian).
    // Old one-byte BEGIN remains supported, but cannot resume by frame ID.
    if (len != 1 && len != 9) return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    instance->begin_frame(len == 9, len == 9 ? read_le32(p + 1) : 0,
                         len == 9 ? read_le32(p + 5) : 0);
    return len;
  }
  if (p[0] == 0x02) {
    if (len != 1) return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    return instance->commit_frame() ? len : BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  }
  return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
}

BT_GATT_SERVICE_DEFINE(
    gtag_service,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_GTAG_SERVICE),
    BT_GATT_CHARACTERISTIC(BT_UUID_GTAG_LED,
        BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
        BT_GATT_PERM_READ | BT_GATT_PERM_WRITE, read_led, write_led, nullptr),
    BT_GATT_CHARACTERISTIC(BT_UUID_GTAG_FRAME,
        BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
        BT_GATT_PERM_WRITE, nullptr, write_frame, nullptr),
    BT_GATT_CHARACTERISTIC(BT_UUID_GTAG_STATUS, BT_GATT_CHRC_READ,
        BT_GATT_PERM_READ, read_status, nullptr, nullptr),
    BT_GATT_CHARACTERISTIC(BT_UUID_GTAG_CONTROL, BT_GATT_CHRC_WRITE,
        BT_GATT_PERM_WRITE, nullptr, write_control, nullptr)
);

static const struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
    BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_GTAG_SERVICE_VAL),
};
static const struct bt_data sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME, sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static void connected_cb(struct bt_conn *, uint8_t err) {
  ESP_LOGI(TAG, "BLE connection result: 0x%02X", err);
  if (instance != nullptr) instance->connection_changed(err == 0);
}
static void disconnected_cb(struct bt_conn *, uint8_t reason) {
  ESP_LOGI(TAG, "BLE disconnected: reason=0x%02X", reason);
  if (instance != nullptr) instance->connection_changed(false);
}
static struct bt_conn_cb conn_callbacks = {};

void GTagBLETest::connection_changed(bool connected) {
  connected_.store(connected);
  restart_advertising_.store(!connected);
}
void GTagBLETest::queue_led_state(bool state) {
  pending_led_state_.store(state);
  led_update_pending_.store(true);
}
void GTagBLETest::apply_led_state_(bool state) {
  led_pin_->digital_write(state);
  led_state_.store(state);
  ESP_LOGI(TAG, "LED = %s", state ? "ON" : "OFF");
}
void GTagBLETest::begin_frame(bool tagged, uint32_t id, uint32_t crc) {
  bool fresh = true;
  if (tagged) fresh = receiver_.begin(id, crc);
  else receiver_.begin_legacy();
  if (receiver_.state() != FrameReceiver::COMPLETE) queue_led_state(false);
  ESP_LOGI(TAG, "BEGIN %s: id=%08X received=%u state=%u crc_expected=%08X",
           fresh ? "new" : "resume", unsigned(id), unsigned(receiver_.received()),
           unsigned(receiver_.state()), unsigned(crc));
}
bool GTagBLETest::write_frame_chunk(uint16_t offset, const uint8_t *data, size_t len) {
  const auto before = receiver_.received();
  const bool ok = receiver_.write(offset, data, len);
  if (!ok) {
    ESP_LOGW(TAG, "FRAME rejected: offset=%u len=%u expected=%u state=%u error=%u",
             unsigned(offset), unsigned(len), unsigned(before),
             unsigned(receiver_.state()), unsigned(receiver_.error()));
  } else if (offset < before) {
    ESP_LOGD(TAG, "FRAME duplicate accepted: offset=%u len=%u", unsigned(offset), unsigned(len));
  }
  return ok;
}
bool GTagBLETest::commit_frame() {
  const bool first_commit = receiver_.state() != FrameReceiver::COMPLETE;
  const bool ok = receiver_.commit();
  queue_led_state(ok);
  if (ok) {
    ESP_LOGI(TAG, "Frame OK: 4096 bytes, CRC32=%08X", unsigned(receiver_.crc()));
    if (first_commit && lcd_.configured()) {
      // Fast snapshot only; LCD IO is deferred to the normal ESPHome loop.
      lcd_.queue_frame(receiver_.data(), FrameReceiver::SIZE, receiver_.session_id(), receiver_.crc());
    }
  } else {
    ESP_LOGW(TAG, "COMMIT rejected: received=%u state=%u error=%u crc=%08X",
             unsigned(receiver_.received()), unsigned(receiver_.state()),
             unsigned(receiver_.error()), unsigned(receiver_.crc()));
  }
  return ok;
}
bool GTagBLETest::start_advertising_() {
  // Proven API for the user's NCS 2.9.2, not the Zephyr 4.x FAST_1 macro.
  const int err = bt_le_adv_start(BT_LE_ADV_CONN, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
  if (err == 0 || err == -EALREADY) {
    ESP_LOGI(TAG, "BLE advertising ready");
    return true;
  }
  ESP_LOGW(TAG, "Advertising failed: %d; retry in 1s", err);
  return false;
}
void GTagBLETest::setup() {
  instance = this;
  if (led_pin_ == nullptr) { mark_failed(); return; }
  led_pin_->setup();
  apply_led_state_(false);
  lcd_.setup();
  const int err = bt_enable(nullptr);
  if (err != 0 && err != -EALREADY) {
    ESP_LOGE(TAG, "bt_enable failed: %d", err);
    mark_failed();
    return;
  }
  conn_callbacks.connected = connected_cb;
  conn_callbacks.disconnected = disconnected_cb;
  bt_conn_cb_register(&conn_callbacks);
  ESP_LOGI(TAG, "GTag LCD v0.5; reliable transport v0.3/v0.4; Bluetooth initialized");
  restart_advertising_.store(true);
}
void GTagBLETest::loop() {
  if (led_update_pending_.exchange(false)) apply_led_state_(pending_led_state_.load());
  lcd_.loop();
  if (restart_advertising_.load() && !connected_.load() &&
      int32_t(millis() - next_advertising_attempt_) >= 0) {
    // Clearing before the call preserves a restart request from a later callback.
    restart_advertising_.store(false);
    if (!start_advertising_()) restart_advertising_.store(true);
    next_advertising_attempt_ = millis() + 1000;
  }
}
void GTagBLETest::dump_config() {
  ESP_LOGCONFIG(TAG, "GTag LCD v0.5 (unchanged UUID 0011..0015)");
  LOG_PIN("  LED: ", led_pin_);
  lcd_.dump_config();
#ifdef CONFIG_BT_CTLR_TX_PWR_DBM
  ESP_LOGCONFIG(TAG, "  Configured controller TX power: %d dBm", CONFIG_BT_CTLR_TX_PWR_DBM);
#endif
}

}  // namespace gtag_ble_test
}  // namespace esphome
