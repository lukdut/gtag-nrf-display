#pragma once

#include "frame_protocol.h"
#include "zigbee_protocol.h"

namespace esphome::gtag_display::zigbee_frame {

// Use the main thread's reply snapshot, never the live LCD/receiver state in
// the ZBOSS thread. The sender performs one request/reply at a time.
inline bool expects_followup(const uint8_t *packet, size_t len, const uint8_t reply[REPLY_SIZE]) {
  if (len < 5 || reply[0] != VERSION || reply[1] != packet[0] ||
      reply[2] != uint8_t(Result::OK) || reply[14] != uint8_t(frame::Error::NONE))
    return false;
  const auto command = static_cast<Command>(packet[0]);
  if (command == Command::BEGIN)
    return (len == 13 || len == 17) && read32(packet + 5) == read32(reply + 3);
  if (read32(packet + 1) != read32(reply + 3)) return false;
  if (command == Command::DATA) return len >= 8 && len <= MAX_PACKET_SIZE;
  if ((command != Command::COMMIT && command != Command::STATUS) || len != 5) return false;
  const auto state = static_cast<frame::State>(reply[7]);
  if (state == frame::State::RECEIVING) return command == Command::STATUS;
  return state == frame::State::COMPLETE &&
      ((reply[15] & FLAG_PENDING) || !(reply[15] & FLAG_RENDERED) || read32(reply + 16) != read32(reply + 3));
}

}  // namespace esphome::gtag_display::zigbee_frame
