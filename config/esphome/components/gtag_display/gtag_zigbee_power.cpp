#include "gtag_zigbee_power.h"
#if defined(USE_GTAG_ZIGBEE) && defined(USE_GTAG_ZIGBEE_POWER_DIAGNOSTICS)
#include <atomic>
#include <cstring>
#include <soc.h>
#include <zephyr/kernel.h>
extern "C" {
#include <zboss_api.h>
}

namespace esphome::gtag_display::zigbee_power {
namespace {
uint32_t sleep_ms = 0;
uint32_t sleep_calls = 0;
std::atomic<uint32_t> samples{0}, radio_off{0}, hfclk_xtal{0};

void put_u32(uint8_t *out, uint32_t value) {
  for (unsigned i = 0; i != 4; i++) out[i] = value >> (i * 8);
}

void thread_stats(const k_thread *thread, void *context) {
  const char *name = k_thread_name_get(const_cast<k_thread *>(thread));
  if (name == nullptr) return;
  unsigned offset;
  if (std::strcmp(name, "main") == 0) offset = 20;
  else if (std::strcmp(name, "zboss") == 0) offset = 24;
  else return;
  k_thread_runtime_stats_t stats{};
  if (k_thread_runtime_stats_get(const_cast<k_thread *>(thread), &stats) == 0)
    put_u32(static_cast<uint8_t *>(context) + offset, k_cyc_to_ms_floor64(stats.execution_cycles));
}
}  // namespace

void sample() {
  samples.fetch_add(1, std::memory_order_relaxed);
  if (NRF_RADIO->STATE == 0) radio_off.fetch_add(1, std::memory_order_relaxed);
  if ((NRF_CLOCK->HFCLKSTAT & CLOCK_HFCLKSTAT_SRC_Msk) ==
      (CLOCK_HFCLKSTAT_SRC_Xtal << CLOCK_HFCLKSTAT_SRC_Pos))
    hfclk_xtal.fetch_add(1, std::memory_order_relaxed);
}

void make_reply(uint8_t *reply) {
  std::memset(reply, 0, REPLY_SIZE);
  reply[0] = 1;
  reply[1] = OPCODE;
  reply[3] = (zb_get_rx_on_when_idle() ? 1 : 0) | (zb_zdo_joined() ? 2 : 0);
  put_u32(reply + 4, k_uptime_get_32());
  put_u32(reply + 8, sleep_ms);
  put_u32(reply + 12, sleep_calls);
  k_thread_runtime_stats_t stats{};
  if (k_thread_runtime_stats_all_get(&stats) == 0)
    put_u32(reply + 16, k_cyc_to_ms_floor64(stats.idle_cycles));
  k_thread_foreach_unlocked(thread_stats, reply);
  put_u32(reply + 28, samples.load(std::memory_order_relaxed));
  put_u32(reply + 32, radio_off.load(std::memory_order_relaxed));
  put_u32(reply + 36, hfclk_xtal.load(std::memory_order_relaxed));
}
}  // namespace esphome::gtag_display::zigbee_power

extern "C" uint32_t __real_zb_osif_sleep(uint32_t timeout);
extern "C" uint32_t __wrap_zb_osif_sleep(uint32_t timeout) {
  // Call the SDK implementation unchanged, including its time-unit rounding.
  // Account elapsed wall time, not its rounded return value. Both writer and
  // diagnostics reader run in the ZBOSS thread.
  const uint32_t before = k_uptime_get_32();
  const uint32_t result = __real_zb_osif_sleep(timeout);
  esphome::gtag_display::zigbee_power::sleep_ms += k_uptime_get_32() - before;
  esphome::gtag_display::zigbee_power::sleep_calls++;
  return result;
}
#endif
