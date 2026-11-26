#pragma once

#include <stdio.h>
#include "../../firmware_common/metric_evidence.h"

namespace vfence {

struct EvidenceSlot {
  MetricWindow window{};
  uint64_t boot = 0;
  ExactReception rx;
  uint32_t lastSerial = 0;
  bool closed = false;
  bool sent = false;
  bool committed = false;
  bool ackPending = false;
  bool conflict = false;

  MetricProof proof() const {
    return MetricProof{window, boot, firstTx_, lastTx_, hdop_, samples_};
  }

  bool retain(const MetricProof& candidate) {
    if (conflict || candidate.boot != boot || !sameMetricWindow(candidate.window, window)) return false;
    if (closed) {
      if (firstTx_ != candidate.firstTx || lastTx_ != candidate.lastTx ||
          hdop_ != candidate.hdop || samples_ != candidate.samples) return false;
      ackPending = committed;
      return true;
    }
    if (!rx.proves(candidate)) return false;
    firstTx_ = candidate.firstTx;
    lastTx_ = candidate.lastTx;
    hdop_ = candidate.hdop;
    samples_ = candidate.samples;
    closed = true;
    return true;
  }

 private:
  // The proof shares the slot's window and boot identity.
  uint32_t firstTx_ = 0;
  uint32_t lastTx_ = 0;
  uint16_t hdop_ = 0;
  uint8_t samples_ = 0;
};

inline int32_t evidenceMean(int32_t sum, uint16_t samples) {
  return !samples ? 0 : sum >= 0 ? (sum + samples / 2) / samples : (sum - samples / 2) / samples;
}

inline size_t formatEvidence(const EvidenceSlot& slot, unsigned collar, char (&out)[512]) {
  if (!slot.closed || slot.conflict) return 0;
  const auto p = slot.proof();
  const unsigned tx = p.lastTx - p.firstTx;
  char ratio[11] = "null", hdop[6] = "null", rssi[12] = "null", snr[12] = "null";
  if (tx) snprintf(ratio, sizeof(ratio), "%u", (10000U * slot.rx.count + tx / 2U) / tx);
  if (p.samples) snprintf(hdop, sizeof(hdop), "%u", unsigned(p.hdop));
  if (slot.rx.radioCount) {
    snprintf(rssi, sizeof(rssi), "%ld", static_cast<long>(evidenceMean(slot.rx.rssiSum, slot.rx.radioCount)));
    snprintf(snr, sizeof(snr), "%ld", static_cast<long>(evidenceMean(slot.rx.snrSum, slot.rx.radioCount)));
  }
  const int size = snprintf(out, sizeof(out),
      "[APP] {\"schema\":\"vfence.gateway.evidence.v1\",\"kind\":\"metric_evidence\","
      "\"collarId\":%u,\"gatewaySessionId\":\"%016llx\",\"collarSessionId\":\"%016llx\","
      "\"windowId\":\"%016llx\",\"startedAtMs\":%lu,\"durationMs\":60000,\"profileId\":%u,"
      "\"txFirst\":%lu,\"txLast\":%lu,\"receivedCount\":%u,\"pdrX100\":%s,"
      "\"radioCount\":%u,\"rssiX100\":%s,\"snrX100\":%s,\"hdopCount\":%u,\"hdopX100\":%s}\n",
      collar, static_cast<unsigned long long>(p.window.gateway), static_cast<unsigned long long>(p.boot),
      static_cast<unsigned long long>(p.window.id), static_cast<unsigned long>(p.window.start),
      unsigned(p.window.profile), static_cast<unsigned long>(p.firstTx), static_cast<unsigned long>(p.lastTx),
      unsigned(slot.rx.count), ratio, unsigned(slot.rx.radioCount), rssi, snr, unsigned(p.samples), hdop);
  return size > 0 && size < static_cast<int>(sizeof(out)) ? static_cast<size_t>(size) : 0;
}

class GatewayMetricEvidence {
 public:
  static constexpr uint8_t MAX_COLLARS = 16;
  struct Member {
    uint8_t id = 0;
    uint32_t lastClock = 0;
    uint64_t boot = 0;
    EvidenceSlot windows[EVIDENCE_HISTORY];
  };

  Member members[MAX_COLLARS];

  Member* admit(uint8_t id, uint32_t now) {
    Member* available = nullptr;
    for (auto& member : members) {
      if (member.id == id) { member.lastClock = now; return &member; }
      if (!available && (!member.id || now - member.lastClock > EVIDENCE_HISTORY * EVIDENCE_WINDOW_MS))
        available = &member;
    }
    if (available) {
      *available = Member{};
      available->id = id;
      available->lastClock = now;
    }
    return available;
  }

  EvidenceSlot* slot(uint8_t id, const MetricWindow& window, uint64_t boot, bool create) {
    for (auto& member : members) {
      if (member.id != id) continue;
      auto& slot = member.windows[window.id % EVIDENCE_HISTORY];
      if (sameMetricWindow(slot.window, window)) {
        if (slot.boot != boot && create) slot.conflict = true;
        return slot.boot == boot && !slot.conflict ? &slot : nullptr;
      }
      if (!create || (slot.window.gateway == window.gateway && slot.window.id > window.id)) return nullptr;
      slot = EvidenceSlot{};
      slot.window = window;
      slot.boot = boot;
      return &slot;
    }
    return nullptr;
  }
};

}  // namespace vfence
