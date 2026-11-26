#pragma once

#include <string.h>
#include "vfence_protocol.h"

namespace vfence {

constexpr uint8_t METRIC_CLOCK = 0x06;
constexpr uint8_t METRIC_PROOF = 0x07;
constexpr uint8_t METRIC_UPLINK = 0x08;
constexpr uint8_t EVIDENCE_VERSION = 1;
constexpr uint32_t EVIDENCE_WINDOW_MS = 60000;
constexpr uint32_t CLOCK_LEASE_MS = 90000;
constexpr uint32_t MAX_CLOCK_RTT_MS = 4000;
constexpr uint16_t MAX_WINDOW_ATTEMPTS = 512;
constexpr uint8_t EVIDENCE_HISTORY = 4;
constexpr size_t EVIDENCE_PREFIX = 31;
constexpr size_t EVIDENCE_PACKET_MAX = HEADER_SIZE + EVIDENCE_PREFIX + MAX_PACKET_SIZE;

struct MetricWindow {
  uint64_t gateway;
  uint64_t id;
  uint32_t start;
  uint8_t profile;
};

inline bool sameMetricWindow(const MetricWindow& a, const MetricWindow& b) {
  return a.gateway == b.gateway && a.id == b.id && a.start == b.start && a.profile == b.profile;
}

inline void evidenceWrite64(uint8_t* p, uint64_t n) {
  writeU32BE(p, static_cast<uint32_t>(n >> 32));
  writeU32BE(p + 4, static_cast<uint32_t>(n));
}

inline uint64_t evidenceRead64(const uint8_t* p) {
  return (static_cast<uint64_t>(readU32BE(p)) << 32) | readU32BE(p + 4);
}

inline uint8_t* evidenceHeader(uint8_t* data, size_t capacity, uint8_t id, uint16_t sequence, uint8_t size) {
  const Header header{PROTOCOL_VERSION, MessageType::CONTROL, false, false, false, id, sequence, size};
  return capacity >= HEADER_SIZE + size && encodeHeader(data, capacity, header) ? data + HEADER_SIZE : nullptr;
}

inline bool evidenceControl(const PacketView& packet, uint8_t subtype) {
  return packet.header.version == PROTOCOL_VERSION && packet.header.type == MessageType::CONTROL &&
         !packet.header.ackReq && !packet.header.retry && !packet.header.urgent && packet.payloadLength >= 2 &&
         packet.payload[0] == subtype && packet.payload[1] == EVIDENCE_VERSION;
}

inline size_t encodeMetricClock(uint8_t* data, size_t capacity, uint8_t id, uint16_t seq, uint32_t nonce,
                                uint64_t boot, const MetricWindow* window = nullptr, uint32_t tick = 0) {
  const uint8_t size = window ? 40 : 15;
  auto* p = evidenceHeader(data, capacity, id, seq, size);
  if (!p) return 0;
  p[0] = METRIC_CLOCK; p[1] = EVIDENCE_VERSION; p[2] = window ? 2 : 1;
  writeU32BE(p + 3, nonce);
  if (window) {
    evidenceWrite64(p + 7, window->gateway);
    evidenceWrite64(p + 15, window->id);
    writeU32BE(p + 23, window->start);
    writeU32BE(p + 27, tick);
    p[31] = window->profile;
    evidenceWrite64(p + 32, boot);
  } else evidenceWrite64(p + 7, boot);
  return HEADER_SIZE + size;
}

struct MetricAttempt {
  uint64_t gateway;
  uint64_t boot;
  uint64_t window;
  uint32_t ordinal;
  uint8_t profile;
};

inline size_t wrapMetricAttempt(uint8_t* output, size_t capacity, const uint8_t* original, size_t length,
                                const MetricAttempt& attempt) {
  PacketView inner;
  if (length > MAX_PACKET_SIZE || !decodePacket(original, length, inner)) return 0;
  auto* p = evidenceHeader(output, capacity, inner.header.collarId, inner.header.sequence,
                           static_cast<uint8_t>(EVIDENCE_PREFIX + length));
  if (!p) return 0;
  p[0] = METRIC_UPLINK; p[1] = EVIDENCE_VERSION;
  evidenceWrite64(p + 2, attempt.gateway);
  evidenceWrite64(p + 10, attempt.boot);
  evidenceWrite64(p + 18, attempt.window);
  writeU32BE(p + 26, attempt.ordinal);
  p[30] = attempt.profile;
  memcpy(p + EVIDENCE_PREFIX, original, length);
  return HEADER_SIZE + EVIDENCE_PREFIX + length;
}

inline bool unwrapMetricAttempt(const PacketView& outer, PacketView& inner, MetricAttempt& attempt) {
  if (!evidenceControl(outer, METRIC_UPLINK) || outer.payloadLength < EVIDENCE_PREFIX + HEADER_SIZE ||
      outer.payloadLength > EVIDENCE_PREFIX + MAX_PACKET_SIZE ||
      !decodePacket(outer.payload + EVIDENCE_PREFIX, outer.payloadLength - EVIDENCE_PREFIX, inner) ||
      inner.header.collarId != outer.header.collarId || inner.header.sequence != outer.header.sequence ||
      inner.header.version != PROTOCOL_VERSION || evidenceControl(inner, METRIC_UPLINK)) return false;
  const auto* p = outer.payload;
  attempt = {evidenceRead64(p + 2), evidenceRead64(p + 10), evidenceRead64(p + 18), readU32BE(p + 26), p[30]};
  return attempt.gateway && attempt.boot && attempt.window && attempt.profile <= 3;
}

struct MetricProof {
  MetricWindow window;
  uint64_t boot;
  uint32_t firstTx;
  uint32_t lastTx;
  uint16_t hdop;
  uint8_t samples;
};

inline size_t encodeMetricProof(uint8_t* data, size_t capacity, uint8_t id, uint16_t seq,
                                const MetricProof& proof, bool ack = false) {
  const uint8_t size = ack ? 27 : 43;
  auto* p = evidenceHeader(data, capacity, id, seq, size);
  if (!p) return 0;
  p[0] = METRIC_PROOF; p[1] = EVIDENCE_VERSION; p[2] = ack ? 2 : 1;
  evidenceWrite64(p + 3, proof.window.gateway);
  evidenceWrite64(p + 11, proof.boot);
  evidenceWrite64(p + 19, proof.window.id);
  if (!ack) {
    writeU32BE(p + 27, proof.window.start);
    writeU32BE(p + 31, proof.firstTx);
    writeU32BE(p + 35, proof.lastTx);
    writeU16BE(p + 39, proof.hdop);
    p[41] = proof.samples;
    p[42] = proof.window.profile;
  }
  return HEADER_SIZE + size;
}

inline bool decodeMetricProof(const PacketView& packet, MetricProof& proof, bool ack = false) {
  if (!evidenceControl(packet, METRIC_PROOF) || packet.payloadLength != (ack ? 27 : 43) ||
      packet.payload[2] != (ack ? 2 : 1)) return false;
  const auto* p = packet.payload;
  proof = {};
  proof.window.gateway = evidenceRead64(p + 3);
  proof.boot = evidenceRead64(p + 11);
  proof.window.id = evidenceRead64(p + 19);
  if (!proof.window.gateway || !proof.boot || !proof.window.id) return false;
  if (ack) return true;
  proof.window.start = readU32BE(p + 27);
  proof.firstTx = readU32BE(p + 31);
  proof.lastTx = readU32BE(p + 35);
  proof.hdop = readU16BE(p + 39);
  proof.samples = p[41];
  proof.window.profile = p[42];
  return proof.window.profile <= 3 && proof.samples <= 60 &&
         (proof.samples ? proof.hdop != 0xffff : proof.hdop == 0xffff);
}

class ExactReception {
 public:
  uint16_t count = 0;
  uint16_t radioCount = 0;
  int32_t rssiSum = 0, snrSum = 0;

  bool seen(uint32_t ordinal) const {
    if (!count) return false;
    const int32_t delta = static_cast<int32_t>(ordinal - anchor_);
    if (delta < low_ || delta > high_) return false;
    const uint16_t index = ordinal % MAX_WINDOW_ATTEMPTS;
    return seen_[index / 32] & (uint32_t(1) << (index % 32));
  }

  void observe(uint32_t ordinal, bool radioValid, int16_t rssi, int16_t snr) {
    if (failed_) return;
    const int32_t delta = count ? static_cast<int32_t>(ordinal - anchor_) : 0;
    if (delta <= -MAX_WINDOW_ATTEMPTS || delta >= MAX_WINDOW_ATTEMPTS) { failed_ = true; return; }
    if (!count) anchor_ = ordinal;
    if (delta < low_) low_ = delta;
    if (delta > high_) high_ = delta;
    if (high_ - low_ >= MAX_WINDOW_ATTEMPTS) { failed_ = true; return; }
    const uint16_t index = ordinal % MAX_WINDOW_ATTEMPTS;
    const uint32_t bit = uint32_t(1) << (index % 32);
    auto& cell = seen_[index / 32];
    if (cell & bit) return;
    cell |= bit;
    ++count;
    if (radioValid) { ++radioCount; rssiSum += rssi; snrSum += snr; }
  }

  bool proves(const MetricProof& proof) const {
    const uint32_t total = proof.lastTx - proof.firstTx;
    return !failed_ && total <= MAX_WINDOW_ATTEMPTS && count <= total &&
           (!count || ((anchor_ + low_ - proof.firstTx) >= 1 &&
                       (anchor_ + high_ - proof.firstTx) <= total));
  }

 private:
  uint32_t seen_[MAX_WINDOW_ATTEMPTS / 32]{};
  uint32_t anchor_ = 0;
  int32_t low_ = 0, high_ = 0;
  bool failed_ = false;
};

inline bool metricDue(uint32_t now, uint32_t target) {
  return static_cast<int32_t>(now - target) >= 0;
}

class CollarMetricClock {
 public:
  bool observe(const MetricWindow& observed, uint32_t tick, uint32_t sent, uint32_t now, uint8_t profile) {
    const uint32_t rtt = now - sent, elapsed = tick - observed.start;
    if (!observed.gateway || !observed.id || observed.profile != profile ||
        rtt > MAX_CLOCK_RTT_MS || elapsed >= EVIDENCE_WINDOW_MS) return false;
    const uint32_t uncertainty = (rtt + 1) / 2 + 1;
    const uint32_t boundary = sent + rtt / 2 + EVIDENCE_WINDOW_MS - elapsed;
    if (static_cast<int32_t>(boundary - now) <= static_cast<int32_t>(uncertainty)) return false;
    const MetricWindow next{observed.gateway, observed.id + 1, observed.start + EVIDENCE_WINDOW_MS, profile};
    const bool sameEpoch = scheduled_ && next.gateway == next_.gateway && next.profile == next_.profile;
    if (sameEpoch) {
      if (next.id < next_.id) return false;
      const uint64_t advance = next.id - next_.id;
      if (advance > 2 || next.start != next_.start + static_cast<uint32_t>(advance) * EVIDENCE_WINDOW_MS)
        reset();
      else {
        const int32_t difference = static_cast<int32_t>(boundary - nextAt_ - advance * EVIDENCE_WINDOW_MS);
        // 1 ms/s of combined drift budget over the lifetime of the reference.
        const int32_t budget = uncertainty + uncertainty_ + (now - refreshed_) / 1000 + 2;
        if (difference < -budget || difference > budget) reset();
      }
    } else {
      reset();
    }
    next_ = next;
    nextAt_ = boundary;
    uncertainty_ = uncertainty;
    refreshed_ = now;
    scheduled_ = true;
    return true;
  }

  template <class Close>
  void service(uint32_t now, uint8_t profile, uint64_t boot, uint32_t tx, Close close) {
    if (!scheduled_) return;
    if (profile != next_.profile || now - refreshed_ >= CLOCK_LEASE_MS) { reset(); return; }
    if (!metricDue(now, nextAt_)) return;
    if (now - nextAt_ >= EVIDENCE_WINDOW_MS) { reset(); return; }
    if (active_ && current_.id + 1 == next_.id) {
      const uint16_t mean = samples_ ? static_cast<uint16_t>((sum_ + samples_ / 2U) / samples_) : 0xffff;
      close(MetricProof{current_, boot, startTx_, tx, mean, samples_});
    }
    current_ = next_;
    localStart_ = nextAt_;
    startTx_ = tx;
    prefixReset();
    ++next_.id;
    next_.start += EVIDENCE_WINDOW_MS;
    nextAt_ += EVIDENCE_WINDOW_MS;
    active_ = true;
  }

  const MetricWindow* window() const { return active_ ? &current_ : nullptr; }

  void hdop(uint32_t now, uint16_t value) {
    if (!active_ || value == 0xffff) return;
    const uint32_t slot = (now - localStart_) / 1000;
    if (slot >= 60 || slot == lastSlot_) return;
    lastSlot_ = slot;
    sum_ += value;
    ++samples_;
  }

 private:
  void prefixReset() { sum_ = 0; samples_ = 0; lastSlot_ = 60; }
  void reset() { scheduled_ = active_ = false; prefixReset(); }
  MetricWindow current_{}, next_{};
  uint32_t nextAt_ = 0, localStart_ = 0, refreshed_ = 0, uncertainty_ = 0, startTx_ = 0, sum_ = 0;
  uint32_t lastSlot_ = 60;
  uint8_t samples_ = 0;
  bool scheduled_ = false, active_ = false;
};

}  // namespace vfence
