#include <cassert>

#include "profile_transition.h"

using namespace vfence;

struct Network {
  GatewayProfileTransition gateway;
  CollarProfileTransition collars[2];
  uint32_t now = 0;
  bool online[2] = {true, true};
  bool dropRestored[2] = {};
  bool denyVerify = false, failApply = false;
  bool gatewayBusy = false, failRecovery = false;
  uint8_t physical = 0;
  unsigned recoveryAttempts = 0;

  void tick() {
    for (auto& device : collars) device.service(now, false, [](uint8_t) { return true; });
    gateway.service(now, gatewayBusy, [this](uint8_t id, const ProfileSignal& input) {
      auto& device = collars[id - 1];
      if (online[id - 1] && device.active == physical) {
        device.receive(input, id + 100, now, false, [](const ProfileSignal&) { return true; },
            [this, id](const ProfileSignal& reply) {
              if ((denyVerify && reply.step == ProfileStep::VERIFIED) ||
                  (dropRestored[id - 1] && reply.step == ProfileStep::RESTORED)) return;
              gateway.receive(id, reply, now);
            });
      }
    }, [this](uint8_t profile) {
      if (profile && failApply) return false;
      if (profile == INITIAL_LORA_PROFILE && gateway.phase == ProfilePhase::ROLLING_BACK) {
        ++recoveryAttempts;
        if (failRecovery) return false;
      }
      physical = profile;
      return true;
    });
    now += 100;
  }

  void waitFor(ProfilePhase phase) {
    const auto until = now + 300000;
    while (gateway.phase != phase && now < until) tick();
    assert(gateway.phase == phase);
  }

  void start(uint32_t operation, uint8_t profile) {
    const uint8_t roster[] = {1, 2};
    assert(gateway.start(99, operation, profile, roster, 2, now));
  }

  void rebootGateway() {
    const auto recorded = gateway;
    gateway = GatewayProfileTransition{};
    gateway.session = recorded.session + 1;
    gateway.operation = recorded.operation;
    gateway.source = recorded.source;
    gateway.target = recorded.target;
    gateway.count = recorded.count;
    for (uint8_t i = 0; i < gateway.count; ++i) gateway.members[i].id = recorded.members[i].id;
    physical = INITIAL_LORA_PROFILE;
    gateway.recover(now);
  }
};

static void rollbackWaitsForApplicationAndBothProofs() {
  Network n;
  n.denyVerify = true;
  n.start(1, 3);
  n.waitFor(ProfilePhase::VERIFYING);
  assert(n.gateway.target == 3 && n.gateway.active == 3 && n.physical == 3);
  n.gatewayBusy = true;
  n.waitFor(ProfilePhase::ROLLING_BACK);
  const uint32_t deadline = n.gateway.deadline;
  while (!profileDue(n.now, deadline)) n.tick();
  n.tick();
  assert(n.gateway.recoveryOverdue() && n.gateway.busy() && n.recoveryAttempts == 0);
  assert(n.gateway.active == 3 && n.physical == 3 && n.gateway.target == 3);
  n.gateway.receive(1, {ProfileStep::RESTORED, 99, 1, 101, 0, 0, 3}, n.now);
  assert(!n.gateway.members[0].proved);

  n.gatewayBusy = false;
  n.failRecovery = true;
  for (int i = 0; i < 40; ++i) n.tick();
  assert(n.recoveryAttempts == 4 && n.gateway.active == 3 && n.physical == 3);
  assert(n.gateway.phase == ProfilePhase::ROLLING_BACK && n.gateway.recoveryOverdue());
  const uint8_t roster[] = {1, 2};
  assert(!n.gateway.start(99, 2, 0, roster, 2, n.now));

  n.failRecovery = false;
  n.dropRestored[1] = true;
  for (int i = 0; i < 50; ++i) n.tick();
  assert(n.gateway.active == 0 && n.physical == 0 && n.gateway.target == 3);
  assert(n.gateway.members[0].proved && !n.gateway.members[1].proved);
  assert(n.gateway.phase == ProfilePhase::ROLLING_BACK && n.gateway.recoveryOverdue());
  n.gateway.receive(2, {ProfileStep::RESTORED, 98, 1, 102, 0, 0, 3}, n.now);
  n.gateway.receive(2, {ProfileStep::RESTORED, 99, 2, 102, 0, 0, 3}, n.now);
  assert(!n.gateway.members[1].proved);
  n.dropRestored[1] = false;
  n.waitFor(ProfilePhase::FAILED);
  assert(!n.gateway.busy() && !n.gateway.recoveryOverdue() && n.gateway.active == 0);
  assert(n.gateway.members[0].proved && n.gateway.members[1].proved);
}

static void recoveredCollarCannotStillBeArmed() {
  CollarProfileTransition collar;
  ProfileSignal request{ProfileStep::PREPARE, 99, 1, 101, 0, 0, 3};
  ProfileStep reply = ProfileStep::PREPARE;
  auto save = [](const ProfileSignal&) { return true; };
  auto receive = [&](const ProfileSignal& received) { reply = received.step; };
  collar.receive(request, 101, 0, false, save, receive);
  assert(reply == ProfileStep::READY);
  request.step = ProfileStep::ARM;
  request.waitMs = 2000;
  collar.receive(request, 101, 100, false, save, receive);
  assert(reply == ProfileStep::ARMED);
  request = {ProfileStep::RECOVER, 100, 2, 0, 0, 0, 3};
  collar.receive(request, 101, 200, false, save, receive);
  assert(reply == ProfileStep::ARMED);
  request.operation = 1;
  collar.receive(request, 101, 300, false, save, receive);
  assert(reply == ProfileStep::RESTORED);
  collar.service(2500, false, [](uint8_t) { assert(false); return false; });
  assert(collar.active == INITIAL_LORA_PROFILE);
}

static void collarRetriesLeaseRecoveryWhenRadioBecomesFree() {
  CollarProfileTransition collar;
  ProfileSignal request{ProfileStep::PREPARE, 99, 1, 101, 0, 0, 3};
  const auto save = [](const ProfileSignal&) { return true; };
  const auto reply = [](const ProfileSignal&) {};
  collar.receive(request, 101, 0, false, save, reply);
  request.step = ProfileStep::ARM;
  request.waitMs = 2000;
  collar.receive(request, 101, 0, false, save, reply);
  collar.service(2000, false, [](uint8_t profile) { return profile == 3; });
  assert(collar.active == 3);
  unsigned attempts = 0;
  auto apply = [&](uint8_t profile) {
    assert(profile == INITIAL_LORA_PROFILE);
    return ++attempts == 2;
  };
  const auto expiry = 2000 + PROFILE_LEASE_MS;
  collar.service(expiry, true, apply);
  assert(collar.active == 3 && attempts == 0);
  collar.service(expiry + 1, false, apply);
  assert(collar.active == 3 && attempts == 1);
  collar.service(expiry + 2, false, apply);
  assert(collar.active == INITIAL_LORA_PROFILE && attempts == 2);
}

int main() {
  rollbackWaitsForApplicationAndBothProofs();
  recoveredCollarCannotStillBeArmed();
  collarRetriesLeaseRecoveryWhenRadioBecomesFree();

  Network network;
  uint32_t operation = 0;
  for (uint8_t profile = 1; profile <= 3; ++profile) {
    network.start(++operation, profile);
    const uint8_t roster[] = {1, 2};
    assert(!network.gateway.start(99, operation + 1, 0, roster, 2, network.now));
    network.waitFor(ProfilePhase::COMPLETED);
    assert(network.physical == profile && network.collars[0].active == profile && network.collars[1].active == profile);
    network.start(++operation, 0);
    network.waitFor(ProfilePhase::COMPLETED);
    assert(network.physical == 0 && network.collars[0].active == 0 && network.collars[1].active == 0);
  }

  Network silent;
  silent.online[1] = false;
  silent.start(1, 3);
  silent.waitFor(ProfilePhase::FAILED);
  assert(silent.physical == 0 && silent.collars[0].active == 0 && silent.collars[1].active == 0);

  Network lostProof;
  lostProof.denyVerify = true;
  lostProof.start(1, 2);
  lostProof.waitFor(ProfilePhase::VERIFYING);
  // Lose contact after entering the destination so leases also stop renewing.
  lostProof.online[1] = false;
  lostProof.waitFor(ProfilePhase::ROLLING_BACK);
  const auto deadline = lostProof.gateway.deadline;
  while (!profileDue(lostProof.now, deadline)) lostProof.tick();
  lostProof.tick();
  assert(lostProof.gateway.recoveryOverdue() && lostProof.gateway.active == 0);
  assert(!lostProof.gateway.members[1].proved);
  lostProof.online[1] = true;
  lostProof.waitFor(ProfilePhase::FAILED);
  assert(lostProof.physical == 0 && lostProof.collars[0].active == 0 && lostProof.collars[1].active == 0);

  Network restart;
  restart.start(1, 1);
  restart.waitFor(ProfilePhase::VERIFYING);
  restart.rebootGateway();
  restart.gateway.receive(1, {ProfileStep::RESTORED, 99, 1, 101, 0, 0, 1}, restart.now);
  assert(!restart.gateway.members[0].proved);
  restart.waitFor(ProfilePhase::FAILED);
  assert(restart.collars[0].active == 0 && restart.collars[1].active == 0);

  Network radioFailed;
  radioFailed.failApply = true;
  radioFailed.start(1, 3);
  radioFailed.waitFor(ProfilePhase::FAILED);
  assert(radioFailed.physical == 0 && radioFailed.collars[0].active == 0 && radioFailed.collars[1].active == 0);

  uint8_t bytes[MAX_PACKET_SIZE];
  ProfileSignal packet{ProfileStep::ARM, 0x1234567890ULL, 9, 0x1234567891ULL, 12000, 0, 3};
  const auto count = encodeProfileSignal(bytes, sizeof(bytes), 2, 42, packet);
  PacketView view{};
  assert(decodePacket(bytes, count, view));
  ProfileSignal decoded{};
  assert(decodeProfileSignal(view, decoded));
  assert(decoded.gateway == packet.gateway && decoded.collarBoot == packet.collarBoot && decoded.waitMs == packet.waitMs);
}
