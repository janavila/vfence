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
    if (!known_) {
      known_ = true;
      session_ = tag.sessionId;
    }
    if (tag.sessionId != session_) {
      considerSession(tag, now, appliedProfile);
      return false;
    }

    const uint32_t advance = tag.ordinal - frontier_;
    const bool forward = !hasFrontier_ || (advance != 0 && advance < 0x80000000UL);
    if (sessionUncertain_) {
      if (forward) advanceFrontier(tag.ordinal, now);
      return false;
    }
    if (hasFrontier_ && now - lastAdvanceAt_ >= PDR_REFERENCE_TIMEOUT_MS) {
      // A long gap may re-anchor on forward progress, never on a duplicate or
      // a delayed ordinal at or behind the last known frontier.
      if (!forward) return false;
      if (anchored_) markDiscontinuity();
      anchored_ = false;
    }

    if (!forward) {
      if (!anchored_) return false;
      const uint32_t offset = tag.ordinal - anchor_;
      if (offset >= PDR_MAX_SPAN || (seen_ & (UINT64_C(1) << offset))) return false;
      seen_ |= UINT64_C(1) << offset;
      countReception();
      noteProfile(tag.profileId, appliedProfile);
      return true;
    }

    advanceFrontier(tag.ordinal, now);
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
    sessionUncertain_ = false;
    return result;
  }

 private:
  void considerSession(const PositionDeliveryTag& tag, uint32_t now, uint8_t appliedProfile) {
    if (isRetired(tag.sessionId)) return;
    // Random IDs carry no chronology. Do not count observations from either
    // session in the rest of a window with an unrecognized foreign session.
    sessionUncertain_ = true;
    window_.hasSession = false;
    if (window_.reason != PdrReason::PROFILE_CHANGED) window_.reason = PdrReason::SESSION_CHANGED;

    // An uninterrupted incumbent wins over an unsolicited switch. After a
    // quiet period, require two forward observations in the same profile.
    if (tag.profileId != appliedProfile || now - lastAdvanceAt_ < PDR_REFERENCE_TIMEOUT_MS) return;
    if (!candidatePending_ || candidate_ != tag.sessionId || candidateProfile_ != tag.profileId ||
        now - candidateSeenAt_ >= PDR_REFERENCE_TIMEOUT_MS) {
      candidate_ = tag.sessionId;
      candidateOrdinal_ = tag.ordinal;
      candidateSeenAt_ = now;
      candidateProfile_ = tag.profileId;
      candidatePending_ = true;
      return;
    }
    const uint32_t advance = tag.ordinal - candidateOrdinal_;
    if (advance == 0 || advance >= 0x80000000UL) return;
    if (advance >= PDR_MAX_SPAN) {
      candidateOrdinal_ = tag.ordinal;
      candidateSeenAt_ = now;
      return;
    }

    retire(session_);
    session_ = tag.sessionId;
    anchored_ = false;
    // The new session only contributes to the next window. Keep the confirmed
    // ordinal as a frontier so the probation packets cannot be counted again.
    advanceFrontier(tag.ordinal, now);
  }

  void advanceFrontier(uint32_t ordinal, uint32_t now) {
    frontier_ = ordinal;
    lastAdvanceAt_ = now;
    hasFrontier_ = true;
    candidatePending_ = false;
  }

  // Three indexes in a 128-bit Bloom filter. Bits are never cleared during
  // this Gateway boot: retired IDs remain rejected, with possible false positives.
  static uint64_t sessionHash(uint64_t id) {
    id ^= id >> 33;
    id *= UINT64_C(0xff51afd7ed558ccd);
    return id ^ (id >> 33);
  }

  bool isRetired(uint64_t session) const {
    uint64_t hash = sessionHash(session);
    for (uint8_t i = 0; i < 3; ++i, hash >>= 7) {
      if (!(retired_[((hash >> 6) & 1)] & (UINT64_C(1) << (hash & 63)))) return false;
    }
    return true;
  }

  void retire(uint64_t session) {
    uint64_t hash = sessionHash(session);
    for (uint8_t i = 0; i < 3; ++i, hash >>= 7) {
      retired_[((hash >> 6) & 1)] |= UINT64_C(1) << (hash & 63);
    }
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
  uint64_t candidate_ = 0;
  uint32_t frontier_ = 0;
  uint32_t anchor_ = 0;
  uint32_t lastAdvanceAt_ = 0;
  uint32_t candidateOrdinal_ = 0;
  uint32_t candidateSeenAt_ = 0;
  PdrWindowResult window_;
  uint8_t candidateProfile_ = PDR_UNKNOWN_PROFILE;
  bool known_ = false;
  bool hasFrontier_ = false;
  bool anchored_ = false;
  bool candidatePending_ = false;
  bool sessionUncertain_ = false;
};

}  // namespace vfence
