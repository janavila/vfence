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
                              char (&line)[512]) {
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
      "[APP] {\"schema\":\"vfence.gateway.pdr.v1\",\"kind\":\"pdr_window_summary\",\"collarId\":%u,"
      "\"gatewaySessionId\":\"%016llx\",\"windowId\":\"%016llx\",\"collarSessionId\":%s,"
      "\"startedAtMs\":%lu,\"durationMs\":60000,\"profileId\":%s,\"receivedCount\":%u,"
      "\"estimatedTxCount\":%s,\"estimatedLostCount\":%s,\"pdrX100\":%s,\"valid\":%s,\"reason\":\"%s\"}\n",
      collarId, static_cast<unsigned long long>(gatewaySession),
      static_cast<unsigned long long>(report.windowId), session,
      static_cast<unsigned long>(report.startedAtMs), profile, unsigned(result.received),
      expected, lost, pdr, valid ? "true" : "false", pdrReasonName(result.reason));
  return length > 0 && length < static_cast<int>(sizeof(line)) ? static_cast<size_t>(length) : 0;
}

}  // namespace vfence
