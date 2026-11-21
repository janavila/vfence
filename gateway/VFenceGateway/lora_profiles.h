#pragma once

#include <stdint.h>

namespace vfence {

struct LoRaProfile {
  uint8_t id;
  uint32_t frequencyHz;
  uint32_t bandwidthHz;
  uint8_t spreadingFactor;
  uint8_t codingRateDenominator;
  uint8_t txPowerDbm;
  uint16_t preambleSymbols;
  uint8_t syncWord;
  bool crcEnabled;
  bool explicitHeader;
  bool invertedIq;
};

// P1-P3 are candidates. Only P0 is ever applied by either sketch.
constexpr LoRaProfile LORA_PROFILES[] = {
    {0, 915000000, 125000, 7, 5, 14, 8, 0x12, true, true, false},
    {1, 915000000, 125000, 8, 5, 14, 8, 0x12, true, true, false},
    {2, 915000000, 125000, 9, 5, 14, 8, 0x12, true, true, false},
    {3, 915000000, 125000, 10, 5, 14, 8, 0x12, true, true, false},
};

constexpr uint8_t LORA_PROFILE_COUNT = sizeof(LORA_PROFILES) / sizeof(LORA_PROFILES[0]);
constexpr uint8_t INITIAL_LORA_PROFILE = LORA_PROFILES[0].id;

}  // namespace vfence
