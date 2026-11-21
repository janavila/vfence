#pragma once

#include <stddef.h>
#include "lora_profiles.h"

// Host-only model. The acknowledgements here are evidence supplied by the
// simulator, not messages implemented on the radio.
class ProfileTransitionModel {
 public:
  enum class State { IDLE, PREPARING, COMMITTING, VERIFYING, COMPLETED, ROLLING_BACK, FAILED };
  enum class Proof { READY_OLD, ARMED_OLD, VERIFIED_TARGET, RESTORED_OLD };

  struct Member {
    uint8_t id;
    uint64_t boot;
    bool online;
  };

  struct Ticket {
    uint64_t gatewayBoot;
    uint32_t operation;
  };

  bool start(Ticket ticket, uint8_t target, const Member* roster, size_t count, bool radioBusy) {
    if (state_ != State::IDLE || !ticket.operation || radioBusy || target == active_ ||
        target >= vfence::LORA_PROFILE_COUNT || !count || count > 254) return false;
    bool included[255]{};
    for (size_t i = 0; i < count; ++i) {
      const auto& member = roster[i];
      if (!member.id || member.id == 255 || !member.online || included[member.id]) return false;
      included[member.id] = true;
    }
    for (size_t i = 0; i < count; ++i) {
      const auto& member = roster[i];
      members_[member.id] = {member.boot, true, false};
    }
    count_ = count;
    ticket_ = ticket;
    target_ = target;
    previous_ = active_;
    enter(State::PREPARING);
    return true;
  }

  bool confirm(Ticket ticket, uint8_t id, uint64_t collarBoot, Proof proof, uint8_t seenProfile) {
    if (ticket.gatewayBoot != ticket_.gatewayBoot || ticket.operation != ticket_.operation ||
        !id || id == 255 || !members_[id].included || members_[id].boot != collarBoot) return false;
    const bool expected =
        (state_ == State::PREPARING && proof == Proof::READY_OLD && seenProfile == previous_) ||
        (state_ == State::COMMITTING && proof == Proof::ARMED_OLD && seenProfile == previous_) ||
        (state_ == State::VERIFYING && proof == Proof::VERIFIED_TARGET && seenProfile == target_) ||
        (state_ == State::ROLLING_BACK && proof == Proof::RESTORED_OLD && seenProfile == previous_);
    if (!expected || members_[id].confirmed) return false;
    members_[id].confirmed = true;
    ++confirmed_;
    if (confirmed_ == count_) {
      if (state_ == State::PREPARING) enter(State::COMMITTING);
      else if (state_ == State::VERIFYING) {
        active_ = target_;
        enter(State::COMPLETED);
      } else if (state_ == State::ROLLING_BACK) {
        active_ = previous_;
        enter(State::FAILED);
      }
    }
    return true;
  }

  bool startTrial() {
    if (state_ != State::COMMITTING || confirmed_ != count_) return false;
    enter(State::VERIFYING);
    return true;
  }

  void timeout() {
    if (state_ == State::PREPARING) enter(State::FAILED);
    if (state_ == State::COMMITTING || state_ == State::VERIFYING) enter(State::ROLLING_BACK);
    else if (state_ == State::ROLLING_BACK) rollbackUnverified_ = true;
  }

  // Simulates restoring a journal and reacquiring members on the P0 rendezvous.
  void recoverAtBoot(uint64_t gatewayBoot) {
    if (state_ != State::PREPARING && state_ != State::COMMITTING && state_ != State::VERIFYING &&
        state_ != State::ROLLING_BACK) return;
    ticket_.gatewayBoot = gatewayBoot;
    previous_ = vfence::INITIAL_LORA_PROFILE;
    active_ = previous_;
    enter(State::ROLLING_BACK);
  }

  bool reacquire(uint8_t id, uint64_t rebootedCollar, uint8_t seenProfile) {
    if (state_ != State::ROLLING_BACK || !id || id == 255 || !members_[id].included ||
        seenProfile != previous_) return false;
    if (members_[id].confirmed) {
      members_[id].confirmed = false;
      --confirmed_;
    }
    members_[id].boot = rebootedCollar;
    return true;
  }

  State state() const { return state_; }
  uint8_t active() const { return active_; }
  bool rollbackUnverified() const { return rollbackUnverified_; }

 private:
  struct Evidence { uint64_t boot; bool included; bool confirmed; };
  Evidence members_[255]{};
  Ticket ticket_{};
  size_t count_ = 0;
  size_t confirmed_ = 0;
  uint8_t active_ = vfence::INITIAL_LORA_PROFILE;
  uint8_t previous_ = vfence::INITIAL_LORA_PROFILE;
  uint8_t target_ = vfence::INITIAL_LORA_PROFILE;
  bool rollbackUnverified_ = false;
  State state_ = State::IDLE;

  void enter(State next) {
    state_ = next;
    confirmed_ = 0;
    for (auto& member : members_) member.confirmed = false;
  }
};
