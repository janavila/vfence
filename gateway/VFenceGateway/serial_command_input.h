#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace vfence {

constexpr size_t SERIAL_COMMAND_MAX_LENGTH = 512;

class SerialCommandInput {
 public:
  enum class Result { INCOMPLETE, LINE, DISCARDED, TOO_LONG };

  Result feed(uint8_t byte) {
    if (byte == 0) {
      discarded = Result::DISCARDED;
    } else if (byte == '\n') {
      if (length && data[length - 1] == '\r') --length;
      if (discarded == Result::INCOMPLETE) {
        if (length > SERIAL_COMMAND_MAX_LENGTH) discarded = Result::TOO_LONG;
        else if (std::memchr(data, '\r', length)) discarded = Result::DISCARDED;
        else discarded = Result::LINE;
      }
      data[length] = '\0';
      const Result result = discarded;
      length = 0;
      discarded = Result::INCOMPLETE;
      return result;
    } else if (discarded == Result::INCOMPLETE) {
      // Um byte adicional permite CR antes de LF em um comando de 512 bytes.
      if (length == sizeof(data) - 1) discarded = Result::TOO_LONG;
      else data[length++] = static_cast<char>(byte);
    }
    return Result::INCOMPLETE;
  }

  const char* line() const { return data; }

 private:
  char data[SERIAL_COMMAND_MAX_LENGTH + 2]{};
  size_t length = 0;
  Result discarded = Result::INCOMPLETE;
};

// Valida um unico objeto antes de expor campos. Literais, strings e objetos
// aninhados em campos auxiliares nunca participam da busca de chaves da raiz.
class GatewayCommandJson {
 public:
  explicit GatewayCommandJson(const char* json) : valid(parseObject(json, 0) && *skipSpace(json) == '\0') {}

  bool isValid() const { return valid; }

  const char* field(const char* key) const {
    if (!valid) return nullptr;
    const auto names = fieldNames();
    for (size_t i = 0; i < FIELD_COUNT; ++i) {
      if (std::strcmp(key, names[i]) == 0) return fields[i];
    }
    return nullptr;
  }

  bool stringField(const char* key, const char*& value, size_t& length) const {
    value = field(key);
    if (!value || *value++ != '"') return false;
    const char* end = std::strchr(value, '"');
    if (!end || std::memchr(value, '\\', end - value)) return false;
    length = end - value;
    return true;
  }

  bool unsignedField(const char* key, uint32_t& value, uint32_t maximum, bool required = true) const {
    const char* p = field(key);
    if (!p) return !required;
    if (!digit(*p)) return false;
    uint32_t number = 0;
    do {
      const uint32_t d = *p++ - '0';
      if (d > maximum || number > (maximum - d) / 10) return false;
      number = number * 10 + d;
    } while (digit(*p));
    if (!endOfField(p)) return false;
    value = number;
    return true;
  }

  bool doubleField(const char* key, double& value) const {
    const char* p = field(key);
    if (!p || (!digit(*p) && *p != '-')) return false;
    char* end = nullptr;
    const double number = std::strtod(p, &end);
    if (end == p || !std::isfinite(number) || !endOfField(end)) return false;
    value = number;
    return true;
  }

 private:
  static constexpr size_t FIELD_COUNT = 21;
  static constexpr unsigned MAX_DEPTH = 8;
  const char* fields[FIELD_COUNT]{};
  const bool valid;

  static const char* const* fieldNames() {
    static const char* const names[FIELD_COUNT] = {
      "schema", "requestId", "command", "collarId", "token", "veryNearM", "nearM",
      "telemetryIntervalS", "gpsTimeoutS", "lowBatteryPct", "criticalBatteryPct",
      "fenceVersion", "vertexCount", "index", "latitude", "longitude",
      "gatewaySessionId", "operationId", "profileId", "collarSessionId", "windowId"
    };
    return names;
  }

  static bool digit(char c) { return c >= '0' && c <= '9'; }

  static const char* skipSpace(const char* p) {
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') ++p;
    return p;
  }

  static bool endOfField(const char* p) {
    const char next = *skipSpace(p);
    return next == ',' || next == '}';
  }

  static int hexDigit(char c) {
    if (digit(c)) return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  }

  static bool scanUtf8(const char*& p, uint8_t first) {
    if (first < 0xC2 || first > 0xF4) return false;
    const unsigned count = first < 0xE0 ? 1 : first < 0xF0 ? 2 : 3;
    const auto second = static_cast<uint8_t>(p[1]);
    if ((first == 0xE0 && second < 0xA0) || (first == 0xED && second >= 0xA0) ||
        (first == 0xF0 && second < 0x90) || (first == 0xF4 && second >= 0x90)) return false;
    for (unsigned i = 0; i < count; ++i) {
      if ((static_cast<uint8_t>(*++p) & 0xC0) != 0x80) return false;
    }
    return true;
  }

  static bool scanString(const char*& p) {
    if (*p != '"') return false;
    while (*++p != '"') {
      const auto byte = static_cast<uint8_t>(*p);
      if (byte < 0x20 || (byte >= 0x80 && !scanUtf8(p, byte))) return false;
      if (*p != '\\') continue;
      ++p;
      if (*p == 'u') {
        for (unsigned i = 0; i < 4; ++i) {
          if (hexDigit(*++p) < 0) return false;
        }
      } else if (!*p || !std::strchr("\"\\/bfnrt", *p)) {
        return false;
      }
    }
    ++p;
    return true;
  }

  // As chaves do protocolo sao ASCII. Reconhece tambem a grafia \uXXXX,
  // inclusive para rejeitar uma segunda grafia da mesma chave reservada.
  static bool sameKey(const char* p, const char* key) {
    while (*key && *p != '"') {
      unsigned c = static_cast<uint8_t>(*p++);
      if (c == '\\') {
        if (*p++ != 'u') return false;
        c = 0;
        for (unsigned i = 0; i < 4; ++i) c = (c << 4) | hexDigit(*p++);
      }
      if (c != static_cast<uint8_t>(*key++)) return false;
    }
    return *key == '\0' && *p == '"';
  }

  static bool scanNumber(const char*& p) {
    if (*p == '-') ++p;
    if (!digit(*p)) return false;
    if (*p == '0') ++p;
    else while (digit(*p)) ++p;
    if (*p == '.') {
      if (!digit(*++p)) return false;
      while (digit(*p)) ++p;
    }
    if (*p == 'e' || *p == 'E') {
      ++p;
      if (*p == '+' || *p == '-') ++p;
      if (!digit(*p)) return false;
      while (digit(*p)) ++p;
    }
    return true;
  }

  bool parseObject(const char*& p, unsigned depth) {
    p = skipSpace(p);
    if (*p != '{' || depth > MAX_DEPTH) return false;
    p = skipSpace(p + 1);
    if (*p != '}') {
      do {
        const char* key = p + 1;
        if (!scanString(p)) return false;
        p = skipSpace(p);
        if (*p != ':') return false;
        p = skipSpace(p + 1);
        if (depth == 0 && !recordField(key, p)) return false;
        if (!parseValue(p, depth)) return false;
        p = skipSpace(p);
        if (*p != ',') break;
        p = skipSpace(p + 1);
      } while (true);
    }
    if (*p != '}') return false;
    ++p;
    return true;
  }

  bool recordField(const char* key, const char* value) {
    const auto names = fieldNames();
    for (size_t i = 0; i < FIELD_COUNT; ++i) {
      if (!sameKey(key, names[i])) continue;
      if (fields[i]) return false;
      fields[i] = value;
      break;
    }
    return true;
  }

  bool parseValue(const char*& p, unsigned depth) {
    if (*p == '"') return scanString(p);
    if (*p == '{') return parseObject(p, depth + 1);
    if (*p == '[') {
      if (depth >= MAX_DEPTH) return false;
      p = skipSpace(p + 1);
      if (*p != ']') {
        while (true) {
          if (!parseValue(p, depth + 1)) return false;
          p = skipSpace(p);
          if (*p != ',') break;
          p = skipSpace(p + 1);
        }
      }
      if (*p != ']') return false;
      ++p;
      return true;
    }
    const char* literal = *p == 't' ? "true" : *p == 'f' ? "false" : *p == 'n' ? "null" : nullptr;
    if (!literal) return scanNumber(p);
    const size_t size = std::strlen(literal);
    if (std::strncmp(p, literal, size) != 0) return false;
    p += size;
    return true;
  }
};

}  // namespace vfence
