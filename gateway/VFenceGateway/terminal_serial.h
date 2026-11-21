#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "framed_serial.h"

namespace vfence {

class TerminalSerial {
 public:
  uint32_t omitted() const { return omitted_; }

  template <class Port, class... Args>
  void print(Port& serial, const char* format, Args... args) {
    formatAndWrite(serial, true, format, args...);
  }

  // Periodic summaries use tryPrint so a full UART cannot create more notices.
  template <class Port, class... Args>
  bool tryPrint(Port& serial, const char* format, Args... args) {
    return formatAndWrite(serial, false, format, args...);
  }

  template <class Port>
  bool write(Port& serial, const uint8_t* message, size_t length, bool countRefusal = true) {
    const auto result = serial.tryWrite(message, length);
    if (result == SerialWriteResult::COMPLETE) return true;
    if (countRefusal || result == SerialWriteResult::PARTIAL) ++omitted_;
    return false;
  }

  void recordOmission() { ++omitted_; }

 private:
  template <class Port, class... Args>
  bool formatAndWrite(Port& serial, bool countRefusal, const char* format, Args... args) {
    char message[384];
    const int length = render(message, format, args...);
    if (length <= 0 || length >= int(sizeof(message))) {
      if (countRefusal) recordOmission();
      return false;
    }
    return write(serial, reinterpret_cast<const uint8_t*>(message), length, countRefusal);
  }

  template <size_t N>
  static int render(char (&message)[N], const char* text) {
    return snprintf(message, N, "%s", text);
  }

  template <size_t N, class First, class... Rest>
  static int render(char (&message)[N], const char* format, First first, Rest... rest) {
    return snprintf(message, N, format, first, rest...);
  }

  uint32_t omitted_ = 0;
};

template <size_t N>
void terminalHex(const uint8_t* data, size_t length, char (&out)[N]) {
  const char digits[] = "0123456789ABCDEF";
  size_t used = 0;
  for (size_t i = 0; i < length && i < N / 3; ++i) {
    if (i != 0) out[used++] = ' ';
    out[used++] = digits[data[i] >> 4];
    out[used++] = digits[data[i] & 0x0f];
  }
  out[used] = '\0';
}

}  // namespace vfence
