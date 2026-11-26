#include <cassert>
#include <cstring>
#include <iostream>

#include "pdr_serial.h"
#include "profile_plan.h"
#include "profile_transition_model.h"

using namespace vfence;

static void check(const PdrWindowResult& result, unsigned rx, unsigned tx, unsigned percentX100) {
  assert(result.reason == PdrReason::OK && result.hasSession && result.profileId == 0);
  assert(result.received == rx && result.estimatedTx == tx && result.pdrX100 == percentX100);
}

static void pdrCases() {
  LegacyPdrSlots slots;
  assert(!slots.acquire(0) && !slots.acquire(255));
  assert(!slots.find(1) && !slots.find(254));
  for (uint8_t id = 1; id <= 16; ++id) {
    auto* slot = slots.acquire(id);
    assert(slot && slots.acquire(id) == slot && !slot->tracker.known());
    assert(slot->tracker.observe({9, 10, 0}, 10, 0));
  }
  auto* highId = slots.acquire(254);
  assert(highId && highId != slots.find(1) && highId->tracker.observe({9, 10, 0}, 10, 0));
  assert(!slots.acquire(1)->tracker.observe({9, 10, 0}, 20, 0));
  assert(!slots.find(17));

  PositionPdr collars[255];
  const auto rx = [&](unsigned id, uint32_t sequence, uint64_t session = 9, uint32_t now = 10) {
    return collars[id].observe({session, sequence, 0}, now, 0);
  };
  assert(rx(1, 10) && rx(7, 21) && rx(254, 50));
  assert(rx(1, 11) && rx(7, 23) && rx(254, 58));
  assert(!rx(1, 11) && !rx(7, 21));
  check(collars[1].closeWindow(), 2, 2, 10000);
  check(collars[7].closeWindow(), 2, 3, 6667);
  check(collars[254].closeWindow(), 2, 9, 2222);
  assert(!rx(7, 22));  // The preceding window is immutable.
  assert(!rx(7, 23));
  assert(rx(7, 24) && rx(7, 26) && rx(7, 25) && !rx(7, 25));
  check(collars[7].closeWindow(), 3, 3, 10000);
  assert(collars[7].closeWindow().reason == PdrReason::NO_SAMPLES);
  assert(collars[7].closeWindow().received == 0);
  assert(rx(7, 5000, 9, 130000));  // Long silence reestablishes the reference.
  assert(collars[7].closeWindow().reason == PdrReason::ONE_SAMPLE);

  assert(rx(8, UINT32_MAX - 1, 1, UINT32_MAX - 20));
  assert(rx(8, 0, 1, 5));
  assert(rx(8, UINT32_MAX, 1, 10));
  check(collars[8].closeWindow(), 3, 3, 10000);
  assert(rx(9, UINT32_MAX - 1) && rx(9, 1));
  check(collars[9].closeWindow(), 2, 4, 5000);
  assert(rx(10, 0) && rx(10, 63));
  check(collars[10].closeWindow(), 2, 64, 313);
  assert(rx(11, 0) && rx(11, 64));
  auto bad = collars[11].closeWindow();
  assert(bad.reason == PdrReason::DISCONTINUITY && bad.estimatedTx == 0 && bad.received == 2);
  assert(rx(12, 150));
  assert(!rx(12, 150u + 0x80000000UL));
  assert(rx(12, 151));
  check(collars[12].closeWindow(), 2, 2, 10000);

  assert(rx(13, 800, 91) && rx(13, 802, 91) && !rx(13, 1, 92));
  assert(!rx(13, 803, 91));  // Track incumbent liveness without using an ambiguous window.
  bad = collars[13].closeWindow();
  assert(bad.reason == PdrReason::SESSION_CHANGED && !bad.hasSession && bad.received == 2);
  assert(rx(13, 804, 91) && rx(13, 806, 91));
  check(collars[13].closeWindow(), 2, 3, 6667);

  assert(rx(14, 1));
  assert(collars[14].observe({9, 2, 1}, 20, 1));
  bad = collars[14].closeWindow();
  assert(bad.reason == PdrReason::PROFILE_CHANGED && bad.profileId == PDR_UNKNOWN_PROFILE);
  collars[14].profileChanged();
  assert(rx(14, 3) && rx(14, 4));
  assert(collars[14].closeWindow().reason == PdrReason::PROFILE_CHANGED);
}

static void sessionCases() {
  PositionPdr pdr;
  const auto rx = [&](uint64_t id, uint32_t ordinal, uint32_t now) {
    return pdr.observe({id, ordinal, 0}, now, 0);
  };
  assert(rx(900, 30, 1) && rx(900, 31, 2));
  check(pdr.closeWindow(), 2, 2, 10000);
  assert(!rx(900, 31, 120002) && !rx(900, 30, 120003));
  assert(pdr.closeWindow().reason == PdrReason::NO_SAMPLES);

  uint32_t now = 120004;
  // IDs deliberately have no numeric boot ordering. Exercise more changes
  // than the formerly retained two IDs, and confirm only after silence.
  for (const uint64_t session : {44, 1000, 7, 63}) {
    assert(!rx(session, 1, now));
    assert(!rx(session, 1, now + 1));   // Candidate duplicate is not proof.
    assert(!rx(session, 0, now + 2));   // Nor is an older ordinal.
    assert(!rx(session, 2, now + 3));   // Confirmation window is still invalid.
    assert(!rx(session, 3, now + 4));   // No mixed or provisional observations.
    const auto transition = pdr.closeWindow();
    assert(transition.reason == PdrReason::SESSION_CHANGED && !transition.hasSession);
    assert(transition.received == 0 && transition.estimatedTx == 0);
    assert(!rx(session, 3, now + 5));
    assert(rx(session, 4, now + 6) && rx(session, 6, now + 7));
    const auto measured = pdr.closeWindow();
    check(measured, 2, 3, 6667);
    assert(measured.collarSession == session);
    now += PDR_REFERENCE_TIMEOUT_MS + 10;
  }

  // Replaying an old boot, even with increasing ordinals after silence,
  // neither promotes it nor invalidates the proven incumbent.
  assert(!rx(900, 601, now) && !rx(900, 602, now + 1));
  assert(!rx(44, 500, now + 2) && !rx(1000, 505, now + 3) && !rx(7, 509, now + 4));
  assert(pdr.closeWindow().reason == PdrReason::NO_SAMPLES);
  assert(rx(63, 7, now + 5) && rx(63, 8, now + 6));
  check(pdr.closeWindow(), 2, 2, 10000);

  // A lone unknown boot never displaces a progressing incumbent.
  assert(!rx(1234, 1, now + 7));
  assert(!rx(63, 9, now + 8));
  assert(pdr.closeWindow().reason == PdrReason::SESSION_CHANGED);
  assert(rx(63, 10, now + 9) && rx(63, 11, now + 10));
  check(pdr.closeWindow(), 2, 2, 10000);

  PositionPdr wrapped;
  assert(wrapped.observe({81, 1, 0}, UINT32_MAX - 500, 0));
  assert(wrapped.closeWindow().reason == PdrReason::ONE_SAMPLE);
  assert(!wrapped.observe({88, UINT32_MAX, 0}, 120000, 0));
  assert(!wrapped.observe({88, 0, 1}, 120001, 0));
  assert(!wrapped.observe({88, 0, 0}, 120002, 0));
  assert(wrapped.closeWindow().reason == PdrReason::SESSION_CHANGED);
  assert(wrapped.observe({88, 1, 0}, 120010, 0));
  assert(wrapped.observe({88, 2, 0}, 120015, 0));
  check(wrapped.closeWindow(), 2, 2, 10000);
}

static void minuteCadenceCases() {
  // Exhaust every five-minute loss pattern for two independent collars.
  // At one counted position per minute, neither can form an observed interval.
  for (unsigned first = 0; first < 32; ++first) {
    for (unsigned second = 0; second < 32; ++second) {
      PositionPdr collars[2];
      const unsigned deliveries[] = {first, second};
      for (unsigned minute = 0; minute < 5; ++minute) {
        for (unsigned id = 0; id < 2; ++id) {
          const bool received = (deliveries[id] & (1u << minute)) != 0;
          if (received) {
            const PositionDeliveryTag tag{id + 1, minute + 1, 0};
            assert(collars[id].observe(tag, minute * PDR_WINDOW_MS + 500, 0));
            assert(!collars[id].observe(tag, minute * PDR_WINDOW_MS + 501, 0));
          }
          const auto result = collars[id].closeWindow();
          assert(result.reason == (received ? PdrReason::ONE_SAMPLE : PdrReason::NO_SAMPLES));
          assert(result.received == unsigned(received) && result.estimatedTx == 0);
        }
      }
    }
  }

  PositionPdr tracker;
  assert(tracker.observe({1, 10, 0}, 100, 0));
  assert(tracker.closeWindow().reason == PdrReason::ONE_SAMPLE);
  assert(tracker.observe({1, 11, 0}, 60100, 0));
  assert(!tracker.observe({1, 10, 0}, 60101, 0)); // Late after the first window closed.
  assert(tracker.observe({1, 13, 0}, 60200, 0));
  check(tracker.closeWindow(), 2, 3, 6667);

  // The same wall-clock loss on a fast link is measurable between anchors.
  PositionPdr good, degraded;
  for (unsigned minute = 0; minute < 5; ++minute) {
    const auto time = minute * PDR_WINDOW_MS;
    const auto sequence = minute * 3;
    for (unsigned i = 0; i < 3; ++i) {
      assert(good.observe({1, sequence + i, 0}, time + i * 10000, 0));
      if (i != 1) assert(degraded.observe({2, sequence + i, 0}, time + i * 10000, 0));
    }
    check(good.closeWindow(), 3, 3, 10000);
    check(degraded.closeWindow(), 2, 3, 6667);
  }
}

static void codecCase() {
  uint8_t original[MAX_PACKET_SIZE], wire[MAX_PACKET_SIZE], oldPayload[LEGACY_POSITION_PAYLOAD_SIZE];
  const TelemetryPayload pos{{COORD_UNAVAILABLE, COORD_UNAVAILABLE}, U16_UNAVAILABLE,
      U8_UNAVAILABLE, U8_UNAVAILABLE, U16_UNAVAILABLE, 0, 7};
  assert(encodeTelemetry(original, sizeof(original), 254, 65535, pos) == 23);
  const PositionDeliveryTag tag{UINT64_C(0x123456789abcdef0), 0x76543210, 0};
  assert(!encodeCountedPosition(wire, 23, 254, 1, pos, tag));
  assert(encodeCountedPosition(wire, sizeof(wire), 254, 65535, pos, tag) == 36);
  assert(wire[23] == 0x12 && wire[30] == 0xf0 && wire[31] == 0x76 && wire[34] == 0x10);
  PacketView frame{}, decoded{};
  PositionDeliveryTag decodedTag{};
  assert(decodePacket(wire, 36, frame));
  assert(unpackCountedPosition(frame, decodedTag, decoded, oldPayload));
  assert(decodedTag.sessionId == tag.sessionId && decodedTag.ordinal == tag.ordinal);
  assert(decoded.header.sequence == 65535 && decoded.header.collarId == 254);
  assert(std::memcmp(oldPayload, original + HEADER_SIZE, sizeof(oldPayload)) == 0);
  TelemetryPayload unavailable;
  assert(decodeTelemetry(decoded, unavailable) && !(unavailable.stateFlags & STATE_GPS_VALID));
  wire[35] = 4;
  assert(decodePacket(wire, 36, frame) && !unpackCountedPosition(frame, decodedTag, decoded, oldPayload));
  assert(!decodePacket(wire, 35, frame));
}

static void profileCases() {
  assert(INITIAL_LORA_PROFILE == 0 && LORA_PROFILE_COUNT == 4);
  for (unsigned i = 0; i < LORA_PROFILE_COUNT; ++i) {
    assert(LORA_PROFILES[i].id == i && LORA_PROFILES[i].spreadingFactor == 7 + i);
    assert(LORA_PROFILES[i].frequencyHz == 915000000 && LORA_PROFILES[i].bandwidthHz == 125000);
  }
  ProfilePlan plan;
  assert(!plan.pending() && plan.operation() == 0);
  assert(plan.request(1, 2, 1, 0, false) == ProfileDecision::WRONG_SESSION);
  assert(plan.request(1, 1, 1, 4, false) == ProfileDecision::INVALID);
  assert(plan.request(1, 1, 1, 0, false) == ProfileDecision::UNCHANGED);
  assert(plan.request(1, 1, 2, 3, true) == ProfileDecision::BUSY);
  assert(plan.request(1, 1, 2, 3, false) == ProfileDecision::PLANNED);
  assert(plan.request(1, 1, 2, 3, false) == ProfileDecision::DUPLICATE);
  assert(plan.request(1, 1, 3, 2, false) == ProfileDecision::BUSY);
  assert(plan.request(1, 1, 1, 2, false) == ProfileDecision::STALE);
  assert(plan.cancel(1, 1, 2) == ProfileDecision::CANCELLED);
  assert(plan.cancel(1, 1, 2) == ProfileDecision::DUPLICATE);
  assert(!plan.pending());

  using Model = ProfileTransitionModel;
  using Phase = Model::State;
  using Proof = Model::Proof;
  const Model::Member peers[] = {{1, 101, true}, {254, 102, true}};
  const Model::Ticket ticket{11, 9}, old{11, 8};
  Model model;
  Model::Member offline[] = {{1, 101, true}, {254, 102, false}};
  assert(!model.start(ticket, 1, offline, 2, false));
  assert(!model.start(ticket, 1, peers, 2, true));
  assert(model.start(ticket, 1, peers, 2, false));
  assert(!model.start(ticket, 2, peers, 2, false) && !model.startTrial());
  assert(!model.confirm(old, 1, 101, Proof::READY_OLD, 0));
  assert(!model.confirm(ticket, 1, 100, Proof::READY_OLD, 0));
  assert(model.confirm(ticket, 1, 101, Proof::READY_OLD, 0));
  assert(!model.confirm(ticket, 1, 101, Proof::READY_OLD, 0));
  assert(model.confirm(ticket, 254, 102, Proof::READY_OLD, 0));
  assert(model.state() == Phase::COMMITTING && model.active() == 0);
  assert(model.confirm(ticket, 1, 101, Proof::ARMED_OLD, 0));
  assert(!model.startTrial());
  assert(model.confirm(ticket, 254, 102, Proof::ARMED_OLD, 0));
  assert(model.startTrial() && model.active() == 0);
  assert(!model.confirm(ticket, 1, 101, Proof::VERIFIED_TARGET, 0));
  assert(model.confirm(ticket, 1, 101, Proof::VERIFIED_TARGET, 1) && model.active() == 0);
  assert(model.confirm(ticket, 254, 102, Proof::VERIFIED_TARGET, 1));
  assert(model.state() == Phase::COMPLETED && model.active() == 1);

  Model prepareTimeout;
  assert(prepareTimeout.start(ticket, 2, peers, 2, false));
  assert(prepareTimeout.confirm(ticket, 1, 101, Proof::READY_OLD, 0));
  prepareTimeout.timeout();
  assert(prepareTimeout.state() == Phase::FAILED && prepareTimeout.active() == 0);

  Model rollback;
  assert(rollback.start(ticket, 3, peers, 2, false));
  for (const auto& peer : peers) {
    assert(rollback.confirm(ticket, peer.id, peer.boot, Proof::READY_OLD, 0));
  }
  for (const auto& peer : peers) {
    assert(rollback.confirm(ticket, peer.id, peer.boot, Proof::ARMED_OLD, 0));
  }
  assert(rollback.startTrial());
  rollback.timeout();
  assert(rollback.state() == Phase::ROLLING_BACK);
  rollback.recoverAtBoot(12);
  assert(!rollback.confirm(ticket, 1, 101, Proof::RESTORED_OLD, 0));
  assert(rollback.reacquire(254, 103, 0));
  assert(!rollback.confirm({12, 9}, 254, 102, Proof::RESTORED_OLD, 0));
  assert(rollback.confirm({12, 9}, 1, 101, Proof::RESTORED_OLD, 0));
  rollback.timeout();
  assert(rollback.state() == Phase::ROLLING_BACK && rollback.rollbackUnverified());
  assert(rollback.confirm({12, 9}, 254, 103, Proof::RESTORED_OLD, 0));
  assert(rollback.state() == Phase::FAILED && rollback.active() == 0);
}

int main() {
  minuteCadenceCases();
  pdrCases();
  sessionCases();
  codecCase();
  profileCases();
  PositionPdr sample;
  sample.observe({10, 0, 0}, 0, 0);
  sample.observe({10, 2, 0}, 10000, 0);
  char line[512];
  assert(formatPdrReport({sample.closeWindow(), 1, 0}, 11, 254, line));
  std::cout << line;
  assert(formatPdrReport({sample.closeWindow(), 2, 60000}, 11, 254, line));
  std::cout << line;
}
