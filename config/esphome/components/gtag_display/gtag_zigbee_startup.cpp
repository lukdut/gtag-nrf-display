#include "esphome/core/defines.h"
#ifdef USE_GTAG_ZIGBEE
extern "C" {
#include <zigbee/zigbee_app_utils.h>

zb_ret_t __real_zigbee_default_signal_handler(zb_bufid_t buffer);

zb_ret_t __wrap_zigbee_default_signal_handler(zb_bufid_t buffer) {
  // NCS 2.9.2 restores RX-on-when-idle from the common NVRAM dataset after
  // ESPHome's setup() has configured sleepy behavior. SKIP_STARTUP is emitted
  // after that restore, before the default handler starts BDB initialization.
  // Reapply the YAML choice here so the MAC and join/rejoin capabilities use
  // it, without erasing pairing, keys, counters, or reporting configuration.
  const auto signal = zb_get_app_signal(buffer, nullptr);
  if (signal == ZB_ZDO_SIGNAL_SKIP_STARTUP && ZB_GET_APP_SIGNAL_STATUS(buffer) == RET_OK)
    zb_set_rx_on_when_idle(GTAG_ZIGBEE_SLEEPY ? ZB_FALSE : ZB_TRUE);

  // Preserve normal commissioning, CAN_SLEEP processing, and error handling.
  // Buffer ownership stays with ESPHome's original signal handler.
  return __real_zigbee_default_signal_handler(buffer);
}
}
#endif
