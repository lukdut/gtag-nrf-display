#include "gtag_zigbee.h"
#ifdef USE_GTAG_ZIGBEE
#include <cstring>
#include "esphome/core/log.h"
#include "gtag_zigbee_power.h"
extern "C" {
#include <zb_nrf_platform.h>
}

namespace esphome::gtag_display {
namespace {
const char *const TAG = "gtag_zigbee";
GTagZigbee *transport = nullptr;
}  // namespace

void GTagZigbee::setup() {
  transport = this;
  // The generated setup registers the endpoint context before App.setup().
  ZB_AF_SET_ENDPOINT_HANDLER(this->endpoint_, handle_packet_);
  // Joining resets the stack's long poll interval. Apply our value after every
  // join/reboot; the ESPHome callback runs in the main thread, not ZBOSS.
  this->parent_->add_on_join_callback([this](bool) { this->schedule_polling_(); });
#ifdef USE_GTAG_ZIGBEE_POWER_DIAGNOSTICS
  this->set_interval("power_snapshot", 1000, []() { zigbee_power::sample(); });
#endif
  this->disable_loop();
}

void GTagZigbee::schedule_polling_() {
  if (zigbee_schedule_callback(configure_polling_, 0) != RET_OK)
    this->set_timeout("zigbee_polling", 100, [this]() { this->schedule_polling_(); });
}

void GTagZigbee::configure_polling_(zb_uint8_t) {
  if (!zb_get_rx_on_when_idle()) {
    zb_zdo_pim_set_long_poll_interval(3000);  // API takes milliseconds.
    zb_zdo_pim_permit_turbo_poll(ZB_TRUE);
  }
}

zb_uint8_t GTagZigbee::handle_packet_(zb_bufid_t buffer) {
  const auto *header = ZB_BUF_GET_PARAM(buffer, zb_zcl_parsed_hdr_t);
  if (transport == nullptr || header->cluster_id != zigbee_frame::CLUSTER_ID ||
      header->profile_id != ZB_AF_HA_PROFILE_ID || header->is_common_command ||
      header->is_manuf_specific || header->cmd_direction != ZB_ZCL_FRAME_DIRECTION_TO_SRV ||
      header->cmd_id != zigbee_frame::COMMAND_PACKET)
    return ZB_FALSE;

  const auto &address = ZB_ZCL_PARSED_HDR_SHORT_DATA(header);
  if (address.source.addr_type != ZB_ZCL_ADDR_TYPE_SHORT) {
    zb_buf_free(buffer);
    return ZB_TRUE;
  }
  const Destination destination{address.source.u.short_addr, address.src_endpoint, header->seq_number};
  const auto *data = static_cast<const uint8_t *>(zb_buf_begin(buffer));
  const size_t len = zb_buf_len(buffer);
  // One ZCL OCTET_STRING: one length byte followed by the bounded packet.
  bool valid = len >= 2 && len <= zigbee_frame::MAX_PACKET_SIZE + 1 && data[0] == len - 1;
#ifdef USE_GTAG_ZIGBEE_POWER_DIAGNOSTICS
  if (valid && len == 2 && data[1] == zigbee_power::OPCODE) {
    uint8_t reply[zigbee_power::REPLY_SIZE];
    zigbee_power::make_reply(reply);
    send_reply_(buffer, destination, reply, sizeof(reply));
    return ZB_TRUE;
  }
#endif
  Phase expected = Phase::IDLE;
  if (!valid || !transport->phase_.compare_exchange_strong(expected, Phase::FILLING)) {
    uint8_t reply[zigbee_frame::REPLY_SIZE] = {};
    reply[0] = zigbee_frame::VERSION;
    reply[1] = len >= 2 ? data[1] : 0;
    reply[2] = uint8_t(valid ? zigbee_frame::Result::BUSY : zigbee_frame::Result::INVALID);
    send_reply_(buffer, destination, reply);
    return ZB_TRUE;
  }
  transport->destination_ = destination;
  transport->buffer_ = buffer;
  transport->packet_size_ = len - 1;
  std::memcpy(transport->packet_.data(), data + 1, len - 1);
  if (!zb_get_rx_on_when_idle()) {
    // Expect the next chunk/status request soon. ZBOSS starts near 100 ms and
    // backs off automatically if the sender disappears (15 s SDK timeout).
    // Refresh on each accepted packet, without keeping the receiver awake.
    zb_zdo_pim_start_turbo_poll_packets(2);
  }
  transport->phase_.store(Phase::READY);
  transport->enable_loop_soon_any_context();
  return ZB_TRUE;  // The retained buffer belongs to reply_callback_ now.
}

void GTagZigbee::loop() {
  this->disable_loop();
  if (this->phase_.load() == Phase::READY) {
    this->display_->process_zigbee_packet(this->packet_.data(), this->packet_size_, this->reply_.data());
    this->phase_.store(Phase::PROCESSED);
  }
  if (this->phase_.load() == Phase::PROCESSED) {
    this->phase_.store(Phase::QUEUED);
    // Nordic's wrapper is thread-safe; all buffer operations stay in ZBOSS.
    if (zigbee_schedule_callback(reply_callback_, this->buffer_) != RET_OK) {
      this->phase_.store(Phase::PROCESSED);
      this->set_timeout("zigbee_reply", 10, [this]() { this->enable_loop_soon_any_context(); });
    }
  }
}

void GTagZigbee::reply_callback_(zb_uint8_t buffer) {
  send_reply_(buffer, transport->destination_, transport->reply_.data());
  transport->phase_.store(Phase::IDLE);
}

void GTagZigbee::send_reply_(zb_bufid_t buffer, const Destination &destination, const uint8_t *reply, size_t size) {
  auto *out = static_cast<uint8_t *>(ZB_ZCL_START_PACKET(buffer));
  ZB_ZCL_CONSTRUCT_SPECIFIC_COMMAND_RES_FRAME_CONTROL(out);
  ZB_ZCL_CONSTRUCT_COMMAND_HEADER(out, destination.sequence, zigbee_frame::COMMAND_REPLY);
  *out++ = size;
  std::memcpy(out, reply, size);
  out += size;
  zb_addr_u address{};
  address.addr_short = destination.address;
  const auto result = zb_zcl_finish_and_send_packet(buffer, out, &address,
      ZB_APS_ADDR_MODE_16_ENDP_PRESENT, destination.endpoint, transport->endpoint_,
      ZB_AF_HA_PROFILE_ID, zigbee_frame::CLUSTER_ID, nullptr);
  // Successful sends are owned by ZBOSS; an immediate failure still owns buf.
  if (result != RET_OK) {
    zb_buf_free(buffer);
    ESP_LOGW(TAG, "Reply send failed: %d", int(result));
  }
}

void GTagZigbee::dump_config() {
  ESP_LOGCONFIG(TAG, "GTag frames: endpoint=%u cluster=0xFC11, chunks=32 bytes, protocol=1", this->endpoint_);
}
}  // namespace esphome::gtag_display
#endif
