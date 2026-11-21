#pragma once

#include "lora_profiles.h"
#include "vfence_protocol.h"

namespace vfence {

constexpr uint8_t PROFILE_CONTROL = 0x05;
constexpr uint8_t PROFILE_WIRE_VERSION = 2;
constexpr uint8_t PROFILE_MAX_MEMBERS = 16;
constexpr uint32_t PROFILE_LEASE_MS = 120000;

enum class ProfileStep : uint8_t { PREPARE = 1, READY, ARM, ARMED, POLL, VERIFIED, RECOVER, RESTORED };
enum class ProfilePhase : uint8_t { IDLE, PREPARING, COMMITTING, VERIFYING, COMPLETED, ROLLING_BACK, FAILED };

inline const char* profilePhaseName(ProfilePhase phase) {
  const char* names[] = {"IDLE", "PREPARING", "COMMITTING", "VERIFYING", "COMPLETED", "ROLLING_BACK", "FAILED"};
  return names[static_cast<uint8_t>(phase)];
}

struct ProfileSignal {
  ProfileStep step;
  uint64_t gateway;
  uint32_t operation;
  uint64_t collarBoot;
  uint16_t waitMs;
  uint8_t source;
  uint8_t target;
};

inline size_t encodeProfileSignal(uint8_t* out, size_t capacity, uint8_t collar,
                                 uint16_t sequence, const ProfileSignal& signal) {
  constexpr uint8_t PAYLOAD = 27;
  const Header header{PROTOCOL_VERSION, MessageType::CONTROL, false, false, false, collar, sequence, PAYLOAD};
  if (capacity < HEADER_SIZE + PAYLOAD || !encodeHeader(out, capacity, header)) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = PROFILE_CONTROL;
  p[1] = PROFILE_WIRE_VERSION;
  p[2] = static_cast<uint8_t>(signal.step);
  p[3] = signal.source;
  p[4] = signal.target;
  writeU32BE(p + 5, signal.operation);
  writeU32BE(p + 9, static_cast<uint32_t>(signal.gateway >> 32));
  writeU32BE(p + 13, static_cast<uint32_t>(signal.gateway));
  writeU32BE(p + 17, static_cast<uint32_t>(signal.collarBoot >> 32));
  writeU32BE(p + 21, static_cast<uint32_t>(signal.collarBoot));
  writeU16BE(p + 25, signal.waitMs);
  return HEADER_SIZE + PAYLOAD;
}

inline bool decodeProfileSignal(const PacketView& packet, ProfileSignal& out) {
  const auto* p = packet.payload;
  if (packet.header.type != MessageType::CONTROL || packet.header.ackReq ||
      packet.payloadLength != 27 || p[0] != PROFILE_CONTROL || p[1] != PROFILE_WIRE_VERSION ||
      p[2] < 1 || p[2] > 8 || p[3] >= LORA_PROFILE_COUNT || p[4] >= LORA_PROFILE_COUNT) return false;
  out = {static_cast<ProfileStep>(p[2]), (uint64_t(readU32BE(p + 9)) << 32) | readU32BE(p + 13),
         readU32BE(p + 5), (uint64_t(readU32BE(p + 17)) << 32) | readU32BE(p + 21),
         readU16BE(p + 25), p[3], p[4]};
  return out.operation != 0;
}

inline bool profileDue(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

// Every usable profile has an explicit header. receive() selects explicit RX;
// the sketches transmit using beginPacket() with its explicit default.
template <class Radio>
bool applyLoRaProfile(Radio& radio, uint8_t profile) {
  if (profile >= LORA_PROFILE_COUNT || !LORA_PROFILES[profile].explicitHeader) return false;
  const auto& p = LORA_PROFILES[profile];
  radio.idle();
  radio.setFrequency(p.frequencyHz);
  radio.setSignalBandwidth(p.bandwidthHz);
  radio.setSpreadingFactor(p.spreadingFactor);
  radio.setCodingRate4(p.codingRateDenominator);
  radio.setTxPower(p.txPowerDbm);
  radio.setPreambleLength(p.preambleSymbols);
  radio.setSyncWord(p.syncWord);
  if (p.crcEnabled) radio.enableCrc(); else radio.disableCrc();
  if (p.invertedIq) radio.enableInvertIQ(); else radio.disableInvertIQ();
  radio.receive();
  return true;
}

class CollarProfileTransition {
 public:
  uint8_t active = INITIAL_LORA_PROFILE;

  void recover(const ProfileSignal& recorded) {
    pending_ = recorded;
    active = INITIAL_LORA_PROFILE;
    prepared_ = armed_ = false;
  }

  template <class Save, class Reply>
  void receive(const ProfileSignal& input, uint64_t boot, uint32_t now, bool radioBusy,
               Save save, Reply reply) {
    auto response = input;
    response.collarBoot = boot;
    if (input.step == ProfileStep::RECOVER && active == INITIAL_LORA_PROFILE) {
      // A rebooted gateway changes its session while recovering the same operation.
      // Never report P0 while an unrelated switch is still prepared or armed.
      if (prepared_ && (input.operation != pending_.operation || input.source != pending_.source ||
                        input.target != pending_.target)) return;
      prepared_ = armed_ = false;
      response.step = ProfileStep::RESTORED;
    } else if (input.step == ProfileStep::PREPARE && !radioBusy && input.source == active && input.target != active &&
               (matches(input) || (!armed_ && (input.gateway != pending_.gateway ||
                                               input.operation > pending_.operation)))) {
      if (!matches(input)) {
        if (!save(input)) return;
        pending_ = input;
        prepared_ = true;
        preparedUntil_ = now + PROFILE_LEASE_MS;
      }
      if (active) leaseUntil_ = now + PROFILE_LEASE_MS;
      response.step = ProfileStep::READY;
    } else if (input.step == ProfileStep::ARM && !radioBusy && matches(input) && active == input.source &&
               input.collarBoot == boot && input.waitMs >= 1000 && input.waitMs <= 60000) {
      if (!save(input)) return;
      switchAt_ = now + input.waitMs;
      armed_ = true;
      response.step = ProfileStep::ARMED;
    } else if (input.step == ProfileStep::POLL && matches(input) && active == input.target &&
               input.collarBoot == boot) {
      if (active) leaseUntil_ = now + PROFILE_LEASE_MS;
      response.step = ProfileStep::VERIFIED;
    } else {
      return;
    }
    reply(response);
  }

  template <class Apply>
  void service(uint32_t now, bool radioBusy, Apply apply) {
    if (armed_ && profileDue(now, switchAt_)) {
      if (!radioBusy && apply(pending_.target)) {
        active = pending_.target;
        if (active) leaseUntil_ = now + PROFILE_LEASE_MS;
      }
      armed_ = false;
    }
    if (active && !radioBusy && profileDue(now, leaseUntil_) && apply(INITIAL_LORA_PROFILE)) {
      active = INITIAL_LORA_PROFILE;
      prepared_ = armed_ = false;
    }
    if (!active && prepared_ && !armed_ && profileDue(now, preparedUntil_)) prepared_ = false;
  }

 private:
  ProfileSignal pending_{};
  uint32_t switchAt_ = 0, leaseUntil_ = 0, preparedUntil_ = 0;
  bool prepared_ = false, armed_ = false;

  bool matches(const ProfileSignal& signal) const {
    return prepared_ && pending_.gateway == signal.gateway && pending_.operation == signal.operation &&
           pending_.source == signal.source && pending_.target == signal.target;
  }
};

class GatewayProfileTransition {
 public:
  struct Member { uint64_t boot; uint32_t proofTime; uint8_t id; bool proved; };
  Member members[PROFILE_MAX_MEMBERS]{};
  uint8_t count = 0, active = INITIAL_LORA_PROFILE, source = INITIAL_LORA_PROFILE, target = INITIAL_LORA_PROFILE;
  uint32_t operation = 0, deadline = 0;
  uint64_t session = 0;
  ProfilePhase phase = ProfilePhase::IDLE;

  bool busy() const {
    return phase == ProfilePhase::PREPARING || phase == ProfilePhase::COMMITTING ||
           phase == ProfilePhase::VERIFYING || phase == ProfilePhase::ROLLING_BACK;
  }

  bool recoveryOverdue() const { return phase == ProfilePhase::ROLLING_BACK && recoveryOverdue_; }

  bool start(uint64_t boot, uint32_t id, uint8_t next, const uint8_t* roster, uint8_t size, uint32_t now) {
    if (busy() || !id || id <= operation || !size || size > PROFILE_MAX_MEMBERS || next == active ||
        next >= LORA_PROFILE_COUNT || (active && next != INITIAL_LORA_PROFILE)) return false;
    if (active && size != count) return false;
    for (uint8_t i = 0; i < size; ++i) {
      if (!roster[i] || roster[i] == 255 || (active && roster[i] != members[i].id)) return false;
      for (uint8_t j = 0; j < i; ++j) if (roster[i] == roster[j]) return false;
    }
    count = size;
    for (uint8_t i = 0; i < count; ++i) members[i] = {0, now, roster[i], false};
    source = active;
    target = next;
    operation = id;
    session = boot;
    enter(ProfilePhase::PREPARING, now, 55000);
    return true;
  }

  void receive(uint8_t id, const ProfileSignal& signal, uint32_t now) {
    if (signal.gateway != session || signal.operation != operation ||
        signal.source != source || signal.target != target) return;
    for (uint8_t i = 0; i < count; ++i) {
      auto& member = members[i];
      if (member.id != id) continue;
      const bool ready = phase == ProfilePhase::PREPARING && signal.step == ProfileStep::READY;
      const bool armed = phase == ProfilePhase::COMMITTING && signal.step == ProfileStep::ARMED;
      const bool verified = (phase == ProfilePhase::VERIFYING || phase == ProfilePhase::COMPLETED) &&
                            signal.step == ProfileStep::VERIFIED;
      const bool restored = phase == ProfilePhase::ROLLING_BACK && recoveryApplied_ &&
                            signal.step == ProfileStep::RESTORED;
      if (ready || restored) member.boot = signal.collarBoot;
      if (ready || restored || ((armed || verified) && member.boot == signal.collarBoot)) {
        member.proved = true;
        member.proofTime = now;
      }
      return;
    }
  }

  bool cancel(uint32_t now) {
    if (phase != ProfilePhase::PREPARING) return false;
    if (!active) enter(ProfilePhase::FAILED, now, 0);
    else enter(ProfilePhase::ROLLING_BACK, now, PROFILE_LEASE_MS + 55000);
    return true;
  }

  template <class Send, class Apply>
  void service(uint32_t now, bool radioBusy, Send send, Apply apply) {
    if (phase == ProfilePhase::PREPARING) {
      if (allProved()) enter(ProfilePhase::COMMITTING, now, 5000 + count * 2900);
      else if (profileDue(now, deadline)) cancel(now);
    } else if (phase == ProfilePhase::COMMITTING && profileDue(now, deadline)) {
      if (allProved() && !radioBusy && apply(target)) {
        active = target;
        enter(ProfilePhase::VERIFYING, now, 55000);
      } else {
        enter(ProfilePhase::ROLLING_BACK, now, PROFILE_LEASE_MS + 55000);
      }
    } else if (phase == ProfilePhase::VERIFYING) {
      if (allProved()) phase = ProfilePhase::COMPLETED;
      else if (profileDue(now, deadline)) enter(ProfilePhase::ROLLING_BACK, now, PROFILE_LEASE_MS + 55000);
    } else if (phase == ProfilePhase::COMPLETED && active) {
      for (uint8_t i = 0; i < count; ++i)
        if (now - members[i].proofTime >= PROFILE_LEASE_MS / 2) {
          enter(ProfilePhase::ROLLING_BACK, now, PROFILE_LEASE_MS + 55000);
          break;
        }
    }
    if (phase == ProfilePhase::ROLLING_BACK) {
      // Expiry is diagnostic. Retries continue until local application and all
      // participant proofs are available, even after the initial deadline.
      if (profileDue(now, deadline)) recoveryOverdue_ = true;
      if (radioBusy) nextSend_ = now;
      if (!recoveryApplied_) {
        if (radioBusy || !profileDue(now, nextSend_)) return;
        if (!apply(INITIAL_LORA_PROFILE)) {
          nextSend_ = now + 1000;
          return;
        }
        active = INITIAL_LORA_PROFILE;
        recoveryApplied_ = true;
      }
      if (allProved()) {
        phase = ProfilePhase::FAILED;
        return;
      }
    }
    if (radioBusy || !profileDue(now, nextSend_) ||
        !(busy() || (phase == ProfilePhase::COMPLETED && active))) return;
    nextSend_ = now + 1000;
    for (uint8_t n = 0; n < count; ++n) {
      auto& member = members[next_];
      next_ = (next_ + 1) % count;
      if (phase != ProfilePhase::COMPLETED && member.proved) continue;
      ProfileStep step = ProfileStep::POLL;
      uint16_t wait = 0;
      if (phase == ProfilePhase::PREPARING) step = ProfileStep::PREPARE;
      if (phase == ProfilePhase::ROLLING_BACK) step = ProfileStep::RECOVER;
      if (phase == ProfilePhase::COMMITTING) {
        if (deadline - now < 1800) return;
        step = ProfileStep::ARM;
        wait = static_cast<uint16_t>(deadline - now);
      }
      send(member.id, {step, session, operation, member.boot, wait, source, target});
      return;
    }
  }

  void recover(uint32_t now) {
    if (count && operation) enter(ProfilePhase::ROLLING_BACK, now, PROFILE_LEASE_MS + 55000);
  }

 private:
  uint32_t nextSend_ = 0;
  uint8_t next_ = 0;
  bool recoveryApplied_ = false;
  bool recoveryOverdue_ = false;

  bool allProved() const {
    for (uint8_t i = 0; i < count; ++i) if (!members[i].proved) return false;
    return count != 0;
  }
  void enter(ProfilePhase state, uint32_t now, uint32_t duration) {
    phase = state;
    deadline = now + duration;
    nextSend_ = now;
    next_ = 0;
    recoveryApplied_ = recoveryOverdue_ = false;
    for (uint8_t i = 0; i < count; ++i) members[i].proved = false;
  }
};

}  // namespace vfence
