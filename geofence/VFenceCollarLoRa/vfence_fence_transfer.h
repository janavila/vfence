#pragma once

#include <stdint.h>

#include "vfence_protocol.h"

namespace vfence {

enum class ChunkResult : uint8_t {
  OK = 0,
  DUPLICATE_IDENTICAL = 1,
  INVALID = 2,
  DIFFERENT_DUPLICATE = 3
};

class FenceAssembler {
 public:
  FenceAssembler();

  bool begin(uint16_t fenceVersion, uint8_t vertexCount,
             uint8_t chunkCount, uint32_t expectedCrc32);

  ChunkResult addChunk(uint16_t fenceVersion, uint8_t chunkIndex,
                       uint8_t firstVertex, const CoordinateE7* points,
                       uint8_t pointCount);

  uint16_t missingBitmap() const;
  bool complete() const;
  bool crcValid() const;
  bool validateCommit(uint16_t fenceVersion, uint32_t fenceCrc32,
                      ErrorCode& error, uint16_t& detail) const;

  const CoordinateE7* points() const { return points_; }
  uint8_t vertexCount() const { return vertexCount_; }
  uint8_t chunkCount() const { return chunkCount_; }
  uint16_t fenceVersion() const { return fenceVersion_; }
  uint32_t expectedCrc32() const { return expectedCrc32_; }
  bool activeTransfer() const { return active_; }
  void clear();

 private:
  bool active_;
  uint16_t fenceVersion_;
  uint8_t vertexCount_;
  uint8_t chunkCount_;
  uint32_t expectedCrc32_;
  uint16_t receivedMask_;
  CoordinateE7 points_[MAX_VERTICES];
};

} // namespace vfence
