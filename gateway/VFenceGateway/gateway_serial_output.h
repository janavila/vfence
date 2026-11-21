#pragma once

#include <Arduino.h>
#include <stdarg.h>

#include "terminal_serial.h"

namespace vfence {

// Preserve Arduino Print formatting and CRLF while assembling a whole line in RAM.
template <class Port>
class GatewaySerialOutput : public Print {
 public:
  GatewaySerialOutput(FramedSerial<Port>& serial, TerminalSerial& terminal)
      : serial_(serial), terminal_(terminal) {}

  void reserveFrame() { reserved_ = true; }

  size_t write(uint8_t byte) override { return write(&byte, 1); }

  size_t write(const uint8_t* data, size_t size) override {
    size_t offset = 0;
    while (offset < size) {
      const auto* lf = static_cast<const uint8_t*>(memchr(data + offset, '\n', size - offset));
      const size_t part = lf ? static_cast<size_t>(lf - (data + offset)) + 1 : size - offset;
      if (part > sizeof(line_) - used_) overflowed_ = true;
      if (!overflowed_) {
        memcpy(line_ + used_, data + offset, part);
        used_ += part;
      }
      if (lf) finishLine();
      offset += part;
    }
    return size;
  }

  size_t printf(const char* format, ...) {
    char text[384];
    va_list args;
    va_start(args, format);
    const int size = vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    if (size < 0 || static_cast<size_t>(size) >= sizeof(text)) {
      overflowed_ = true;
      if (strchr(format, '\n')) finishLine();
      return 0;
    }
    return write(reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(size));
  }

 private:
  void finishLine() {
    const bool operational = used_ >= 6 && memcmp(line_, "[APP] ", 6) == 0;
    if (overflowed_) {
      if (operational || reserved_) serial_.recordOperationalRefusal();
      else terminal_.recordOmission();
    } else if (operational) {
      serial_.queue(line_, used_, reserved_);
    } else {
      terminal_.write(serial_, line_, used_);
    }
    used_ = 0;
    overflowed_ = false;
    reserved_ = false;
  }

  FramedSerial<Port>& serial_;
  TerminalSerial& terminal_;
  uint8_t line_[FramedSerial<Port>::MAX_FRAME_BYTES]{};
  size_t used_ = 0;
  bool overflowed_ = false;
  bool reserved_ = false;
};

}  // namespace vfence
