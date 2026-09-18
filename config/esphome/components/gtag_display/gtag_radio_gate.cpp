#include "esphome/core/defines.h"
#if defined(USE_GTAG_ZIGBEE) && defined(USE_GTAG_BATTERY)
#include "gtag_radio_gate.h"
// NCS 2.9.2 starts ZBOSS through zigbee_enable(). Gate thread creation,
// not an already running stack. Both calls run on the ESPHome main thread.
extern "C" {
void __real_zigbee_enable(void);
void __wrap_zigbee_enable(void) {
  // Do not create the ZBOSS radio thread until the first safe ADC sample.
  esphome::gtag_display::zigbee_radio_gate.request(__real_zigbee_enable);
}
}
#endif
