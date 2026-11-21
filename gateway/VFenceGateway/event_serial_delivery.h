#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace vfence {

class EventSerialDelivery {
 public:
  static constexpr size_t CAPACITY = 16;
  static constexpr size_t FRAME_BYTES = 512;
  struct Entry {
    bool occupied;
    uint8_t collar;
    uint16_t sequence;
    uint32_t collarRun;
    uint8_t payloadSize;
    uint8_t payload[64];
    uint32_t ordinal;
    uint32_t lastSent;
    bool sent;
    size_t length;
    uint8_t line[FRAME_BYTES];
  };

  Entry* find(uint8_t collar, uint16_t sequence, uint32_t collarRun) {
    for (auto& entry : entries_)
      if (entry.occupied && entry.collar == collar && entry.sequence == sequence &&
          entry.collarRun == collarRun) return &entry;
    return nullptr;
  }

  Entry* available() {
    for (auto& entry : entries_)
      if (!entry.occupied) return &entry;
    return nullptr;
  }

  bool hasPending() const {
    for (const auto& entry : entries_) if (entry.occupied) return true;
    return false;
  }

  bool retain(Entry& entry, uint64_t session, uint8_t collar, uint16_t sequence,
              uint32_t collarRun, const uint8_t* payload, size_t size, size_t rawLength) {
    if (!rawLength || rawLength > FRAME_BYTES || !size || size > sizeof(entry.payload) ||
        nextOrdinal_ == UINT32_MAX) return false;
    size_t end = rawLength;
    while (end && (entry.line[end - 1] == '\r' || entry.line[end - 1] == '\n')) --end;
    if (!end || entry.line[end - 1] != '}') return false;
    const auto ordinal = ++nextOrdinal_;
    const int written = std::snprintf(reinterpret_cast<char*>(entry.line + end - 1),
        sizeof(entry.line) - end + 1,
        ",\"deliveryVersion\":1,\"deliveryId\":\"%016llx%08lx\"}\n",
        static_cast<unsigned long long>(session), static_cast<unsigned long>(ordinal));
    if (written <= 0 || static_cast<size_t>(written) >= sizeof(entry.line) - end + 1) return false;
    entry.length = end - 1 + static_cast<size_t>(written);
    entry.collar = collar;
    entry.sequence = sequence;
    entry.collarRun = collarRun;
    entry.payloadSize = size;
    std::memcpy(entry.payload, payload, size);
    entry.ordinal = ordinal;
    entry.sent = false;
    entry.occupied = true;
    return true;
  }

  Entry* confirm(uint64_t session, const char* id, size_t length) {
    if (length != 24) return nullptr;
    for (auto& entry : entries_) {
      if (!entry.occupied || !entry.sent) continue;
      char expected[25];
      std::snprintf(expected, sizeof(expected), "%016llx%08lx",
          static_cast<unsigned long long>(session), static_cast<unsigned long>(entry.ordinal));
      if (std::memcmp(id, expected, length) == 0) return &entry;
    }
    return nullptr;
  }

  template <class Send>
  void service(uint32_t now, Send send) {
    for (size_t visited = 0; visited < CAPACITY; ++visited) {
      auto& entry = entries_[nextSlot_];
      nextSlot_ = (nextSlot_ + 1) % CAPACITY;
      if (!entry.occupied || (entry.sent && now - entry.lastSent < 1000)) continue;
      if (send(entry.line, entry.length)) {
        entry.sent = true;
        entry.lastSent = now;
      }
      return;
    }
  }

 private:
  Entry entries_[CAPACITY]{};
  uint32_t nextOrdinal_ = 0;
  size_t nextSlot_ = 0;
};

}  // namespace vfence
