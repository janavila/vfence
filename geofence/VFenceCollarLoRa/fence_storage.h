#pragma once

#include <cstring>

#include "vfence_protocol.h"

namespace vfence {

struct FenceConfiguration {
  CoordinateE7 points[MAX_VERTICES];
  ParametersPayload parameters;
  uint16_t version;
  uint8_t count;
  bool development;
};

inline bool validStoredFence(const FenceConfiguration& fence) {
  const auto& p = fence.parameters;
  if (fence.count < 3 || fence.count > MAX_VERTICES || !fence.version ||
      p.muitoPertoM > p.pertoM || !p.telemetryIntervalS || p.telemetryIntervalS > 60 ||
      !p.gpsTimeoutS || p.lowBatteryPct > 100 || p.criticalBatteryPct > p.lowBatteryPct)
    return false;
  for (uint8_t i = 0; i < fence.count; ++i) {
    const auto& point = fence.points[i];
    if (point.lat < -900000000 || point.lat > 900000000 ||
        point.lon < -1800000000 || point.lon > 1800000000) return false;
  }
  return true;
}

// A candidate becomes visible at boot only after its separate activation marker
// was written. The previously active slot is kept intact during the commit.
class FenceStorage {
 public:
  template <class Nvs>
  bool load(Nvs& nvs, FenceConfiguration& fence) {
    generation_ = 0;
    for (uint8_t slot = 0; slot < 2; ++slot) {
      FenceConfiguration candidate{};
      uint32_t generation = 0, checksum = 0;
      if (!readRecord(nvs, slot, candidate, generation, checksum) || generation <= generation_) continue;
      uint8_t expected[12], actual[12];
      makeMarker(generation, checksum, expected);
      if (nvs.getBytesLength(markerKey(slot)) != sizeof(actual) ||
          nvs.getBytes(markerKey(slot), actual, sizeof(actual)) != sizeof(actual) ||
          std::memcmp(actual, expected, sizeof(actual)) != 0) continue;
      fence = candidate;
      slot_ = slot;
      generation_ = generation;
    }
    return generation_ != 0;
  }

  template <class Nvs>
  bool hasActivationMarker(Nvs& nvs) const {
    return nvs.getBytesLength(markerKey(0)) || nvs.getBytesLength(markerKey(1));
  }

  template <class Nvs>
  bool commit(Nvs& nvs, const FenceConfiguration& fence) {
    if (!validStoredFence(fence) || generation_ == UINT32_MAX) return false;
    const uint8_t slot = generation_ ? 1 - slot_ : 0;
    const uint32_t generation = generation_ + 1;
    uint8_t data[MAX_RECORD], readback[MAX_RECORD], marker[12];
    const size_t length = encode(fence, generation, data);
    if (nvs.putBytes(recordKey(slot), data, length) != length ||
        nvs.getBytesLength(recordKey(slot)) != length ||
        nvs.getBytes(recordKey(slot), readback, length) != length ||
        std::memcmp(data, readback, length) != 0) return false;
    makeMarker(generation, read32(data + length - 4), marker);
    if (nvs.putBytes(markerKey(slot), marker, sizeof(marker)) != sizeof(marker)) return false;
    slot_ = slot;
    generation_ = generation;
    return true;
  }

 private:
  static constexpr size_t RECORD_HEADER = 24;
  static constexpr size_t MAX_RECORD = RECORD_HEADER + MAX_VERTICES * 8 + 4;
  static const char* recordKey(uint8_t slot) { return slot == 0 ? "fslot_a" : "fslot_b"; }
  static const char* markerKey(uint8_t slot) { return slot == 0 ? "fmark_a" : "fmark_b"; }

  static void put32(uint8_t* out, uint32_t value) {
    for (uint8_t i = 0; i < 4; ++i) out[i] = static_cast<uint8_t>(value >> (8 * i));
  }
  static void put16(uint8_t* out, uint16_t value) {
    out[0] = static_cast<uint8_t>(value);
    out[1] = static_cast<uint8_t>(value >> 8);
  }
  static uint32_t read32(const uint8_t* in) {
    return uint32_t(in[0]) | (uint32_t(in[1]) << 8) | (uint32_t(in[2]) << 16) | (uint32_t(in[3]) << 24);
  }
  static uint16_t read16(const uint8_t* in) { return uint16_t(in[0]) | (uint16_t(in[1]) << 8); }

  static void makeMarker(uint32_t generation, uint32_t checksum, uint8_t* marker) {
    std::memcpy(marker, "VFM1", 4);
    put32(marker + 4, generation);
    put32(marker + 8, checksum);
  }

  static size_t encode(const FenceConfiguration& fence, uint32_t generation, uint8_t* data) {
    std::memcpy(data, "VFS1", 4);
    put32(data + 4, generation);
    put16(data + 8, fence.version);
    data[10] = fence.count;
    data[11] = fence.development;
    const auto& p = fence.parameters;
    put16(data + 12, p.muitoPertoM);
    put16(data + 14, p.pertoM);
    put32(data + 16, p.telemetryIntervalS);
    put16(data + 20, p.gpsTimeoutS);
    data[22] = p.lowBatteryPct;
    data[23] = p.criticalBatteryPct;
    size_t offset = RECORD_HEADER;
    for (uint8_t i = 0; i < fence.count; ++i, offset += 8) {
      put32(data + offset, static_cast<uint32_t>(fence.points[i].lat));
      put32(data + offset + 4, static_cast<uint32_t>(fence.points[i].lon));
    }
    put32(data + offset, crc32IsoHdlc(data, offset));
    return offset + 4;
  }

  template <class Nvs>
  static bool readRecord(Nvs& nvs, uint8_t slot, FenceConfiguration& out, uint32_t& generation,
                         uint32_t& checksum) {
    uint8_t data[MAX_RECORD];
    const size_t length = nvs.getBytesLength(recordKey(slot));
    if (length < RECORD_HEADER + 4 || length > sizeof(data) ||
        nvs.getBytes(recordKey(slot), data, length) != length || std::memcmp(data, "VFS1", 4) != 0)
      return false;
    out.count = data[10];
    if (out.count < 3 || out.count > MAX_VERTICES || data[11] > 1 ||
        length != RECORD_HEADER + out.count * 8 + 4) return false;
    checksum = read32(data + length - 4);
    if (checksum != crc32IsoHdlc(data, length - 4)) return false;
    generation = read32(data + 4);
    out.version = read16(data + 8);
    out.development = data[11] != 0;
    out.parameters = {read16(data + 12), read16(data + 14), read32(data + 16),
                      read16(data + 20), data[22], data[23]};
    for (uint8_t i = 0; i < out.count; ++i) {
      const size_t offset = RECORD_HEADER + i * 8;
      out.points[i] = {static_cast<int32_t>(read32(data + offset)),
                       static_cast<int32_t>(read32(data + offset + 4))};
    }
    return generation && validStoredFence(out);
  }

  uint8_t slot_ = 0;
  uint32_t generation_ = 0;
};

}  // namespace vfence
