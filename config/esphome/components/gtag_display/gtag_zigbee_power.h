#pragma once
#include "esphome/core/defines.h"
#if defined(USE_GTAG_ZIGBEE) && defined(USE_GTAG_ZIGBEE_POWER_DIAGNOSTICS)
#include <cstddef>
#include <cstdint>

namespace esphome::gtag_display::zigbee_power {
constexpr uint8_t OPCODE = 5;
constexpr size_t REPLY_SIZE = 40;
void sample();  // Main thread; approximate once-per-second hardware samples.
void make_reply(uint8_t *reply);  // ZBOSS thread, only on explicit request.
}  // namespace esphome::gtag_display::zigbee_power
#endif
