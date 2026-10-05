#include "vfence_fence_transfer.h"

#include <cstring>

namespace vfence {

FenceAssembler::FenceAssembler() {
  clear();
}

void FenceAssembler::clear() {
  active_ = false;
  fenceVersion_ = 0;
  vertexCount_ = 0;
  chunkCount_ = 0;
  expectedCrc32_ = 0;
  receivedMask_ = 0;
  std::memset(points_, 0, sizeof(points_));
}

bool FenceAssembler::begin(uint16_t fenceVersion, uint8_t vertexCount,
                           uint8_t chunkCount, uint32_t expectedCrc32) {
  if (vertexCount < 3 || vertexCount > MAX_VERTICES) return false;
  const uint8_t expectedChunks = static_cast<uint8_t>((vertexCount + POINTS_PER_CHUNK - 1) /
                                                       POINTS_PER_CHUNK);
  if (chunkCount != expectedChunks || chunkCount < 1 || chunkCount > MAX_CHUNKS) return false;

  clear();
  active_ = true;
  fenceVersion_ = fenceVersion;
  vertexCount_ = vertexCount;
  chunkCount_ = chunkCount;
  expectedCrc32_ = expectedCrc32;
  return true;
}

ChunkResult FenceAssembler::addChunk(uint16_t fenceVersion, uint8_t chunkIndex,
                                     uint8_t firstVertex,
                                     const CoordinateE7* points,
                                     uint8_t pointCount) {
  if (!active_ || !points || fenceVersion != fenceVersion_) return ChunkResult::INVALID;
  if (chunkIndex >= chunkCount_ || pointCount < 1 || pointCount > POINTS_PER_CHUNK) {
    return ChunkResult::INVALID;
  }

  const uint8_t expectedFirst = static_cast<uint8_t>(chunkIndex * POINTS_PER_CHUNK);
  if (firstVertex != expectedFirst || firstVertex >= vertexCount_) return ChunkResult::INVALID;

  const uint8_t remaining = static_cast<uint8_t>(vertexCount_ - firstVertex);
  const uint8_t expectedPointCount = remaining >= POINTS_PER_CHUNK ? POINTS_PER_CHUNK : remaining;
  if (pointCount != expectedPointCount) return ChunkResult::INVALID;

  const uint16_t bit = static_cast<uint16_t>(1u << chunkIndex);
  if ((receivedMask_ & bit) != 0) {
    for (uint8_t i = 0; i < pointCount; ++i) {
      const CoordinateE7& oldPoint = points_[firstVertex + i];
      if (oldPoint.lat != points[i].lat || oldPoint.lon != points[i].lon) {
        return ChunkResult::DIFFERENT_DUPLICATE;
      }
    }
    return ChunkResult::DUPLICATE_IDENTICAL;
  }

  for (uint8_t i = 0; i < pointCount; ++i) {
    points_[firstVertex + i] = points[i];
  }
  receivedMask_ |= bit;
  return ChunkResult::OK;
}

uint16_t FenceAssembler::missingBitmap() const {
  if (!active_) return 0;
  const uint16_t expectedMask = chunkCount_ == 16
      ? 0xFFFFu
      : static_cast<uint16_t>((1u << chunkCount_) - 1u);
  return static_cast<uint16_t>(expectedMask & ~receivedMask_);
}

bool FenceAssembler::complete() const {
  return active_ && missingBitmap() == 0;
}

bool FenceAssembler::crcValid() const {
  if (!complete()) return false;
  return computeFenceCrc32(fenceVersion_, points_, vertexCount_) == expectedCrc32_;
}

bool FenceAssembler::validateCommit(uint16_t fenceVersion, uint32_t fenceCrc32,
                                    ErrorCode& error, uint16_t& detail) const {
  detail = 0;
  if (!active_ || fenceVersion != fenceVersion_) {
    error = ErrorCode::INVALID_FENCE_VERSION;
    return false;
  }

  const uint16_t missing = missingBitmap();
  if (missing != 0) {
    error = ErrorCode::MISSING_CHUNKS;
    detail = missing;
    return false;
  }

  if (fenceCrc32 != expectedCrc32_ || !crcValid()) {
    error = ErrorCode::CRC_ERROR;
    return false;
  }

  return true;
}

} // namespace vfence
