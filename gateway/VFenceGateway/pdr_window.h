#pragma once

#include "counted_position.h"

namespace vfence {

constexpr uint32_t PDR_WINDOW_MS = 60000;
constexpr uint32_t PDR_REFERENCE_TIMEOUT_MS = 2 * PDR_WINDOW_MS;
constexpr unsigned PDR_MAX_SPAN = 64;
constexpr uint8_t PDR_UNKNOWN_PROFILE = 0xff;

enum class PdrReason : uint8_t { OK, NO_SAMPLES, ONE_SAMPLE, SESSION_CHANGED, PROFILE_CHANGED, DISCONTINUITY };

inline const char* pdrReasonName(PdrReason reason) {
  switch (reason) {
    case PdrReason::OK: return "ok";
    case PdrReason::NO_SAMPLES: return "no_samples";
    case PdrReason::ONE_SAMPLE: return "one_sample";
    case PdrReason::SESSION_CHANGED: return "session_changed";
    case PdrReason::PROFILE_CHANGED: return "profile_changed";
    case PdrReason::DISCONTINUITY: return "discontinuity";
  }
  return "discontinuity";
}

struct PdrWindowResult {
  uint64_t collarSession = 0;
  uint16_t pdrX100 = 0;
  uint8_t received = 0;
  uint8_t estimatedTx = 0;
  uint8_t profileId = PDR_UNKNOWN_PROFILE;
  PdrReason reason = PdrReason::NO_SAMPLES;
  bool hasSession = false;
};

class PositionPdr {
 public:
  // True means that the frame contributed evidence to this window.
  bool observe(const PositionDeliveryTag& tag, uint32_t now, uint8_t appliedProfile) {
    if (known_ && tag.sessionId != session_ && isRetired(tag.sessionId)) return false;
    if (known_ && tag.sessionId != session_) {
      retired_[retiredCursor_++ % 2] = session_;
      if (retiredCount_ < 2) ++retiredCount_;
      if (window_.received) {
        window_.hasSession = false;
        window_.reason = PdrReason::SESSION_CHANGED;
      }
      anchored_ = false;
      hasFrontier_ = false;
    }
    known_ = true;
    session_ = tag.sessionId;

    if (hasFrontier_ && now - lastAdvanceAt_ >= PDR_REFERENCE_TIMEOUT_MS) {
      hasFrontier_ = false;
      if (anchored_) markDiscontinuity();
      anchored_ = false;
    }

    const uint32_t advance = tag.ordinal - frontier_;
    if (hasFrontier_ && (advance == 0 || advance >= 0x80000000UL)) {
      if (!anchored_) return false;
      const uint32_t offset = tag.ordinal - anchor_;
      if (offset >= PDR_MAX_SPAN || (seen_ & (UINT64_C(1) << offset))) return false;
      seen_ |= UINT64_C(1) << offset;
      countReception();
      noteProfile(tag.profileId, appliedProfile);
      return true;
    }

    frontier_ = tag.ordinal;
    lastAdvanceAt_ = now;
    hasFrontier_ = true;
    if (!anchored_) {
      if (!window_.received) {
        window_.collarSession = tag.sessionId;
        window_.hasSession = true;
        if (window_.reason != PdrReason::PROFILE_CHANGED) {
          window_.profileId = tag.profileId;
          window_.reason = PdrReason::ONE_SAMPLE;
        }
      }
      countReception();
      anchor_ = tag.ordinal;
      seen_ = 1;
      anchored_ = true;
      noteProfile(tag.profileId, appliedProfile);
      return true;
    }

    const uint32_t offset = tag.ordinal - anchor_;
    if (offset >= PDR_MAX_SPAN) {
      markDiscontinuity();
      anchor_ = tag.ordinal;
      seen_ = 1;
      countReception();
      noteProfile(tag.profileId, appliedProfile);
      return true;
    }
    seen_ |= UINT64_C(1) << offset;
    countReception();
    if (window_.reason == PdrReason::ONE_SAMPLE || window_.reason == PdrReason::OK) {
      window_.reason = PdrReason::OK;
      window_.estimatedTx = static_cast<uint8_t>(offset + 1);
    }
    noteProfile(tag.profileId, appliedProfile);
    return true;
  }

  bool known() const { return known_; }

  void profileChanged() {
    window_.reason = PdrReason::PROFILE_CHANGED;
    window_.profileId = PDR_UNKNOWN_PROFILE;
  }

  PdrWindowResult closeWindow() {
    PdrWindowResult result = window_;
    if (result.reason == PdrReason::OK) {
      result.pdrX100 = (10000UL * result.received + result.estimatedTx / 2) / result.estimatedTx;
    } else {
      result.estimatedTx = 0;
    }
    window_ = {};
    anchored_ = false;
    return result;
  }

 private:
  bool isRetired(uint64_t session) const {
    for (uint8_t i = 0; i < retiredCount_; ++i)
      if (retired_[i] == session) return true;
    return false;
  }

  void noteProfile(uint8_t reported, uint8_t applied) {
    if (reported != applied || window_.profileId != reported) profileChanged();
  }

  void markDiscontinuity() {
    if (window_.reason == PdrReason::ONE_SAMPLE || window_.reason == PdrReason::OK)
      window_.reason = PdrReason::DISCONTINUITY;
  }

  void countReception() {
    if (window_.received < PDR_MAX_SPAN) ++window_.received;
  }

  uint64_t session_ = 0;
  uint64_t retired_[2]{};
  uint64_t seen_ = 0;
  uint32_t frontier_ = 0;
  uint32_t anchor_ = 0;
  uint32_t lastAdvanceAt_ = 0;
  PdrWindowResult window_;
  uint8_t retiredCursor_ = 0;
  uint8_t retiredCount_ = 0;
  bool known_ = false;
  bool hasFrontier_ = false;
  bool anchored_ = false;
};

}  // namespace vfence
