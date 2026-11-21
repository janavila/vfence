#pragma once

#include <string.h>
#include "lora_profiles.h"
#include "vfence_protocol.h"

namespace vfence {

constexpr uint8_t POSITION_STATUS_COUNTED = 0x03;
constexpr size_t LEGACY_POSITION_PAYLOAD_SIZE = 18;
constexpr size_t COUNTED_POSITION_PACKET_SIZE = HEADER_SIZE + LEGACY_POSITION_PAYLOAD_SIZE + 13;
static_assert(COUNTED_POSITION_PACKET_SIZE == 36 && COUNTED_POSITION_PACKET_SIZE <= MAX_PACKET_SIZE,
              "Invalid counted position layout");

struct PositionDeliveryTag {
  uint64_t sessionId;
  uint32_t ordinal;
  uint8_t profileId;
};

inline size_t encodeCountedPosition(uint8_t* out, size_t capacity, uint8_t collarId,
                                    uint16_t sequence, const TelemetryPayload& position,
                                    const PositionDeliveryTag& tag) {
  if (capacity < COUNTED_POSITION_PACKET_SIZE || tag.profileId >= LORA_PROFILE_COUNT ||
      !encodeTelemetry(out, capacity, collarId, sequence, position)) return 0;
  out[4] = COUNTED_POSITION_PACKET_SIZE - HEADER_SIZE;
  out[HEADER_SIZE] = POSITION_STATUS_COUNTED;
  uint8_t* tail = out + HEADER_SIZE + LEGACY_POSITION_PAYLOAD_SIZE;
  for (unsigned i = 0; i < 8; ++i) tail[i] = static_cast<uint8_t>(tag.sessionId >> (56 - 8 * i));
  for (unsigned i = 0; i < 4; ++i) tail[8 + i] = static_cast<uint8_t>(tag.ordinal >> (24 - 8 * i));
  tail[12] = tag.profileId;
  return COUNTED_POSITION_PACKET_SIZE;
}

// Adapt only the validated extended frame to the existing position pipeline.
inline bool unpackCountedPosition(const PacketView& packet, PositionDeliveryTag& tag,
                                   PacketView& legacy, uint8_t (&payload)[LEGACY_POSITION_PAYLOAD_SIZE]) {
  if (packet.header.type != MessageType::TELEMETRY || packet.header.version != PROTOCOL_VERSION ||
      packet.header.ackReq || packet.header.retry || packet.header.urgent ||
      packet.payloadLength != COUNTED_POSITION_PACKET_SIZE - HEADER_SIZE ||
      packet.payload[0] != POSITION_STATUS_COUNTED) return false;

  const uint8_t* tail = packet.payload + LEGACY_POSITION_PAYLOAD_SIZE;
  if (tail[12] >= LORA_PROFILE_COUNT) return false;
  tag = {0, 0, tail[12]};
  for (unsigned i = 0; i < 8; ++i) tag.sessionId = (tag.sessionId << 8) | tail[i];
  for (unsigned i = 0; i < 4; ++i) tag.ordinal = (tag.ordinal << 8) | tail[8 + i];

  memcpy(payload, packet.payload, sizeof(payload));
  payload[0] = static_cast<uint8_t>(TelemetrySubtype::POSITION_STATUS);
  legacy = packet;
  legacy.header.payloadLength = sizeof(payload);
  legacy.payloadLength = sizeof(payload);
  legacy.payload = payload;
  return true;
}

}  // namespace vfence
