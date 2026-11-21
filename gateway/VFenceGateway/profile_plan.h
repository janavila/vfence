#pragma once

#include "lora_profiles.h"

namespace vfence {

enum class ProfileDecision : uint8_t {
  PLANNED, CANCELLED, DUPLICATE, UNCHANGED, INVALID, WRONG_SESSION, STALE, BUSY,
};

inline const char* profileDecisionName(ProfileDecision decision) {
  switch (decision) {
    case ProfileDecision::PLANNED: return "plan_registered";
    case ProfileDecision::CANCELLED: return "plan_cancelled";
    case ProfileDecision::DUPLICATE: return "duplicate_operation";
    case ProfileDecision::UNCHANGED: return "already_active";
    case ProfileDecision::INVALID: return "invalid_profile_operation";
    case ProfileDecision::WRONG_SESSION: return "gateway_session_mismatch";
    case ProfileDecision::STALE: return "stale_operation";
    case ProfileDecision::BUSY: return "busy";
  }
  return "invalid_profile_operation";
}

// Firmware can stage or cancel an advisory plan. There is no apply operation.
class ProfilePlan {
 public:
  uint32_t operation() const { return operation_; }
  uint8_t target() const { return target_; }
  bool pending() const { return pending_; }

  ProfileDecision request(uint64_t session, uint64_t requestSession, uint32_t operation,
                          uint8_t target, bool radioBusy) {
    if (session != requestSession) return ProfileDecision::WRONG_SESSION;
    if (!operation || target >= LORA_PROFILE_COUNT) return ProfileDecision::INVALID;
    if (operation == operation_) {
      return target == target_ ? ProfileDecision::DUPLICATE : ProfileDecision::STALE;
    }
    if (operation < operation_) return ProfileDecision::STALE;
    if (pending_ || radioBusy) return ProfileDecision::BUSY;
    operation_ = operation;
    target_ = target;
    pending_ = target != INITIAL_LORA_PROFILE;
    return pending_ ? ProfileDecision::PLANNED : ProfileDecision::UNCHANGED;
  }

  ProfileDecision cancel(uint64_t session, uint64_t requestSession, uint32_t operation) {
    if (session != requestSession) return ProfileDecision::WRONG_SESSION;
    if (!operation || operation != operation_) return ProfileDecision::STALE;
    if (!pending_) return ProfileDecision::DUPLICATE;
    pending_ = false;
    return ProfileDecision::CANCELLED;
  }

 private:
  uint32_t operation_ = 0;
  uint8_t target_ = INITIAL_LORA_PROFILE;
  bool pending_ = false;
};

}  // namespace vfence
