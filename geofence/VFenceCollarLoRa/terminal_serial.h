#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

namespace vfence {

// This is used only by the sketch's single Serial writer, for human text.
class TerminalSerial {
 public:
  uint32_t omitted() const { return omitted_; }

  template <class Port, class... Args>
  void print(Port& serial, const char* format, Args... args) {
    if (!tryPrint(serial, format, args...)) ++omitted_;
  }

  // Periodic summaries use tryPrint so a full UART cannot create more notices.
  template <class Port, class... Args>
  bool tryPrint(Port& serial, const char* format, Args... args) {
    const char cut[] = " [CORTE]\n";
    const int room = serial.availableForWrite();
    if (room <= int(sizeof(cut) - 1)) return false;
    char message[384];
    const int length = render(message, format, args...);
    if (length <= 0 || length >= int(sizeof(message)) ||
        message[length - 1] != '\n' || length + int(sizeof(cut) - 1) > room) return false;
    if (serial.write(reinterpret_cast<const uint8_t*>(message), length) == static_cast<size_t>(length)) return true;
    // Space for this marker was reserved before the only normal write.
    serial.write(reinterpret_cast<const uint8_t*>(cut), sizeof(cut) - 1);
    return false;
  }

 private:
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
