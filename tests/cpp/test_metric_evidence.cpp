#include <cassert>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include "metric_evidence_store.h"

using namespace vfence;

static void receptionCases() {
  const MetricWindow window{1, 2, 60000, 0};
  const MetricProof zero{window, 3, 4, 4, 0xffff, 0};
  ExactReception empty;
  assert(empty.proves(zero));
  assert(empty.proves(MetricProof{window, 3, 4, 14, 0xffff, 0}));
  assert(!empty.proves(MetricProof{window, 3, 4, 517, 0xffff, 0}));
  ExactReception received;
  for (uint32_t n : {7u, 5u, 5u, 6u}) received.observe(n, true, -9000, 800);
  assert(received.seen(5) && received.count == 3 && received.radioCount == 3);
  assert(received.proves(MetricProof{window, 3, 4, 8, 0xffff, 0}));
  assert(!received.proves(MetricProof{window, 3, 5, 8, 0xffff, 0}));
  received.observe(600, true, -9000, 800);
  assert(!received.proves(MetricProof{window, 3, 4, 8, 0xffff, 0}));
}

int main() {
  receptionCases();
  constexpr uint8_t collar = 2;
  constexpr uint64_t gateway = 0x1234, boot = 0x4321;
  CollarMetricClock clock;
  GatewayMetricEvidence receiver;
  assert(receiver.admit(collar, 110000));
  // At the first observation the clock round trip is 100 ms. Start the
  // following Gateway window, and refresh while that window is running.
  assert(clock.observe({gateway, 8, 100000, 1}, 110000, 5000, 5100, 1));
  assert(!clock.window());
  assert(!clock.observe({gateway, 8, 100000, 0}, 110000, 5000, 5100, 1)); // wrong profile
  bool proofClosed = false;
  MetricProof proof{};
  const auto collect = [&](const MetricProof& measured) { proof = measured; proofClosed = true; };
  uint32_t sent = 0xfffffffc;
  clock.service(55049, 1, boot, sent, collect);
  assert(!clock.window());
  clock.service(55050, 1, boot, sent, collect);
  assert(clock.window() && clock.window()->id == 9 && clock.window()->start == 160000);
  assert(clock.observe({gateway, 9, 160000, 1}, 162000, 57000, 57100, 1));
  assert(!clock.observe({gateway, 9, 160000, 1}, 162000, 57000, 62000, 1));
  clock.hdop(55051, 100);
  clock.hdop(55100, 200); // same slot
  clock.hdop(56051, 120);

  const MessageType types[] = {MessageType::TELEMETRY, MessageType::EVENT, MessageType::CONTROL,
                               MessageType::CONFIG};
  for (unsigned i = 0; i < 10; ++i) {
    uint8_t original[6];
    const Header legacy{PROTOCOL_VERSION, types[i % 4], i == 1, i == 4, i == 2, collar, 88, 1};
    assert(encodeHeader(original, sizeof(original), legacy));
    original[5] = 1;
    uint8_t encoded[EVIDENCE_PACKET_MAX];
    const auto size = wrapMetricAttempt(encoded, sizeof(encoded), original, sizeof(original),
                                         {gateway, boot, clock.window()->id, ++sent, 1});
    assert(size == HEADER_SIZE + EVIDENCE_PREFIX + sizeof(original));
    // The first and last physical attempts are lost.
    if (i == 0 || i == 9) continue;
    PacketView wrapper, recovered;
    MetricAttempt attempt{};
    assert(decodePacket(encoded, size, wrapper) && unwrapMetricAttempt(wrapper, recovered, attempt));
    assert(attempt.ordinal == sent && recovered.header.type == legacy.type &&
           recovered.header.ackReq == legacy.ackReq && recovered.header.retry == legacy.retry &&
           recovered.header.urgent == legacy.urgent);
    assert(memcmp(original, encoded + HEADER_SIZE + EVIDENCE_PREFIX, sizeof(original)) == 0);
    auto* slot = receiver.slot(collar, *clock.window(), boot, true);
    assert(slot);
    slot->rx.observe(attempt.ordinal, true, -9400, 600);
    slot->rx.observe(attempt.ordinal, true, -9400, 600);
  }
  clock.service(115050, 1, boot, sent, collect);
  assert(proofClosed && proof.window.id == 9 && proof.lastTx - proof.firstTx == 10 &&
         proof.samples == 2 && proof.hdop == 110 && clock.window()->id == 10);
  uint8_t packet[MAX_PACKET_SIZE];
  PacketView packetView;
  MetricProof decoded;
  auto size = encodeMetricProof(packet, sizeof(packet), collar, 89, proof);
  assert(size == 48 && decodePacket(packet, size, packetView) && decodeMetricProof(packetView, decoded));
  auto* slot = receiver.slot(collar, decoded.window, decoded.boot, true);
  assert(slot && slot->retain(decoded) && slot->rx.count == 8);
  assert(slot->retain(decoded) && !slot->ackPending);
  slot->committed = true;
  assert(slot->retain(decoded) && slot->ackPending); // lost ACK
  const auto saved = slot->proof();
  assert(saved.firstTx == proof.firstTx && saved.lastTx == proof.lastTx &&
         saved.hdop == proof.hdop && saved.samples == proof.samples);
  size = encodeMetricProof(packet, sizeof(packet), collar, 90, saved, true);
  assert(size == 32 && decodePacket(packet, size, packetView) && decodeMetricProof(packetView, decoded, true));
  assert(decoded.boot == boot && decoded.window.id == proof.window.id);
  char line[512];
  assert(formatEvidence(*slot, collar, line));
  std::fputs(line, stdout);
  // A proof for another boot must not overwrite the same closed window.
  auto restarted = proof;
  restarted.boot += 1;
  assert(!slot->retain(restarted));
  assert(!receiver.slot(collar, proof.window, restarted.boot, true));
  assert(!formatEvidence(*slot, collar, line));
  clock.service(147101, 1, boot, sent, collect);
  assert(!clock.window());
  assert(clock.observe({gateway + 1, 1, 0, 2}, 700, 200000, 200100, 2));
  assert(!clock.window());
}
