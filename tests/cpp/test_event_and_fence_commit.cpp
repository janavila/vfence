#include <cassert>
#include <map>
#include <string>
#include <vector>

#include "event_serial_delivery.h"
#include "fence_storage.h"
#include "pdr_serial.h"
#include "vfence_fence_transfer.h"

using namespace vfence;

struct Storage {
  std::map<std::string, std::vector<uint8_t>> memory;
  std::string stopWrite;
  std::string corruptWrite;
  std::string stopRead;

  size_t getBytesLength(const char* key) const {
    auto item = memory.find(key);
    return item == memory.end() ? 0 : item->second.size();
  }
  size_t getBytes(const char* key, void* data, size_t size) const {
    if (stopRead == key || getBytesLength(key) != size) return 0;
    std::memcpy(data, memory.at(key).data(), size);
    return size;
  }
  size_t putBytes(const char* key, const void* data, size_t size) {
    auto bytes = static_cast<const uint8_t*>(data);
    const size_t written = stopWrite == key ? size / 2 : size;
    memory[key].assign(bytes, bytes + written);
    if (corruptWrite == key) memory[key][0] ^= 0xff;
    return written;
  }
};

static FenceConfiguration fence(uint16_t version) {
  FenceConfiguration config{};
  config.version = version;
  config.count = 3;
  config.points[0] = {-310000000, -540000000};
  config.points[1] = {-310010000, -540000000};
  config.points[2] = {-310000000, -540010000 + version};
  config.parameters = {10, 30, 10, 60, 20, 5};
  return config;
}

static void fenceCommits() {
  Storage nvs;
  FenceStorage storage;
  FenceConfiguration current{}, old = fence(1), next = fence(2);
  nvs.stopWrite = "fslot_a";
  assert(!storage.commit(nvs, old) && !storage.load(nvs, current));
  nvs.stopWrite.clear();
  assert(storage.commit(nvs, old));
  assert(storage.load(nvs, current) && current.version == 1);
  nvs.stopWrite = "fslot_b";
  assert(!storage.commit(nvs, next));
  FenceStorage reboot;
  assert(reboot.load(nvs, current) && current.version == 1);
  nvs.stopWrite.clear();
  nvs.corruptWrite = "fslot_b";
  assert(!storage.commit(nvs, next));
  assert(reboot.load(nvs, current) && current.version == 1);
  nvs.corruptWrite.clear();
  nvs.stopRead = "fslot_b";
  assert(!storage.commit(nvs, next));
  nvs.stopRead.clear();
  assert(reboot.load(nvs, current) && current.version == 1);
  nvs.stopWrite = "fmark_b";
  assert(!storage.commit(nvs, next));
  assert(reboot.load(nvs, current) && current.version == 1);
  nvs.stopWrite.clear();
  next.parameters.muitoPertoM = 20;
  assert(reboot.commit(nvs, next));
  FenceStorage afterPromotion;
  assert(afterPromotion.load(nvs, current) && current.version == 2);
  assert(current.parameters.muitoPertoM == 20 && current.points[2].lon == next.points[2].lon);
  assert(computeFenceCrc32(current.version, current.points, current.count) ==
         computeFenceCrc32(next.version, next.points, next.count));
  nvs.memory["fslot_b"][10] ^= 1;
  assert(afterPromotion.load(nvs, current) && current.version == 1);

  FenceAssembler transfer;
  const auto crc = computeFenceCrc32(next.version, next.points, next.count);
  assert(transfer.begin(next.version, next.count, 2, crc));
  assert(transfer.addChunk(next.version, 0, 0, next.points, 2) == ChunkResult::OK);
  assert(transfer.addChunk(next.version, 1, 2, next.points + 2, 1) == ChunkResult::OK);
  ErrorCode code = ErrorCode::INVALID_LENGTH;
  uint16_t detail = 0;
  assert(!transfer.validateCommit(next.version, crc ^ 1, code, detail) && code == ErrorCode::CRC_ERROR);
}

static void retainedEvents() {
  EventSerialDelivery delivery;
  const uint8_t payload[] = {1, 2, 3};
  constexpr uint64_t session = 0x1234567890abcdefULL;
  const char original[] = "[APP] {\"kind\":\"event\"}\r\n";
  auto* slot = delivery.available();
  std::memcpy(slot->line, original, sizeof(original) - 1);
  assert(delivery.retain(*slot, session, 1, 33, 4, payload, sizeof(payload), sizeof(original) - 1));
  assert(delivery.find(1, 33, 4) == slot && delivery.find(1, 33, 5) == nullptr);
  const char id[] = "1234567890abcdef00000001";
  assert(!delivery.confirm(session, id, 24));
  unsigned sends = 0;
  std::string wire;
  auto send = [&](const uint8_t* data, size_t count) {
    assert(!sends || wire == std::string(reinterpret_cast<const char*>(data), count));
    wire.assign(reinterpret_cast<const char*>(data), count);
    ++sends;
    return true;
  };
  delivery.service(100, [](const uint8_t*, size_t) { return false; });
  assert(!delivery.confirm(session, id, 24));
  delivery.service(200, send);
  delivery.service(1199, send);
  assert(sends == 1);
  delivery.service(1200, send);
  assert(sends == 2 && wire.find(id) != std::string::npos);
  assert(!delivery.confirm(session + 1, id, 24));
  assert(delivery.confirm(session, id, 24) == slot);
  slot->occupied = false;
  delivery.service(3000, send);
  assert(sends == 2 && !delivery.confirm(session, id, 24));
}

static void retainedPdr() {
  PdrSerialDelivery queue;
  PdrReport report{{}, 10, 60000};
  constexpr uint64_t session = 6;
  for (unsigned i = 0; i < PdrSerialDelivery::CAPACITY; ++i) assert(queue.retain(i + 1, report));
  assert(!queue.retain(100, report) && queue.refused == 1);
  assert(!queue.confirm(session, session, 1, 10));
  std::string frames;
  auto send = [&](const uint8_t* data, size_t size) {
    frames.append(reinterpret_cast<const char*>(data), size);
    return true;
  };
  queue.service(500, session, [](const uint8_t*, size_t) { return false; });
  queue.service(600, session, send);
  assert(frames.find("vfence.gateway.pdr.v2") != std::string::npos);
  assert(!queue.confirm(session, session + 1, 1, 10));
  assert(!queue.confirm(session, session, 2, 10));
  assert(!queue.confirm(session, session, 1, 11));
  queue.service(1600, session, send);
  assert(queue.retries == 1 && queue.transmitted == 2);
  assert(queue.confirm(session, session, 1, 10));
  assert(!queue.confirm(session, session, 1, 10) && queue.confirmed == 1);
  queue.service(1601, session, send);
  assert(queue.confirm(session, session, 2, 10) && queue.confirmed == 2);
}

int main() {
  fenceCommits();
  retainedEvents();
  retainedPdr();
}
