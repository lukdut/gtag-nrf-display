#pragma once
#include "esphome/core/defines.h"
#ifdef USE_GTAG_ZIGBEE
#include "gtag_display.h"
#include "esphome/components/zigbee/zigbee_zephyr.h"

namespace esphome::gtag_display {

class GTagZigbee : public Component {
 public:
  void set_display(GTagDisplay *display) { display_ = display; }
  void set_endpoint(uint8_t endpoint) { endpoint_ = endpoint; }
  void set_parent(zigbee::ZigbeeComponent *parent) { parent_ = parent; }
  void setup() override;
  void loop() override;
  void dump_config() override;

 protected:
  enum class Phase : uint8_t { IDLE, FILLING, READY, PROCESSED, QUEUED };
  struct Destination {
    uint16_t address;
    uint8_t endpoint;
    uint8_t sequence;
  };
  static zb_uint8_t handle_packet_(zb_bufid_t buffer);
  static void reply_callback_(zb_uint8_t buffer);
  static void send_reply_(zb_bufid_t buffer, const Destination &destination, const uint8_t *reply,
                          size_t size = zigbee_frame::REPLY_SIZE);
  void schedule_polling_();
  static void configure_polling_(zb_uint8_t unused);

  GTagDisplay *display_{nullptr};
  zigbee::ZigbeeComponent *parent_{nullptr};
  uint8_t endpoint_{0};
  // ZBOSS produces one request, main loop processes it, ZBOSS sends the reply.
  // Keeping the buffer until reply avoids unbounded allocations/queues.
  std::atomic<Phase> phase_{Phase::IDLE};
  std::array<uint8_t, zigbee_frame::MAX_PACKET_SIZE> packet_{};
  std::array<uint8_t, zigbee_frame::REPLY_SIZE> reply_{};
  size_t packet_size_{0};
  zb_bufid_t buffer_{0};
  Destination destination_{};
};

}  // namespace esphome::gtag_display
#endif
