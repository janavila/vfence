#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace vfence {

enum class SerialWriteResult { COMPLETE, REFUSED, PARTIAL };

template <class Port>
class FramedSerial {
 public:
  // LIST_PROFILES is larger than the 1024-byte UART TX buffer.
  static constexpr size_t MAX_FRAME_BYTES = 1280;
  static constexpr size_t FRAME_CAPACITY = 6;
  static constexpr size_t RESERVED_FRAMES = 3;

  explicit FramedSerial(Port& port) : port_(port) {}

  int availableForWrite() {
    return count_ || recoveryOffset_ < RECOVERY_SIZE ? 0 : port_.availableForWrite();
  }

  bool canQueue(size_t needed) const { return needed <= FRAME_CAPACITY - count_; }
  uint32_t operationalRefusals() const { return operationalRefusals_; }
  uint32_t shortWrites() const { return shortWrites_; }
  void recordOperationalRefusal() { ++operationalRefusals_; }

  void queue(const uint8_t* data, size_t size, bool reserved) {
    if (!size || size > MAX_FRAME_BYTES || data[size - 1] != '\n' ||
        !canQueue(reserved ? 1 : RESERVED_FRAMES + 1)) {
      recordOperationalRefusal();
      return;
    }
    Frame& frame = frames_[(head_ + count_) % FRAME_CAPACITY];
    memcpy(frame.data, data, size);
    frame.size = size;
    if (++count_ == 1 && recoveryOffset_ == RECOVERY_SIZE) service();
  }

  // Used by the existing HDOP/RADIO retry policy, PDR, and optional diagnostics.
  SerialWriteResult tryWrite(const uint8_t* data, size_t size) {
    const int room = availableForWrite();
    if (!size || size > MAX_FRAME_BYTES || data[size - 1] != '\n' ||
        room <= 0 || static_cast<size_t>(room) < size) return SerialWriteResult::REFUSED;
    const size_t accepted = port_.write(data, size);
    if (accepted == size) return SerialWriteResult::COMPLETE;
    ++shortWrites_;
    if (accepted) recoveryOffset_ = 0;
    return SerialWriteResult::PARTIAL;
  }

  // One physical write at most. The loop never waits for room to become available.
  void service() {
    if (!count_ && recoveryOffset_ == RECOVERY_SIZE) return;
    const int available = port_.availableForWrite();
    if (available <= 0) return;
    const size_t room = static_cast<size_t>(available);
    if (recoveryOffset_ < RECOVERY_SIZE) {
      const uint8_t recovery[] = {0, '\n'};
      const size_t size = RECOVERY_SIZE - recoveryOffset_;
      recoveryOffset_ += port_.write(recovery + recoveryOffset_, room < size ? room : size);
      return;
    }
    const Frame& frame = frames_[head_];
    const size_t remaining = frame.size - offset_;
    const size_t size = room < remaining ? room : remaining;
    const size_t accepted = port_.write(frame.data + offset_, size);
    if (accepted < size) {
      ++shortWrites_;
      if (accepted || offset_) recoveryOffset_ = 0;
      // Retry the same complete frame only after the peer can reject its fragment.
      offset_ = 0;
      return;
    }
    offset_ += accepted;
    if (offset_ == frame.size) {
      offset_ = 0;
      head_ = (head_ + 1) % FRAME_CAPACITY;
      --count_;
    }
  }

 private:
  static constexpr size_t RECOVERY_SIZE = 2;
  struct Frame {
    uint8_t data[MAX_FRAME_BYTES];
    size_t size;
  };
  Port& port_;
  Frame frames_[FRAME_CAPACITY]{};
  size_t head_ = 0;
  size_t count_ = 0;
  size_t offset_ = 0;
  size_t recoveryOffset_ = RECOVERY_SIZE;
  uint32_t operationalRefusals_ = 0;
  uint32_t shortWrites_ = 0;
};

}  // namespace vfence
