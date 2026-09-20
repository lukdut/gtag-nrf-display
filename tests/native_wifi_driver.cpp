#include "native_wifi_stubs.h"
#include <memory>
#include "gtag_display_esp32.cpp"
using esphome::gtag_display::GTagDisplay;
std::unique_ptr<GTagDisplay> lcd;
std::array<esphome::GPIOPin, 4> pins{{esphome::GPIOPin(0), esphome::GPIOPin(1), esphome::GPIOPin(2), esphome::GPIOPin(3)}};
extern "C" {
void wifi_decoder(wifi_sim::Decode decode) { wifi_sim::decode = decode; }
void wifi_create() {
  wifi_sim::now_us = 0; wifi_sim::words.clear();
  std::fill(std::begin(wifi_sim::levels), std::end(wifi_sim::levels), false);
  wifi_sim::bits = 0;
  lcd = std::make_unique<GTagDisplay>();
  lcd->set_dio_pin(&pins[0]); lcd->set_clk_pin(&pins[1]); lcd->set_cs_pin(&pins[2]); lcd->set_reset_pin(&pins[3]);
  lcd->set_boot_pattern(esphome::gtag_display::BootPattern::NONE);
  lcd->setup();
}
unsigned wifi_run(unsigned ticks) {
  uint64_t max_us = 0;
  for (unsigned i = 0; i < ticks; ++i) {
    wifi_sim::now_us += 16000;
    const auto before = wifi_sim::now_us;
    lcd->loop();
    max_us = std::max(max_us, wifi_sim::now_us - before);
  }
  return max_us;
}
bool wifi_submit(const char *payload, int version, int codec, const char *id, const char *crc, int timeout) {
  return lcd->submit(payload, version, codec, id, crc, timeout);
}
bool wifi_confirm(const char *id, const char *crc, const char *sequence) { return lcd->confirm(id, crc, sequence); }
bool wifi_rendered(const char *id, const char *crc) { return lcd->rendered(id, crc); }
unsigned wifi_word_count() { return wifi_sim::words.size(); }
unsigned wifi_word(unsigned i) { return wifi_sim::words.at(i); }
const char *wifi_error() { return lcd->error().c_str(); }
}
