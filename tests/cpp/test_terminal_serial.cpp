#include <algorithm>
#include <cassert>
#include <iostream>
#include <limits>
#include <string>

#include "terminal_serial.h"

struct SerialStub {
  size_t advertised = 0;
  size_t acceptNext = std::numeric_limits<size_t>::max();
  unsigned writes = 0;
  std::string output;

  int availableForWrite() const { return static_cast<int>(advertised); }

  size_t write(const uint8_t* data, size_t size) {
    assert(size <= advertised);
    ++writes;
    // The next write may see less space than the preceding capacity query.
    const auto accepted = std::min(size, acceptNext);
    acceptNext = std::numeric_limits<size_t>::max();
    advertised = accepted < size ? 0 : advertised - accepted;
    output.append(reinterpret_cast<const char*>(data), accepted);
    return accepted;
  }
};

using Wire = vfence::FramedSerial<SerialStub>;
using Result = vfence::SerialWriteResult;
const std::string END_FRAGMENT("\0\n", 2);
const std::string POSITION = "[APP] {\"schema\":\"vfence.gateway.serial.v1\",\"kind\":\"telemetry\",\"collarId\":7,\"sequence\":2,\"gpsValid\":false}\r\n";
const std::string RESPONSE = "[APP] {\"schema\":\"vfence.gateway.serial.v1\",\"kind\":\"command_response\",\"command\":\"GET_PROFILE\",\"requestId\":17,\"ok\":true}\r\n";
const std::string RADIO = "[APP] {\"schema\":\"vfence.gateway.serial.v1\",\"kind\":\"radio_window_summary\",\"collarId\":7}\n";
const std::string PDR = "[APP] {\"schema\":\"vfence.gateway.pdr.v1\",\"kind\":\"pdr_window_summary\",\"collarId\":7}\n";

const uint8_t* bytes(const std::string& text) {
  return reinterpret_cast<const uint8_t*>(text.data());
}

int main(int argc, char**) {
  const std::string log = "[RADIO] [COL 254]\n";
  SerialStub port;
  Wire wire(port);
  vfence::TerminalSerial terminal;
  terminal.print(wire, "%s", log.c_str());
  assert(terminal.omitted() == 1 && port.writes == 0);
  assert(!terminal.tryPrint(wire, "resumo sem espaco\n") && terminal.omitted() == 1);
  port.advertised = log.size();
  terminal.print(wire, "%s", log.c_str());
  assert(port.output == log && port.writes == 1);
  port.advertised = 1024;
  terminal.print(wire, "%0384d\n", 0);
  terminal.print(wire, "sem terminador");
  assert(terminal.omitted() == 3 && port.writes == 1);

  for (const auto cut : {size_t(0), size_t(1), log.size() / 2, log.size() - 1}) {
    SerialStub tx;
    Wire channel(tx);
    vfence::TerminalSerial diagnostics;
    tx.advertised = 1024;
    tx.acceptNext = cut;
    diagnostics.print(channel, "%s", log.c_str());
    assert(tx.output == log.substr(0, cut) && diagnostics.omitted() == 1);
    channel.queue(bytes(RESPONSE), RESPONSE.size(), true);
    channel.service();  // No room for the former [CORTE] or any terminator.
    assert(tx.writes == 1 && tx.output == log.substr(0, cut));
    if (cut) {
      tx.advertised = 2;
      tx.acceptNext = 1;  // NUL succeeds, LF is refused after the capacity check.
      channel.service();
      tx.advertised = 1;
      tx.acceptNext = 0;
      channel.service();
      diagnostics.print(channel, "[COL 001] precisa esperar\n");
      assert(tx.output == log.substr(0, cut) + std::string("\0", 1));
      tx.advertised = 1;
      channel.service();
      assert(tx.output == log.substr(0, cut) + END_FRAGMENT);
    }
    tx.advertised = 1024;
    channel.service();
    diagnostics.print(channel, "[COL 007] ok\n");
    assert(tx.output == log.substr(0, cut) + (cut ? END_FRAGMENT : "") +
                       RESPONSE + "[COL 007] ok\n");
    if (argc > 1 && cut == log.size() - 1) std::cout << tx.output;
  }

  SerialStub tx;
  Wire channel(tx);
  tx.advertised = 1024;
  tx.acceptNext = POSITION.size() - 1;
  channel.queue(bytes(POSITION), POSITION.size(), false);
  channel.queue(bytes(RESPONSE), RESPONSE.size(), true);
  tx.advertised = 1024;
  channel.service();
  tx.acceptNext = 9;
  channel.service();  // A second short write on the same queued position.
  tx.advertised = 1024;
  channel.service();
  channel.service();
  channel.service();
  assert(tx.output == POSITION.substr(0, POSITION.size() - 1) + END_FRAGMENT +
         POSITION.substr(0, 9) + END_FRAGMENT + POSITION + RESPONSE);
  assert(channel.shortWrites() == 2);
  if (argc > 1) std::cout << tx.output;

  SerialStub metricsPort;
  Wire metrics(metricsPort);
  metricsPort.advertised = 1024;
  metricsPort.acceptNext = PDR.size() / 2;
  assert(metrics.tryWrite(bytes(PDR), PDR.size()) == Result::PARTIAL);
  assert(metrics.tryWrite(bytes(RADIO), RADIO.size()) == Result::REFUSED);
  metricsPort.advertised = 1024;
  metrics.service();
  assert(metrics.tryWrite(bytes(RADIO), RADIO.size()) == Result::COMPLETE);
  assert(metricsPort.output == PDR.substr(0, PDR.size() / 2) + END_FRAGMENT + RADIO);

  SerialStub blockedPort;
  Wire blocked(blockedPort);
  for (size_t i = 0; i < Wire::FRAME_CAPACITY; ++i)
    blocked.queue(bytes(POSITION), POSITION.size(), false);
  assert(blocked.operationalRefusals() == Wire::RESERVED_FRAMES);
  for (size_t i = 0; i < Wire::RESERVED_FRAMES; ++i)
    blocked.queue(bytes(RESPONSE), RESPONSE.size(), true);
  assert(!blocked.canQueue(1) && blockedPort.writes == 0);
  for (unsigned cycle = 0; cycle < 120 && !blocked.canQueue(Wire::FRAME_CAPACITY); ++cycle) {
    blockedPort.advertised = 17;  // Normal chunked writes from a small buffer.
    blocked.service();
  }
  assert(blockedPort.output == POSITION + POSITION + POSITION + RESPONSE + RESPONSE + RESPONSE);

  const std::string longResponse = RESPONSE.substr(0, RESPONSE.size() - 2) + std::string(1024, ' ') + "\r\n";
  blockedPort.output.clear();
  blockedPort.advertised = 1024;
  blocked.queue(bytes(longResponse), longResponse.size(), true);
  assert(blockedPort.output == longResponse.substr(0, 1024));
  blockedPort.advertised = 1024;
  blocked.service();
  assert(blockedPort.output == longResponse && blocked.canQueue(Wire::FRAME_CAPACITY));
}
