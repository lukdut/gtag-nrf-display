#include "esphome/core/defines.h"
#ifdef USE_GTAG_ZIGBEE
extern "C" {
#include <zigbee/zigbee_app_utils.h>

zb_ret_t __real_zigbee_default_signal_handler(zb_bufid_t buffer);

#if defined(CONFIG_ZIGBEE_ROLE_END_DEVICE)
static bool gtag_stack_initialized = false;
static bool gtag_rejoin_check_scheduled = false;
static void gtag_check_rejoin(zb_uint8_t unused);

static void gtag_update_rejoin_check() {
  // Only recover a previously paired device. A factory reset/removal must not
  // turn into automatic commissioning of a new network.
  const bool needed = gtag_stack_initialized && !zb_bdb_is_factory_new() && !zb_zdo_joined();
  if (!needed && gtag_rejoin_check_scheduled) {
    ZB_SCHEDULE_APP_ALARM_CANCEL(gtag_check_rejoin, ZB_ALARM_ANY_PARAM);
    gtag_rejoin_check_scheduled = false;
  } else if (needed && !gtag_rejoin_check_scheduled) {
    // One ZBOSS alarm while disconnected; no recurring wakeup while joined.
    gtag_rejoin_check_scheduled =
        ZB_SCHEDULE_APP_ALARM(gtag_check_rejoin, 0, ZB_MILLISECONDS_TO_BEACON_INTERVAL(60000)) == RET_OK;
  }
}

static void gtag_check_rejoin(zb_uint8_t) {
  gtag_rejoin_check_scheduled = false;
  if (!zb_bdb_is_factory_new() && !zb_zdo_joined()) {
    // NCS 2.9.2 stops an End Device's rejoin procedure after 200 seconds and
    // waits for user_input_indicate(). Resume through that public API; it is
    // a no-op during an active attempt and preserves the saved network.
    user_input_indicate();
  }
  gtag_update_rejoin_check();
}
#endif

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
  const auto result = __real_zigbee_default_signal_handler(buffer);
#if defined(CONFIG_ZIGBEE_ROLE_END_DEVICE)
  if (signal == ZB_ZDO_SIGNAL_SKIP_STARTUP && ZB_GET_APP_SIGNAL_STATUS(buffer) == RET_OK)
    gtag_stack_initialized = true;
  gtag_update_rejoin_check();
#endif
  return result;
}
}
#endif
