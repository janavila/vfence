#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace vfence {

constexpr size_t METRIC_SERIAL_MAX_LINE = 512;
constexpr uint8_t METRIC_LAST_COLLAR_ID = 254;
constexpr char METRIC_SERIAL_SCHEMA[] = "vfence.gateway.serial.v1";
constexpr char HDOP_SERIAL_KIND[] = "hdop_window_summary";
constexpr char RADIO_SERIAL_KIND[] = "radio_window_summary";

enum class MetricSerialKind : uint8_t { HDOP, RADIO };

struct MetricSerialId {
  uint64_t sessionId;
  uint64_t windowId;
  uint8_t collarId;
  MetricSerialKind kind;

  bool operator==(const MetricSerialId& other) const {
    return sessionId == other.sessionId && windowId == other.windowId &&
           collarId == other.collarId && kind == other.kind;
  }
};

struct MetricSerialSummary {
  MetricSerialId id;
  uint32_t startedAtMs;
  uint32_t receivedAtMs;
  uint32_t durationMs;
  uint16_t sampleCount;
  int32_t meanX100;
  int32_t snrX100;
  bool meanValid;
  bool snrValid;
  bool attempted;
};

inline const char* metricSerialMean(char (&number)[16], int32_t value, bool valid) {
  if (!valid) return "null";
  std::snprintf(number, sizeof(number), "%ld", static_cast<long>(value));
  return number;
}

inline size_t formatMetricSerialLine(const MetricSerialSummary& summary,
                                     uint64_t gatewaySessionId,
                                     char (&line)[METRIC_SERIAL_MAX_LINE + 1]) {
  const MetricSerialId& id = summary.id;
  const auto gatewaySession = static_cast<unsigned long long>(gatewaySessionId);
  char mean[16], snr[16];
  const char* meanValue = metricSerialMean(mean, summary.meanX100, summary.meanValid);
  int length;
  if (id.kind == MetricSerialKind::HDOP) {
    length = std::snprintf(
        line, sizeof(line),
        "[APP] {\"schema\":\"%s\",\"kind\":\"%s\",\"collarId\":%u,"
        "\"collarSessionId\":\"%016llx\",\"windowId\":\"%016llx\","
        "\"collarStartedAtMs\":%lu,\"durationMs\":%lu,\"hdopMeanX100\":%s,"
        "\"sampleCount\":%u,\"hdopMeanValid\":%s,"
        "\"gatewaySessionId\":\"%016llx\",\"gatewayReceivedAtMs\":%lu}\n",
        METRIC_SERIAL_SCHEMA, HDOP_SERIAL_KIND, static_cast<unsigned>(id.collarId),
        static_cast<unsigned long long>(id.sessionId), static_cast<unsigned long long>(id.windowId),
        static_cast<unsigned long>(summary.startedAtMs), static_cast<unsigned long>(summary.durationMs),
        meanValue, static_cast<unsigned>(summary.sampleCount), summary.meanValid ? "true" : "false",
        gatewaySession, static_cast<unsigned long>(summary.receivedAtMs));
  } else {
    const char* snrValue = metricSerialMean(snr, summary.snrX100, summary.snrValid);
    length = std::snprintf(
        line, sizeof(line),
        "[APP] {\"schema\":\"%s\",\"kind\":\"%s\",\"collarId\":%u,"
        "\"gatewaySessionId\":\"%016llx\",\"windowId\":\"%016llx\","
        "\"gatewayStartedAtMs\":%lu,\"durationMs\":%lu,\"rssiMeanX100\":%s,"
        "\"snrMeanX100\":%s,\"sampleCount\":%u,\"rssiMeanValid\":%s,\"snrMeanValid\":%s}\n",
        METRIC_SERIAL_SCHEMA, RADIO_SERIAL_KIND, static_cast<unsigned>(id.collarId),
        static_cast<unsigned long long>(id.sessionId), static_cast<unsigned long long>(id.windowId),
        static_cast<unsigned long>(summary.startedAtMs), static_cast<unsigned long>(summary.durationMs),
        meanValue, snrValue, static_cast<unsigned>(summary.sampleCount),
        summary.meanValid ? "true" : "false", summary.snrValid ? "true" : "false");
  }
  return length > 0 && static_cast<size_t>(length) <= METRIC_SERIAL_MAX_LINE
      ? static_cast<size_t>(length) : 0;
}

struct MetricCommitAck {
  MetricSerialId id;
  uint64_t gatewaySessionId;
};

inline bool readMetricLiteral(const char*& cursor, const char* literal) {
  const size_t length = std::strlen(literal);
  if (std::strncmp(cursor, literal, length) != 0) return false;
  cursor += length;
  return true;
}

inline bool readMetricUnsigned(const char*& cursor, uint32_t max, uint32_t& value) {
  if (*cursor < '1' || *cursor > '9') return false;
  value = 0;
  while (*cursor >= '0' && *cursor <= '9') {
    const uint32_t digit = *cursor++ - '0';
    if (value > (max - digit) / 10) return false;
    value = value * 10 + digit;
  }
  return true;
}

inline bool readMetricId(const char*& cursor, uint64_t& value) {
  value = 0;
  for (uint8_t i = 0; i < 16; ++i) {
    const char digit = *cursor++;
    if (digit >= '0' && digit <= '9') value = (value << 4) | (digit - '0');
    else if (digit >= 'a' && digit <= 'f') value = (value << 4) | (digit - 'a' + 10);
    else return false;
  }
  return true;
}

// Formato canonico de uma via. A gramática fixa rejeita chaves duplicadas e campos extras.
inline bool parseMetricCommitAck(const char* cursor, MetricCommitAck& ack) {
  uint32_t requestId, collarId;
  if (!readMetricLiteral(cursor, "[CMD] {\"schema\":\"") ||
      !readMetricLiteral(cursor, METRIC_SERIAL_SCHEMA) ||
      !readMetricLiteral(cursor, "\",\"requestId\":") ||
      !readMetricUnsigned(cursor, UINT32_MAX, requestId) ||
      !readMetricLiteral(cursor, ",\"command\":\"METRIC_COMMITTED\",\"kind\":\"")) return false;
  if (readMetricLiteral(cursor, HDOP_SERIAL_KIND)) ack.id.kind = MetricSerialKind::HDOP;
  else if (readMetricLiteral(cursor, RADIO_SERIAL_KIND)) ack.id.kind = MetricSerialKind::RADIO;
  else return false;
  if (!readMetricLiteral(cursor, "\",\"collarId\":") ||
      !readMetricUnsigned(cursor, METRIC_LAST_COLLAR_ID, collarId) ||
      !readMetricLiteral(cursor, ",\"sessionId\":\"") ||
      !readMetricId(cursor, ack.id.sessionId) ||
      !readMetricLiteral(cursor, "\",\"windowId\":\"") ||
      !readMetricId(cursor, ack.id.windowId) || ack.id.windowId == 0 ||
      !readMetricLiteral(cursor, "\",\"gatewaySessionId\":\"") ||
      !readMetricId(cursor, ack.gatewaySessionId) ||
      !readMetricLiteral(cursor, "\"}") || *cursor != '\0') return false;
  ack.id.collarId = static_cast<uint8_t>(collarId);
  return true;
}

struct MetricSerialExportCounts {
  uint64_t attempts = 0;
  uint64_t retries = 0;
  uint64_t localRefusals = 0;
  uint64_t confirmed = 0;
  uint64_t overwrittenInFlight = 0;
  uint64_t expiredTurns = 0;
};

enum class MetricSerialNotice { SENT, RETRIED, EXPIRED, OVERWRITTEN, CONFIRMED };

class MetricSerialExporter {
 public:
  using Observer = void (*)(MetricSerialNotice, const MetricSerialId&);
  MetricSerialExportCounts counts;

  template <class Io>
  void service(uint32_t now, uint64_t gatewaySessionId, bool ready, Io& io,
               Observer observe = nullptr) {
    expireTurn(now, observe);
    if (polled && now - lastPollMs < POLL_MS) return;
    polled = true;
    lastPollMs = now;
    if (backingOff && now - refusedAtMs >= LOCAL_BACKOFF_MS) backingOff = false;
    MetricSerialSummary summary;
    if (attemptsThisTurn != 0) {
      if (!io.peek(inFlight.kind, inFlight.collarId, summary) || !(inFlight == summary.id)) {
        ++counts.overwrittenInFlight;
        attemptsThisTurn = 0;
        if (observe) observe(MetricSerialNotice::OVERWRITTEN, inFlight);
      } else {
        if (attemptsThisTurn == 1 && now - turnStartedMs >= RETRY_AFTER_MS &&
            ready && !backingOff) {
          send(now, gatewaySessionId, summary, io, observe);
        }
        return;
      }
    }
    if (!ready || backingOff) return;
    const auto other = preferred == MetricSerialKind::HDOP ? MetricSerialKind::RADIO : MetricSerialKind::HDOP;
    if (!findNext(preferred, summary, io) && !findNext(other, summary, io)) return;
    // Uma recusa local tambem cede a proxima oportunidade a outro ID/tipo.
    cursor[static_cast<uint8_t>(summary.id.kind)] = summary.id.collarId;
    preferred = summary.id.kind == MetricSerialKind::HDOP ? MetricSerialKind::RADIO : MetricSerialKind::HDOP;
    send(now, gatewaySessionId, summary, io, observe);
  }

  template <class Io>
  bool confirm(uint32_t now, const MetricCommitAck& ack, uint64_t gatewaySessionId, Io& io,
               Observer observe = nullptr) {
    expireTurn(now, observe);
    if (attemptsThisTurn == 0 || ack.gatewaySessionId != gatewaySessionId ||
        !(inFlight == ack.id)) return false;
    attemptsThisTurn = 0;
    if (!io.consume(inFlight)) {
      ++counts.overwrittenInFlight;
      if (observe) observe(MetricSerialNotice::OVERWRITTEN, inFlight);
      return false;
    }
    ++counts.confirmed;
    if (observe) observe(MetricSerialNotice::CONFIRMED, inFlight);
    return true;
  }

 private:
  static constexpr uint32_t POLL_MS = 100;
  static constexpr uint32_t RETRY_AFTER_MS = 1500;
  static constexpr uint32_t TURN_TIMEOUT_MS = 4500;
  static constexpr uint32_t LOCAL_BACKOFF_MS = 500;
  MetricSerialId inFlight{};
  uint32_t lastPollMs = 0;
  uint32_t turnStartedMs = 0;
  uint32_t refusedAtMs = 0;
  uint8_t attemptsThisTurn = 0;
  uint8_t cursor[2]{};
  MetricSerialKind preferred = MetricSerialKind::HDOP;
  bool polled = false;
  bool backingOff = false;

  void expireTurn(uint32_t now, Observer observe) {
    if (attemptsThisTurn != 0 && now - turnStartedMs >= TURN_TIMEOUT_MS) {
      ++counts.expiredTurns;
      attemptsThisTurn = 0;
      if (observe) observe(MetricSerialNotice::EXPIRED, inFlight);
    }
  }

  template <class Io>
  bool findNext(MetricSerialKind kind, MetricSerialSummary& summary, Io& io) const {
    uint8_t id = cursor[static_cast<uint8_t>(kind)];
    for (uint16_t scanned = 0; scanned < METRIC_LAST_COLLAR_ID; ++scanned) {
      id = id == METRIC_LAST_COLLAR_ID ? 1 : id + 1;
      if (io.peek(kind, id, summary)) return true;
    }
    return false;
  }

  template <class Io>
  void send(uint32_t now, uint64_t gatewaySessionId, const MetricSerialSummary& summary,
            Io& io, Observer observe) {
    char line[METRIC_SERIAL_MAX_LINE + 1];
    const size_t length = formatMetricSerialLine(summary, gatewaySessionId, line);
    if (!length || !io.write(line, length)) {
      ++counts.localRefusals;
      refusedAtMs = now;
      backingOff = true;
      return;
    }
    ++counts.attempts;
    if (summary.attempted) ++counts.retries;
    io.markAttempted(summary.id);
    inFlight = summary.id;
    if (attemptsThisTurn == 0) turnStartedMs = now;
    ++attemptsThisTurn;
    if (observe) observe(summary.attempted ? MetricSerialNotice::RETRIED : MetricSerialNotice::SENT, inFlight);
  }
};

}  // namespace vfence
