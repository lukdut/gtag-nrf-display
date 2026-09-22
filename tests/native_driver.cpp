#include "native_stubs.h"
#include <memory>
#include "../config/esphome/components/gtag_display/gtag_display.cpp"
#include "../config/esphome/components/gtag_display/zigbee_polling.h"

using namespace esphome::gtag_display;
class TestDisplay : public GTagDisplay {
 public:
  TestDisplay() {
#ifdef USE_GTAG_BATTERY
    battery_protection_ = false;  // Legacy protocol tests isolate the battery policy.
#endif
  }
  unsigned frames() const { return frames_; }
};
static std::unique_ptr<TestDisplay> display;
#if defined(USE_GTAG_ZIGBEE) && defined(USE_SENSOR)
static esphome::sensor::Sensor battery_sensor, frames_sensor;
#endif

static void run_until(uint64_t end_us) {
  for (unsigned steps = 0; steps < 1000; ++steps) {
    if (sim::reboots) return;
    auto &timers = display->timers;
    auto next = std::min_element(timers.begin(), timers.end(),
        [](const auto &a, const auto &b) { return a.second.deadline < b.second.deadline; });
    if (next != timers.end() && next->second.deadline <= sim::now_us / 1000) {
      auto callback = std::move(next->second.callback);
      timers.erase(next);
      callback();
      continue;
    }
    if (display->pending_enable) {
      display->pending_enable = false;
      display->enabled = true;
    }
    if (display->enabled) {
      ++sim::loop_calls;
      display->loop();
      continue;
    }
    if (sim::now_us >= end_us) return;
    sim::now_us = next == timers.end() ? end_us : std::min(end_us, next->second.deadline * 1000);
  }
  assert(false && "Unexpected polling or an unscheduled wake");
}

extern "C" {
void firmware_info_read(uint8_t *out) { firmware_info::make(out); }
#ifndef USE_GTAG_ZIGBEE
int firmware_info_gatt(uint8_t *out, uint16_t len, uint16_t offset) {
  return read_info(nullptr, nullptr, out, len, offset);
}
#endif
void firmware_info_zigbee(uint8_t *out) { firmware_info::zigbee_reply(out); }
static void create_display(unsigned pattern, unsigned interval, unsigned dio, unsigned clk,
                           unsigned cs, unsigned reset, unsigned battery_pin, bool protect = false,
                           int raw = 3584, int error = 0) {
  display.reset();
  sim::now_us = sim::loop_calls = sim::adv_attempts = sim::adv_failures = 0;
  sim::advertising = sim::connected = false;
  sim::reset_low = sim::reset_high = sim::bit_count = sim::word = 0;
  sim::words.clear();
  std::memset(sim::levels, 0, sizeof(sim::levels));
  sim::on_disable = {};
  sim::adc_reads = sim::reboots = sim::radio_starts = 0;
#ifdef USE_GTAG_ZIGBEE
#ifdef USE_GTAG_BATTERY
  zigbee_radio_gate = {};
#endif
#endif
  sim::adc_raw = raw;
  sim::adc_error = error;
  sim::lcd_pins = {{dio, clk, cs, reset}};
  sim::adc_pin = battery_pin;
  display = std::make_unique<TestDisplay>();
#if defined(USE_GTAG_ZIGBEE) && defined(USE_SENSOR)
  battery_sensor = {};
  frames_sensor = {};
#ifdef USE_GTAG_BATTERY
  display->set_battery_sensor(&battery_sensor);
#endif
  display->set_rendered_frames_sensor(&frames_sensor);
#endif
  display->set_boot_pattern(static_cast<BootPattern>(pattern));
  display->set_advertising_interval(interval);
  display->set_dio_pin(dio);
  display->set_clk_pin(clk);
  display->set_cs_pin(cs);
  display->set_reset_pin(reset);
#ifdef USE_GTAG_BATTERY
  if (protect) display->set_battery_protection(3306, 3450);
#ifdef USE_GTAG_ZIGBEE
  zigbee_radio_gate.request([]() { ++sim::radio_starts; });
#endif
  display->set_battery_pin(battery_pin);
  // Existing wire-protocol tests require the unmodified source framebuffer.
  display->set_battery_indicator(protect);
#else
  (void) protect;
#endif
  display->setup();
}
void firmware_create_with_pins(unsigned pattern, unsigned interval, unsigned dio, unsigned clk,
                               unsigned cs, unsigned reset, unsigned battery_pin) {
  create_display(pattern, interval, dio, clk, cs, reset, battery_pin);
}
void firmware_create(unsigned pattern, unsigned interval) {
  firmware_create_with_pins(pattern, interval, 11, 36, 38, 45, 31);
}
void firmware_run(unsigned ms) { run_until(sim::now_us + uint64_t(ms) * 1000); }
#ifdef USE_GTAG_BATTERY
void firmware_create_protected(int raw, int error, unsigned pattern) {
  create_display(pattern, 1000, 11, 36, 38, 45, 31, true, raw, error);
}
unsigned firmware_radio_starts() { return sim::radio_starts; }
unsigned firmware_reboots() { return sim::reboots; }
bool firmware_radio_ready() { return display->radio_ready(); }
void firmware_battery_indicator(bool enabled) { display->set_battery_indicator(enabled); }
#endif
void firmware_set_time(unsigned ms) { sim::now_us = uint64_t(ms) * 1000; }
void firmware_clock_wrap(unsigned subtract_ms) { sim::now_us += ((uint64_t(1) << 32) - subtract_ms) * 1000; }
#ifndef USE_GTAG_ZIGBEE
void firmware_connect() {
  assert(sim::advertising);
  sim::advertising = false;
  sim::connected = true;
  callbacks->connected(nullptr, 0);
}
void firmware_disconnect() {
  sim::connected = false;
  if (!(sim::adv_options & BT_LE_ADV_OPT_ONE_TIME)) sim::advertising = true;
  callbacks->disconnected(nullptr, 0x13);
}
void firmware_race_disconnect() { sim::on_disable = [] { firmware_disconnect(); }; }
void firmware_fail_advertising(unsigned count) { sim::adv_failures = count; }
int firmware_control(const uint8_t *data, uint16_t len) {
  return write_control(nullptr, nullptr, data, len, 0, 0);
}
int firmware_data(const uint8_t *data, uint16_t len) {
  return write_frame(nullptr, nullptr, data, len, 0, 0);
}
int firmware_led(const uint8_t *data, uint16_t len) {
  return write_led(nullptr, nullptr, data, len, 0, 0);
}
#else
int firmware_pattern(unsigned pattern) {
  return pattern <= 255 && display->request_test_pattern(static_cast<uint8_t>(pattern));
}
void firmware_race_pattern(unsigned pattern) {
  sim::on_disable = [pattern] { firmware_pattern(pattern); };
  display->pending_enable = true;
}
void firmware_packet(const uint8_t *data, unsigned len, uint8_t *reply) {
  display->process_zigbee_packet(data, len, reply);
}
bool firmware_expects_followup(const uint8_t *data, unsigned len, const uint8_t *reply) {
  return zigbee_frame::expects_followup(data, len, reply);
}
#ifdef USE_SENSOR
unsigned firmware_battery_publications() { return battery_sensor.publications; }
unsigned firmware_frames_publications() { return frames_sensor.publications; }
float firmware_battery_published() { return battery_sensor.value; }
float firmware_frames_published() { return frames_sensor.value; }
#endif
#endif
void firmware_status(uint8_t *out) { display->get_status(out); }
#ifndef USE_GTAG_ZIGBEE
int firmware_battery(uint8_t *out, uint16_t len, uint16_t offset) {
  return read_battery(nullptr, nullptr, out, len, offset);
}
#endif
unsigned firmware_battery_mv() { return display->battery_mv(); }
void firmware_adc_value(int raw, int error) { sim::adc_raw = raw; sim::adc_error = error; }
unsigned firmware_adc_reads() { return sim::adc_reads; }
#ifdef USE_GTAG_BATTERY
void firmware_calibration(float value) { display->set_battery_calibration(value); }
#endif
unsigned firmware_frames() { return display->frames(); }
unsigned firmware_loops() { return sim::loop_calls; }
unsigned firmware_advertising() { return sim::advertising; }
unsigned firmware_adv_attempts() { return sim::adv_attempts; }
unsigned firmware_adv_interval() { return sim::adv_interval; }
unsigned firmware_words() { return sim::words.size(); }
unsigned firmware_word(unsigned i) { return sim::words.at(i).value; }
uint64_t firmware_word_time(unsigned i) { return sim::words.at(i).time; }
uint64_t firmware_reset_low() { return sim::reset_low; }
uint64_t firmware_reset_high() { return sim::reset_high; }
}
