#include "metric_serial_export.h"
#include "serial_command_input.h"

#include <cassert>
#include <iostream>
#include <string>
#include <vector>

using namespace vfence;

constexpr uint64_t gatewayBoot = 0x8000000000000000ULL;
constexpr uint64_t collarBoot = UINT64_MAX;
constexpr uint64_t window = UINT64_MAX - 1;

static MetricSerialSummary summary(MetricSerialKind kind, uint8_t collar, bool valid = true) {
  const bool hdop = kind == MetricSerialKind::HDOP;
  return {{hdop ? collarBoot : gatewayBoot, window, collar, kind},
          UINT32_MAX, 1250, 60000, static_cast<uint16_t>(valid ? (hdop ? 58 : 8) : 0),
          hdop ? 142 : -9340, -375, valid, valid && !hdop, false};
}

struct MemoryIo {
  std::vector<MetricSerialSummary> entries;
  std::vector<std::string> writes;
  bool writable = true;

  std::vector<MetricSerialSummary>::iterator head(MetricSerialKind kind, uint8_t collar) {
    for (auto it = entries.begin(); it != entries.end(); ++it) {
      if (it->id.kind == kind && it->id.collarId == collar) return it;
    }
    return entries.end();
  }

  bool peek(MetricSerialKind kind, uint8_t collar, MetricSerialSummary& out) {
    const auto next = head(kind, collar);
    if (next == entries.end()) return false;
    out = *next;
    return true;
  }

  bool write(const char* line, size_t size) {
    if (!writable) return false;
    writes.emplace_back(line, size);
    return true;
  }

  void markAttempted(const MetricSerialId& id) {
    const auto next = head(id.kind, id.collarId);
    assert(next != entries.end() && next->id == id);
    next->attempted = true;
  }

  bool consume(const MetricSerialId& id) {
    const auto next = head(id.kind, id.collarId);
    if (next == entries.end() || !(next->id == id)) return false;
    entries.erase(next);
    return true;
  }
};

static void emit(const MetricSerialSummary& value) {
  char line[METRIC_SERIAL_MAX_LINE + 1];
  const size_t size = formatMetricSerialLine(value, gatewayBoot, line);
  assert(size && size <= 512 && line[size - 1] == '\n' && line[size] == '\0');
  std::cout.write(line, size);
}

static void checkFairness() {
  const auto hdop = MetricSerialKind::HDOP;
  const auto radio = MetricSerialKind::RADIO;
  MemoryIo io;
  auto nextHdop = summary(hdop, 1);
  auto differentBoot = summary(hdop, 7);
  differentBoot.id.sessionId = 10;
  io.entries = {summary(hdop, 1), differentBoot, summary(hdop, 254),
                summary(radio, 1), summary(radio, 7), summary(radio, 254)};
  ++nextHdop.id.windowId;
  io.entries.push_back(nextHdop);
  const std::vector<MetricSerialId> order = {io.entries[0].id, io.entries[3].id, io.entries[1].id,
                                            io.entries[4].id, io.entries[2].id, io.entries[5].id,
                                            nextHdop.id};
  MetricSerialExporter exportState;
  uint32_t now = 0;
  for (const auto& id : order) {
    exportState.service(now, gatewayBoot, true, io);
    assert(exportState.confirm(now, {id, gatewayBoot}, gatewayBoot, io));
    assert(!exportState.confirm(now, {id, gatewayBoot}, gatewayBoot, io));
    now += 100;
  }
  assert(io.entries.empty());
  assert(io.writes.size() == order.size());
  assert(exportState.counts.confirmed == order.size());
}

static void checkRetryOverwriteAndBoot() {
  MemoryIo io;
  const auto source = summary(MetricSerialKind::HDOP, 1);
  const auto other = summary(MetricSerialKind::RADIO, 1);
  io.entries = {source, other};
  MetricSerialExporter exportState;
  const uint32_t first = UINT32_MAX - 500;
  exportState.service(first - 100, gatewayBoot, false, io);
  assert(exportState.counts.attempts == 0);
  exportState.service(first, gatewayBoot, false, io);
  assert(io.writes.empty());
  exportState.service(first + 100, gatewayBoot, true, io);
  assert(io.writes.size() == 1 && io.entries.size() == 2);
  assert(!exportState.confirm(first + 100, {source.id, gatewayBoot - 1}, gatewayBoot, io));
  assert(!exportState.confirm(first + 100, {other.id, gatewayBoot}, gatewayBoot, io));
  auto wrongId = source.id;
  ++wrongId.collarId;
  assert(!exportState.confirm(first + 100, {wrongId, gatewayBoot}, gatewayBoot, io));
  wrongId = source.id;
  --wrongId.sessionId;
  assert(!exportState.confirm(first + 100, {wrongId, gatewayBoot}, gatewayBoot, io));
  --wrongId.windowId;
  assert(!exportState.confirm(first + 100, {wrongId, gatewayBoot}, gatewayBoot, io));
  exportState.service(first + 1500, gatewayBoot, true, io);
  assert(io.writes.size() == 1);
  exportState.service(first + 1600, gatewayBoot, true, io);
  assert(io.writes.size() == 2 && io.writes.front() == io.writes.back());
  exportState.service(first + 4600, gatewayBoot, true, io);
  assert(io.writes.size() == 3 && exportState.counts.expiredTurns == 1);
  assert(!exportState.confirm(first + 4600, {source.id, gatewayBoot}, gatewayBoot, io));
  assert(exportState.confirm(first + 4600, {other.id, gatewayBoot}, gatewayBoot, io));
  exportState.service(first + 4700, gatewayBoot, true, io);
  auto replacement = source;
  ++replacement.id.windowId;
  io.entries[0] = replacement;
  assert(!exportState.confirm(first + 4700, {source.id, gatewayBoot}, gatewayBoot, io));
  assert(exportState.counts.overwrittenInFlight == 1 && io.entries.size() == 1);
  assert(exportState.counts.retries == 2);

  MetricSerialExporter newBoot;
  newBoot.service(0, gatewayBoot + 1, true, io);
  assert(!newBoot.confirm(0, {replacement.id, gatewayBoot}, gatewayBoot + 1, io));
  assert(newBoot.confirm(0, {replacement.id, gatewayBoot + 1}, gatewayBoot + 1, io));
}

static void checkCommitParser() {
  const std::string valid = "[CMD] {\"schema\":\"vfence.gateway.serial.v1\",\"requestId\":4294967295,"
      "\"command\":\"METRIC_COMMITTED\",\"kind\":\"hdop_window_summary\",\"collarId\":254,"
      "\"sessionId\":\"ffffffffffffffff\",\"windowId\":\"fffffffffffffffe\","
      "\"gatewaySessionId\":\"8000000000000000\"}";
  MetricCommitAck ack;
  assert(valid.size() + 1 <= 512 && parseMetricCommitAck(valid.c_str(), ack));
  assert(ack.id == summary(MetricSerialKind::HDOP, 254).id && ack.gatewaySessionId == gatewayBoot);
  for (size_t length = 0; length < valid.size(); ++length) {
    assert(!parseMetricCommitAck(valid.substr(0, length).c_str(), ack));
  }
  for (const auto& field : std::vector<std::pair<std::string, std::string>>{
           {"4294967295", "4294967296"}, {"4294967295", "0"},
           {"254", "255"}, {"254", "01"}, {"ffffffffffffffff", "FFFFFFFFFFFFFFFF"},
           {"fffffffffffffffe", "0000000000000000"}, {"\"collarId\":254", "\"collarId\":254,\"collarId\":254"}}) {
    auto invalid = valid;
    invalid.replace(invalid.find(field.first), field.first.size(), field.second);
    assert(!parseMetricCommitAck(invalid.c_str(), ack));
  }
  assert(!parseMetricCommitAck((valid + "{}").c_str(), ack));
}

static void checkAbsoluteTurnAndBackoff() {
  const auto h1 = summary(MetricSerialKind::HDOP, 1);
  const auto h2 = summary(MetricSerialKind::HDOP, 2);
  const auto r2 = summary(MetricSerialKind::RADIO, 2);
  for (uint32_t outage : {5000u, 20000u, 120000u}) {
    for (bool ready : {true, false}) {
      MemoryIo io;
      io.entries = {h1, h2, r2};
      MetricSerialExporter exporter;
      const uint32_t start = UINT32_MAX - 1499;
      exporter.service(start, gatewayBoot, true, io);
      io.writable = false;
      for (uint32_t elapsed = 100; elapsed <= outage; elapsed += 100) {
        exporter.service(start + elapsed, gatewayBoot, ready, io);
      }
      assert(exporter.counts.expiredTurns == 1);
      assert(exporter.counts.attempts == 1 && exporter.counts.retries == 0);
      assert(exporter.counts.localRefusals == (ready ? (outage - 1500) / 500 + 1 : 0));
      assert(io.entries.size() == 3);
      assert(!exporter.confirm(start + outage, {h1.id, gatewayBoot}, gatewayBoot, io));
    }
  }

  MemoryIo io;
  io.entries = {h1, h2, r2};
  MetricSerialExporter exporter;
  exporter.service(0, gatewayBoot, true, io);
  io.writable = false;
  exporter.service(1500, gatewayBoot, true, io);
  exporter.service(1600, gatewayBoot, true, io);
  assert(exporter.counts.localRefusals == 1);
  io.writable = true;
  exporter.service(4400, gatewayBoot, true, io);
  assert(io.writes.size() == 2 && io.writes.front() == io.writes.back());
  assert(!exporter.confirm(4500, {h1.id, gatewayBoot}, gatewayBoot, io));
  assert(exporter.counts.expiredTurns == 1 && io.entries.size() == 3);
  exporter.service(4500, gatewayBoot, true, io);
  assert(!exporter.confirm(4500, {h1.id, gatewayBoot}, gatewayBoot, io));
  assert(exporter.confirm(4500, {r2.id, gatewayBoot}, gatewayBoot, io));
  exporter.service(4600, gatewayBoot, true, io);
  assert(exporter.confirm(4601, {h2.id, gatewayBoot}, gatewayBoot, io));
  exporter.service(4700, gatewayBoot, true, io);
  assert(io.writes.back() == io.writes.front() && exporter.counts.retries == 2);
  assert(exporter.confirm(6300, {h1.id, gatewayBoot}, gatewayBoot, io));
  assert(io.entries.empty() && exporter.counts.attempts == 5 && exporter.counts.confirmed == 3);
}

static SerialCommandInput::Result feed(SerialCommandInput& input, const std::string& bytes) {
  auto result = SerialCommandInput::Result::INCOMPLETE;
  for (const auto byte : bytes) result = input.feed(static_cast<uint8_t>(byte));
  return result;
}

static void checkSerialCommandInput() {
  using Result = SerialCommandInput::Result;
  SerialCommandInput input;
  assert(feed(input, "PING 1 88") == Result::INCOMPLETE);
  assert(feed(input, "\r\n") == Result::LINE && std::strcmp(input.line(), "PING 1 88") == 0);
  assert(feed(input, "STATUS\n") == Result::LINE && std::strcmp(input.line(), "STATUS") == 0);
  assert(feed(input, "[CMD] {\"requestId\":17,") == Result::INCOMPLETE);
  assert(feed(input, std::string("\0\n", 2)) == Result::DISCARDED);
  // Mesmo um objeto completo sem o LF anterior deve ser descartado ao ressincronizar.
  assert(feed(input, "[CMD] {\"requestId\":17}") == Result::INCOMPLETE);
  assert(feed(input, std::string("\0\n", 2)) == Result::DISCARDED);
  assert(feed(input, std::string(512, 'a') + "\r\n") == Result::LINE);
  assert(feed(input, std::string(513, 'a') + "\n") == Result::TOO_LONG);
  assert(feed(input, std::string(600, 'a') + "\n") == Result::TOO_LONG);
  assert(feed(input, "RADIO\n") == Result::LINE && std::strcmp(input.line(), "RADIO") == 0);

  uint32_t request = 0;
  const GatewayCommandJson valid(R"({"note":"\"requestId\":17,\"command\":\"PING\"","info":[{"requestId":99},true,null],"command":"GET_STATUS","requestId":2})");
  assert(valid.isValid() && valid.unsignedField("requestId", request, UINT32_MAX) && request == 2);
  const char* command = nullptr;
  size_t length = 0;
  assert(valid.stringField("command", command, length) && std::string(command, length) == "GET_STATUS");
  for (const auto* invalid : {
           R"({"requestId":17,[CMD] {"requestId":2,"command":"GET_STATUS"})",
           R"({"requestId":17}{"requestId":2})", R"({"requestId":2} trailing)",
           R"({"requestId":17,"requestId":2})", R"({"requestId":17,"\u0072equestId":2})",
           R"({"command":"PING","command":"GET_RADIO"})", R"({"schema":"a","schema":"b"})",
           R"({"requestId":2,})", R"({"requestId":01})", R"({"note":"\u017Z"})",
           R"({"note":[2,]})", R"({"requestId":2,"note":[[[]]})", R"([{"requestId":2}])"}) {
    assert(!GatewayCommandJson(invalid).isValid());
  }
  for (const auto* invalid : {R"({"requestId":4294967296})", R"({"requestId":"2"})",
           R"({"requestId":1.0})", R"({"requestId":-1})", R"({"info":{"requestId":2}})",
           R"({"note":"\"requestId\":2"})"}) {
    const GatewayCommandJson json(invalid);
    assert(json.isValid() && !json.unsignedField("requestId", request, UINT32_MAX));
  }
  assert(!GatewayCommandJson("{\"note\":\"\xC0\xAF\"}").isValid());
  assert(!GatewayCommandJson("{\"note\":\"\xF0").isValid());
  assert(GatewayCommandJson("{\"note\":\"\xC3\xA7\"}").isValid());
  const GatewayCommandJson fence(R"({"requestId":4294967295,"command":"FENCE_POINT","index":2,"latitude":-3.1e1,"longitude":-54.2})");
  double latitude = 0;
  assert(fence.isValid() && fence.unsignedField("requestId", request, UINT32_MAX) && request == UINT32_MAX);
  assert(fence.doubleField("latitude", latitude) && latitude == -31);
}

int main(int argc, char** argv) {
  if (argc == 1) {
    checkFairness();
    checkRetryOverwriteAndBoot();
    checkCommitParser();
    checkAbsoluteTurnAndBackoff();
    checkSerialCommandInput();
    std::cout << sizeof(MetricSerialExporter) << '\n';
  } else if (std::string(argv[1]) == "frames") {
    emit(summary(MetricSerialKind::HDOP, 1));
    emit(summary(MetricSerialKind::HDOP, 254, false));
    emit(summary(MetricSerialKind::RADIO, 254));
    emit(summary(MetricSerialKind::RADIO, 1, false));
    auto zero = summary(MetricSerialKind::RADIO, 7);
    zero.meanX100 = zero.snrX100 = 0;
    emit(zero);
    zero = summary(MetricSerialKind::HDOP, 7);
    zero.meanX100 = 0;
    emit(zero);
  } else if (std::string(argv[1]) == "commands") {
    SerialCommandInput input;
    char byte;
    while (std::cin.get(byte)) {
      if (input.feed(static_cast<uint8_t>(byte)) != SerialCommandInput::Result::LINE) continue;
      const std::string line = input.line();
      if (line.compare(0, 6, "[CMD] ") != 0) continue;
      const GatewayCommandJson json(line.c_str() + 6);
      uint32_t id;
      const char* command;
      size_t length;
      if (json.unsignedField("requestId", id, UINT32_MAX) && id &&
          json.stringField("command", command, length)) {
        std::cout << id << ' ' << std::string(command, length) << '\n';
      } else {
        std::cout << "invalid\n";
      }
    }
  } else {
    MemoryIo io;
    MetricSerialExporter exportState;
    io.entries = {summary(MetricSerialKind::HDOP, 1)};
    exportState.service(0, gatewayBoot, true, io);
    exportState.service(1500, gatewayBoot, true, io);
    assert(io.writes.size() == 2 && io.entries.size() == 1);
    std::cout << io.writes[0] << io.writes[1] << std::flush;
    SerialCommandInput input;
    char byte;
    bool received = false;
    MetricCommitAck ack;
    while (std::cin.get(byte)) {
      if (input.feed(static_cast<uint8_t>(byte)) == SerialCommandInput::Result::LINE &&
          parseMetricCommitAck(input.line(), ack)) {
        received = true;
        break;
      }
    }
    assert(received);
    assert(exportState.confirm(1500, ack, gatewayBoot, io) && io.entries.empty());
  }
}
