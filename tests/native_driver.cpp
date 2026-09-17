#include "native_stubs.h"
#include <memory>
#include "../config/esphome/components/gtag_display/gtag_display.cpp"

using namespace esphome::gtag_display;
class TestDisplay : public GTagDisplay {
 public:
  unsigned frames() const { return frames_; }
};
static std::unique_ptr<TestDisplay> display;

static void run_until(uint64_t end_us) {
  for (unsigned steps = 0; steps < 1000; ++steps) {
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
void firmware_create(unsigned pattern, unsigned interval) {
  display.reset();
  sim::now_us = sim::loop_calls = sim::adv_attempts = sim::adv_failures = 0;
  sim::advertising = sim::connected = false;
  sim::reset_low = sim::reset_high = sim::bit_count = sim::word = 0;
  sim::words.clear();
  std::memset(sim::levels, 0, sizeof(sim::levels));
  sim::on_disable = {};
  display = std::make_unique<TestDisplay>();
  display->set_boot_pattern(static_cast<BootPattern>(pattern));
  display->set_advertising_interval(interval);
  display->setup();
}
void firmware_run(unsigned ms) { run_until(sim::now_us + uint64_t(ms) * 1000); }
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
void firmware_status(uint8_t *out) { display->get_status(out); }
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
