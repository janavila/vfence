#pragma once

#include <stdio.h>
#include "pdr_window.h"

namespace vfence {

struct PdrReport {
  PdrWindowResult result;
  uint64_t windowId;
  uint32_t startedAtMs;
};

inline size_t formatPdrReport(const PdrReport& report, uint64_t gatewaySession, unsigned collarId,
                              char (&line)[512], bool committedDelivery = false) {
  const auto& result = report.result;
  const bool valid = result.reason == PdrReason::OK;
  char session[19] = "null", profile[5] = "null";
  char expected[5] = "null", lost[5] = "null", pdr[6] = "null";
  if (result.hasSession)
    snprintf(session, sizeof(session), "\"%016llx\"", static_cast<unsigned long long>(result.collarSession));
  if (result.profileId < LORA_PROFILE_COUNT) snprintf(profile, sizeof(profile), "%u", unsigned(result.profileId));
  if (valid) {
    snprintf(expected, sizeof(expected), "%u", unsigned(result.estimatedTx));
    snprintf(lost, sizeof(lost), "%u", unsigned(result.estimatedTx - result.received));
    snprintf(pdr, sizeof(pdr), "%u", unsigned(result.pdrX100));
  }
  const int length = snprintf(line, sizeof(line),
      "[APP] {\"schema\":\"vfence.gateway.pdr.v%u\",\"kind\":\"pdr_window_summary\",\"collarId\":%u,"
      "\"gatewaySessionId\":\"%016llx\",\"windowId\":\"%016llx\",\"collarSessionId\":%s,"
      "\"startedAtMs\":%lu,\"durationMs\":60000,\"profileId\":%s,\"receivedCount\":%u,"
      "\"estimatedTxCount\":%s,\"estimatedLostCount\":%s,\"pdrX100\":%s,\"valid\":%s,\"reason\":\"%s\"}\n",
      committedDelivery ? 2U : 1U, collarId, static_cast<unsigned long long>(gatewaySession),
      static_cast<unsigned long long>(report.windowId), session,
      static_cast<unsigned long>(report.startedAtMs), profile, unsigned(result.received),
      expected, lost, pdr, valid ? "true" : "false", pdrReasonName(result.reason));
  return length > 0 && length < static_cast<int>(sizeof(line)) ? static_cast<size_t>(length) : 0;
}

class PdrSerialDelivery {
 public:
  static constexpr uint8_t CAPACITY = 64;
  uint32_t refused = 0;
  uint32_t transmitted = 0;
  uint32_t retries = 0;
  uint32_t confirmed = 0;

  bool retain(uint8_t collar, const PdrReport& report) {
    if (count_ == CAPACITY) {
      ++refused;
      return false;
    }
    auto& entry = entries_[(head_ + count_) % CAPACITY];
    entry = {report, 0, collar, false};
    ++count_;
    return true;
  }

  template <class Send>
  void service(uint32_t now, uint64_t session, Send send) {
    if (!count_) return;
    auto& entry = entries_[head_];
    if (entry.sent && now - entry.lastSend < 1000) return;
    char line[512];
    const auto length = formatPdrReport(entry.report, session, entry.collar, line, true);
    if (!length || !send(reinterpret_cast<const uint8_t*>(line), length)) return;
    if (entry.sent) ++retries;
    ++transmitted;
    entry.sent = true;
    entry.lastSend = now;
  }

  bool confirm(uint64_t session, uint64_t reportedSession, uint8_t collar, uint64_t window) {
    if (!count_ || session != reportedSession) return false;
    const auto& entry = entries_[head_];
    if (!entry.sent || entry.collar != collar || entry.report.windowId != window) return false;
    head_ = (head_ + 1) % CAPACITY;
    --count_;
    ++confirmed;
    return true;
  }

 private:
  struct Entry {
    PdrReport report;
    uint32_t lastSend;
    uint8_t collar;
    bool sent;
  };
  Entry entries_[CAPACITY]{};
  uint8_t head_ = 0;
  uint8_t count_ = 0;
};

}  // namespace vfence
