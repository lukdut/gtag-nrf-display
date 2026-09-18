#pragma once
// Host model of the scheduler, radio and GPIO. The real driver is compiled
// unchanged; this model does not measure hardware timing or radio power.
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <tuple>
#include <vector>
#include <sys/types.h>

namespace sim {
inline uint64_t now_us = 0;
inline unsigned loop_calls = 0, adv_attempts = 0, adv_failures = 0;
inline unsigned adv_options = 0, adv_interval = 0;
inline bool advertising = false, connected = false;
inline uint64_t reset_low = 0, reset_high = 0;
struct Word { uint64_t time; uint16_t value; };
inline std::vector<Word> words;
inline bool levels[2][32] = {};
inline unsigned bit_count = 0, word = 0;
inline uint64_t word_start = 0;
inline std::function<void()> on_disable;
inline unsigned adc_reads = 0;
inline int adc_error = 0;
inline int16_t adc_raw = 3584;  // 2.1V ADC input -> 4.2V battery.

inline void pin(unsigned port, unsigned bit, bool value) {
  const bool old = levels[port][bit];
  levels[port][bit] = value;
  if (old == value) return;
  if (port == 1 && bit == 13) {
    (value ? reset_high : reset_low) = now_us;
  } else if (port == 1 && bit == 6) {
    if (!value) {
      assert(!advertising && !connected);
      bit_count = word = 0;
      word_start = now_us;
    } else if (bit_count) {
      assert(bit_count == 9);
      words.push_back({word_start, uint16_t(word)});
      bit_count = 0;
    }
  } else if (port == 1 && bit == 4 && value && !levels[1][6]) {
    word = (word << 1) | levels[0][11];
    ++bit_count;
  }
}
}  // namespace sim

namespace esphome {
class Component {
 public:
  virtual ~Component() = default;
  virtual void setup() {}
  virtual void loop() {}
  virtual void dump_config() {}
  bool is_failed() const { return failed; }
  void mark_failed() { failed = true; }
  void disable_loop() {
    enabled = false;
    if (sim::on_disable) {
      auto callback = std::move(sim::on_disable);
      sim::on_disable = {};
      callback();
    }
  }
  void enable_loop_soon_any_context() { pending_enable = true; }
  void set_timeout(const char *name, uint32_t delay, std::function<void()> callback) {
    timers[name] = {sim::now_us / 1000 + delay, std::move(callback)};
  }
  bool cancel_timeout(const char *name) { return timers.erase(name) != 0; }
  struct Timer { uint64_t deadline; std::function<void()> callback; };
  std::map<std::string, Timer> timers;
  bool enabled = true, pending_enable = false, failed = false;
};
struct Application { void feed_wdt() {} };
inline Application App;
}  // namespace esphome

inline void k_busy_wait(unsigned us) { sim::now_us += us; }
inline uint32_t k_uptime_get_32() { return sim::now_us / 1000; }
struct k_mutex {};
constexpr int K_FOREVER = -1;
inline void k_mutex_init(k_mutex *) {}
inline void k_mutex_lock(k_mutex *, int) {}
inline void k_mutex_unlock(k_mutex *) {}
inline int bt_le_adv_stop() { sim::advertising = false; return 0; }

struct device { unsigned port; };
inline device gpio0{0}, gpio1{1}, adc{2};
using gpio_pin_t = unsigned;
using gpio_flags_t = unsigned;
#define DT_NODELABEL(x) x
#define DEVICE_DT_GET(x) (&x)
#define GPIO_INPUT 1
#define GPIO_DISCONNECTED 0
#define GPIO_OUTPUT_HIGH 2
#define GPIO_OUTPUT_LOW 4
#define NRF_GPIO_DRIVE_S0H1 8
#define BIT(x) (1U << (x))
inline bool device_is_ready(const device *) { return true; }
inline int gpio_pin_configure(const device *dev, unsigned bit, unsigned flags) {
  if (dev == &gpio0 && bit == 31) assert(flags == GPIO_DISCONNECTED);
  sim::pin(dev->port, bit, flags & GPIO_OUTPUT_HIGH);
  return 0;
}

#define ADC_GAIN_1_4 4
#define ADC_REF_INTERNAL 0
#define ADC_ACQ_TIME_MICROSECONDS 1
#define ADC_ACQ_TIME(unit, value) value
#define NRF_SAADC_AIN7 8
struct adc_channel_cfg {
  int gain, reference, acquisition_time;
  unsigned channel_id, input_positive;
};
struct adc_sequence {
  unsigned channels;
  void *buffer;
  size_t buffer_size;
  unsigned resolution, oversampling;
  bool calibrate;
};
inline int adc_channel_setup(const device *dev, const adc_channel_cfg *channel) {
  assert(dev == &adc && channel->channel_id == 0);
  assert(channel->input_positive == NRF_SAADC_AIN7);
  assert(channel->acquisition_time == 40 && channel->gain == ADC_GAIN_1_4);
  assert(channel->reference == ADC_REF_INTERNAL);
  return 0;
}
inline int adc_read(const device *dev, const adc_sequence *sequence) {
  assert(dev == &adc && sequence->channels == 1);
  assert(sequence->buffer_size == sizeof(int16_t));
  assert(sequence->resolution == 12 && sequence->oversampling == 4 && sequence->calibrate);
  ++sim::adc_reads;
  *static_cast<int16_t *>(sequence->buffer) = sim::adc_raw;
  return sim::adc_error;
}
struct Register {
  unsigned port;
  bool value;
  void operator=(uint32_t mask) {
    for (unsigned bit = 0; bit < 32; ++bit)
      if (mask & BIT(bit)) sim::pin(port, bit, value);
  }
};
struct Port { Register OUTSET, OUTCLR; };
inline Port port0{{0, true}, {0, false}}, port1{{1, true}, {1, false}};
#define NRF_P0 (&port0)
#define NRF_P1 (&port1)

template<class... T> inline void log_stub(const T &...) {}
#define ESP_LOGI(...) log_stub(__VA_ARGS__)
#define ESP_LOGW(...) log_stub(__VA_ARGS__)
#define ESP_LOGE(...) log_stub(__VA_ARGS__)
#define ESP_LOGCONFIG(...) log_stub(__VA_ARGS__)

struct bt_conn {};
struct bt_gatt_attr {};
struct bt_conn_cb {
  void (*connected)(bt_conn *, uint8_t);
  void (*disconnected)(bt_conn *, uint8_t);
};
inline bt_conn_cb *callbacks = nullptr;
inline void bt_conn_cb_register(bt_conn_cb *cb) { callbacks = cb; }
inline int bt_enable(void *) { return 0; }
inline ssize_t bt_gatt_attr_read(bt_conn *, const bt_gatt_attr *, void *out,
                                 uint16_t len, uint16_t offset, const void *in, size_t size) {
  if (offset > size) return -1;
  len = std::min(size_t(len), size - offset);
  std::memcpy(out, static_cast<const uint8_t *>(in) + offset, len);
  return len;
}
#define BT_UUID_128_ENCODE(...) 0
#define BT_UUID_DECLARE_128(...) 0
#define BT_GATT_ERR(x) (-(x))
#define BT_ATT_ERR_INVALID_OFFSET 7
#define BT_ATT_ERR_INVALID_ATTRIBUTE_LEN 13
#define BT_ATT_ERR_UNLIKELY 14
#define BT_ATT_ERR_VALUE_NOT_ALLOWED 19
#define BT_GATT_CHRC_READ 1
#define BT_GATT_CHRC_WRITE 2
#define BT_GATT_CHRC_WRITE_WITHOUT_RESP 4
#define BT_GATT_PERM_READ 1
#define BT_GATT_PERM_WRITE 2
#define BT_GATT_PRIMARY_SERVICE(...) std::make_tuple(__VA_ARGS__)
#define BT_GATT_CHARACTERISTIC(...) std::make_tuple(__VA_ARGS__)
#define BT_GATT_SERVICE_DEFINE(name, ...) [[maybe_unused]] const auto name = std::make_tuple(__VA_ARGS__)
struct bt_data { unsigned type; };
#define BT_DATA_BYTES(type, ...) bt_data{type}
#define BT_DATA(type, ...) bt_data{type}
#define BT_DATA_FLAGS 1
#define BT_DATA_UUID128_ALL 2
#define BT_DATA_NAME_COMPLETE 3
#define BT_LE_AD_GENERAL 2
#define BT_LE_AD_NO_BREDR 4
#define CONFIG_BT_DEVICE_NAME "GTag Display"
#define BT_LE_ADV_OPT_CONNECTABLE 1
#define BT_LE_ADV_OPT_ONE_TIME 2
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
struct bt_le_adv_param { unsigned options, interval_min, interval_max; };
#define BT_LE_ADV_PARAM_INIT(options, min, max, peer) {options, min, max}
inline int bt_le_adv_start(const bt_le_adv_param *params, const bt_data *,
                           size_t, const bt_data *, size_t) {
  ++sim::adv_attempts;
  assert(!sim::connected);
  assert(params->interval_min == params->interval_max);
  sim::adv_options = params->options;
  sim::adv_interval = params->interval_min;
  if (sim::adv_failures) { --sim::adv_failures; return -12; }
  sim::advertising = true;
  return 0;
}
