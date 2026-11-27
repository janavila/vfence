#include <cassert>
#include <deque>
#include <iostream>
#include <string>

#include "metric_serial_export.h"
#include "metric_evidence_store.h"
#include "serial_command_input.h"

using namespace vfence;

static constexpr uint64_t radioSessionId = 0x1234, boot = 0x4321, evidenceEpochFirst = 1;
static constexpr uint8_t collar = 1;
static constexpr bool PROVEN_METRICS_ENABLED = true;
static uint64_t radioNextWindowId = 9;
static uint32_t radioWindowStartMs = 160000;
static struct { uint8_t active = 0; } radioTransition;
static GatewayMetricEvidence metricEvidence;
static std::string response;

static void emitCommandResponse(uint32_t, const char*, bool, const char* detail) {
  response = detail;
}

// The Python fixture copies the production window lookup and CMD dispatch here.
#include "evidence_dispatch.inc"

static void receiveSerial() {
  SerialCommandInput input;
  char byte;
  while (std::cin.get(byte)) {
    const auto result = input.feed(static_cast<uint8_t>(byte));
    if (result == SerialCommandInput::Result::INCOMPLETE) continue;
    assert(result == SerialCommandInput::Result::LINE);
    const std::string line(input.line());
    assert(line.compare(0, 6, "[CMD] ") == 0);
    processJsonCommand(line);
    return;
  }
  assert(false);
}

static MetricProof overRadio(const MetricProof& proof, bool ack) {
  uint8_t packet[MAX_PACKET_SIZE];
  const auto size = encodeMetricProof(packet, sizeof(packet), collar, 1, proof, ack);
  PacketView view;
  MetricProof received{};
  assert(size && decodePacket(packet, size, view) && decodeMetricProof(view, received, ack));
  return received;
}

int main() {
  CollarMetricClock clock;
  std::deque<MetricProof> proofs;
  assert(clock.observe({radioSessionId, 8, 100000, 0}, 110000, 5000, 5100, 0));
  assert(metricEvidence.admit(collar, 0));
  auto enqueue = [&](const MetricProof& proof) { proofs.push_back(proof); };
  uint32_t tx = 100, localStart = 55050;
  clock.service(localStart, 0, boot, tx, enqueue);
  for (unsigned minute = 0; minute < 5; ++minute) {
    const MetricWindow window = *clock.window();
    assert(window.id == 9 + minute);
    assert(clock.observe(window, window.start + 2000, localStart + 1950, localStart + 2050, 0));
    clock.hdop(localStart + 1, 100 + minute);
    clock.hdop(localStart + 1001, 102 + minute);
    auto* slot = metricEvidence.slot(collar, window, boot, true);
    assert(slot);
    slot->rx.observe(++tx, true, -9400, 600);
    slot->rx.observe(++tx, true, -9500, 601);
    localStart += 60000;
    clock.service(localStart, 0, boot, tx, enqueue);
    assert(proofs.size() == 1 && proofs.front().window.id == window.id);
    radioNextWindowId = window.id + 1;
    radioWindowStartMs = window.start + 60000;
    const auto received = overRadio(proofs.front(), false);
    assert(slot->retain(received));
    char frame[512];
    assert(formatEvidence(*slot, collar, frame));
    slot->sent = true;
    std::cout << frame << std::flush;
    if (!minute) {
      // Four wrong identities, followed by a second APP export after the first ACK is lost.
      for (unsigned i = 0; i < 4; ++i) {
        receiveSerial();
        assert(response.empty() && !slot->committed && !slot->ackPending);
        std::cout << "REJECTED\n" << std::flush;
      }
      std::cout << frame << std::flush;
    }
    receiveSerial();
    assert(response.empty() && slot->committed && slot->ackPending);
    if (!minute) {
      // A locally transmitted LoRa ACK can still be lost. A repeat of the proof rearms it.
      slot->ackPending = false;
      assert(slot->retain(overRadio(proofs.front(), false)) && slot->ackPending && proofs.size() == 1);
    }
    const auto ack = overRadio(slot->proof(), true);
    const auto& head = proofs.front();
    assert(ack.boot == head.boot && ack.window.gateway == head.window.gateway && ack.window.id == head.window.id);
    proofs.pop_front();
    slot->ackPending = false;
    std::cout << "CONSUMED " << window.id << '\n' << std::flush;
  }
  assert(proofs.empty());
}
