#pragma once
#include <cstdint>

namespace esphome::gtag_display::battery_guard {

enum class State : uint8_t { WAITING, LOW, RUNNING, REBOOT };

// Start conservatively: every boot requires the recovery voltage before radio.
// A runtime undervoltage requests one reboot into the same radio-gated boot.
// No flash latch is needed. The recovery margin suppresses rebound restarts.
class Guard {
 public:
  void configure(uint16_t cutoff, uint16_t recovery) {
    cutoff_ = cutoff;
    recovery_ = recovery;
    state_ = State::WAITING;
  }
  State update(uint16_t mv) {
    if (mv == 0xFFFF || state_ == State::REBOOT) return state_;
    if (state_ == State::RUNNING) {
      if (mv < cutoff_) state_ = State::REBOOT;
    } else {
      state_ = mv >= recovery_ ? State::RUNNING : State::LOW;
    }
    return state_;
  }
  State state() const { return state_; }

 private:
  uint16_t cutoff_{3306}, recovery_{3450};
  State state_{State::WAITING};
};

class RadioStartGate {
 public:
  void request(void (*start)()) { start_ = start; maybe_start_(); }
  void permit() { permitted_ = true; maybe_start_(); }
 private:
  void maybe_start_() {
    if (permitted_ && start_ != nullptr && !started_) {
      started_ = true;
      start_();
    }
  }
  void (*start_)() = nullptr;
  bool permitted_{false}, started_{false};
};

}  // namespace esphome::gtag_display::battery_guard
