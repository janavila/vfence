#include "vfence_protocol.h"

#include <cmath>
#include <cstring>

namespace vfence {

static bool hasCapacity(size_t capacity, size_t needed) {
  return capacity >= needed;
}

void writeU16BE(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>((v >> 8) & 0xFF);
  p[1] = static_cast<uint8_t>(v & 0xFF);
}

void writeU32BE(uint8_t* p, uint32_t v) {
  p[0] = static_cast<uint8_t>((v >> 24) & 0xFF);
  p[1] = static_cast<uint8_t>((v >> 16) & 0xFF);
  p[2] = static_cast<uint8_t>((v >> 8) & 0xFF);
  p[3] = static_cast<uint8_t>(v & 0xFF);
}

void writeI32BE(uint8_t* p, int32_t v) {
  writeU32BE(p, static_cast<uint32_t>(v));
}

uint16_t readU16BE(const uint8_t* p) {
  return static_cast<uint16_t>((static_cast<uint16_t>(p[0]) << 8) |
                               static_cast<uint16_t>(p[1]));
}

uint32_t readU32BE(const uint8_t* p) {
  return (static_cast<uint32_t>(p[0]) << 24) |
         (static_cast<uint32_t>(p[1]) << 16) |
         (static_cast<uint32_t>(p[2]) << 8) |
         static_cast<uint32_t>(p[3]);
}

int32_t readI32BE(const uint8_t* p) {
  return static_cast<int32_t>(readU32BE(p));
}

int32_t degreesToE7(double degrees) {
  return static_cast<int32_t>(llround(degrees * 10000000.0));
}

double e7ToDegrees(int32_t value) {
  return static_cast<double>(value) / 10000000.0;
}

uint8_t makeControlByte(MessageType type, bool ackReq, bool retry, bool urgent,
                        uint8_t version) {
  uint8_t b = static_cast<uint8_t>((version & 0x03) << 6);
  b |= static_cast<uint8_t>((static_cast<uint8_t>(type) & 0x03) << 4);
  if (ackReq) b |= FLAG_ACK_REQ;
  if (retry) b |= FLAG_RETRY;
  if (urgent) b |= FLAG_URGENT;
  return b;
}

bool encodeHeader(uint8_t* out, size_t capacity, const Header& header) {
  if (!out || !hasCapacity(capacity, HEADER_SIZE)) return false;
  out[0] = makeControlByte(header.type, header.ackReq, header.retry,
                           header.urgent, header.version);
  out[1] = header.collarId;
  writeU16BE(out + 2, header.sequence);
  out[4] = header.payloadLength;
  return true;
}

bool decodePacket(const uint8_t* data, size_t len, PacketView& out) {
  if (!data || len < HEADER_SIZE) return false;

  const uint8_t control = data[0];
  out.header.version = static_cast<uint8_t>((control >> 6) & 0x03);
  out.header.type = static_cast<MessageType>((control >> 4) & 0x03);
  out.header.ackReq = (control & FLAG_ACK_REQ) != 0;
  out.header.retry = (control & FLAG_RETRY) != 0;
  out.header.urgent = (control & FLAG_URGENT) != 0;
  out.header.collarId = data[1];
  out.header.sequence = readU16BE(data + 2);
  out.header.payloadLength = data[4];

  if (len != HEADER_SIZE + out.header.payloadLength) return false;
  out.payload = data + HEADER_SIZE;
  out.payloadLength = out.header.payloadLength;
  return true;
}

uint32_t crc32IsoHdlc(const uint8_t* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
    }
  }
  return crc ^ 0xFFFFFFFFu;
}

uint32_t computeFenceCrc32(uint16_t fenceVersion,
                           const CoordinateE7* points,
                           uint8_t vertexCount) {
  if (!points || vertexCount < 3 || vertexCount > MAX_VERTICES) return 0;

  uint8_t canonical[3 + MAX_VERTICES * 8];
  size_t pos = 0;
  writeU16BE(canonical + pos, fenceVersion); pos += 2;
  canonical[pos++] = vertexCount;

  for (uint8_t i = 0; i < vertexCount; ++i) {
    writeI32BE(canonical + pos, points[i].lat); pos += 4;
    writeI32BE(canonical + pos, points[i].lon); pos += 4;
  }

  return crc32IsoHdlc(canonical, pos);
}

static size_t beginPacket(uint8_t* out, size_t capacity,
                          MessageType type, bool ackReq, bool retry, bool urgent,
                          uint8_t collarId, uint16_t sequence,
                          uint8_t payloadLength) {
  const size_t total = HEADER_SIZE + payloadLength;
  if (!out || !hasCapacity(capacity, total)) return 0;
  Header h{PROTOCOL_VERSION, type, ackReq, retry, urgent,
           collarId, sequence, payloadLength};
  if (!encodeHeader(out, capacity, h)) return 0;
  return total;
}

size_t encodeFenceBegin(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        uint16_t fenceVersion, uint8_t vertexCount,
                        uint8_t chunkCount, uint32_t fenceCrc32) {
  const uint8_t payloadLen = 9;
  const size_t total = beginPacket(out, capacity, MessageType::CONFIG,
                                   true, retry, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ConfigSubtype::FENCE_BEGIN);
  writeU16BE(p + 1, fenceVersion);
  p[3] = vertexCount;
  p[4] = chunkCount;
  writeU32BE(p + 5, fenceCrc32);
  return total;
}

size_t encodeFenceChunk(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        uint16_t fenceVersion, uint8_t chunkIndex,
                        uint8_t firstVertex, const CoordinateE7* points,
                        uint8_t pointCount) {
  if (!points || pointCount < 1 || pointCount > POINTS_PER_CHUNK) return 0;
  const uint8_t payloadLen = static_cast<uint8_t>(6 + pointCount * 8);
  const size_t total = beginPacket(out, capacity, MessageType::CONFIG,
                                   false, retry, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ConfigSubtype::FENCE_CHUNK);
  writeU16BE(p + 1, fenceVersion);
  p[3] = chunkIndex;
  p[4] = firstVertex;
  p[5] = pointCount;
  size_t pos = 6;
  for (uint8_t i = 0; i < pointCount; ++i) {
    writeI32BE(p + pos, points[i].lat); pos += 4;
    writeI32BE(p + pos, points[i].lon); pos += 4;
  }
  return total;
}

size_t encodeFenceCommit(uint8_t* out, size_t capacity,
                         uint8_t collarId, uint16_t sequence, bool retry,
                         uint16_t fenceVersion, uint32_t fenceCrc32) {
  const uint8_t payloadLen = 7;
  const size_t total = beginPacket(out, capacity, MessageType::CONFIG,
                                   true, retry, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ConfigSubtype::FENCE_COMMIT);
  writeU16BE(p + 1, fenceVersion);
  writeU32BE(p + 3, fenceCrc32);
  return total;
}

size_t encodeParameters(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        const ParametersPayload& params) {
  const uint8_t payloadLen = 13;
  const size_t total = beginPacket(out, capacity, MessageType::CONFIG,
                                   true, retry, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ConfigSubtype::PARAMETERS);
  writeU16BE(p + 1, params.muitoPertoM);
  writeU16BE(p + 3, params.pertoM);
  writeU32BE(p + 5, params.telemetryIntervalS);
  writeU16BE(p + 9, params.gpsTimeoutS);
  p[11] = params.lowBatteryPct;
  p[12] = params.criticalBatteryPct;
  return total;
}

static bool validPositionEventSubtype(EventSubtype subtype) {
  return subtype == EventSubtype::OUTSIDE || subtype == EventSubtype::BACK_INSIDE;
}

size_t encodePositionEvent(uint8_t* out, size_t capacity,
                           uint8_t collarId, uint16_t sequence, bool retry,
                           EventSubtype subtype,
                           const PositionEventPayload& d) {
  if (!validPositionEventSubtype(subtype)) return 0;
  const uint8_t payloadLen = 17;
  const size_t total = beginPacket(out, capacity, MessageType::EVENT,
                                   true, retry, true, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(subtype);
  writeU16BE(p + 1, d.fenceVersion);
  writeI32BE(p + 3, d.position.lat);
  writeI32BE(p + 7, d.position.lon);
  writeU16BE(p + 11, d.distanceBorderM);
  p[13] = d.batteryPct;
  p[14] = d.satellites;
  writeU16BE(p + 15, d.hdopX100);
  return total;
}

size_t encodeGpsLost(uint8_t* out, size_t capacity,
                     uint8_t collarId, uint16_t sequence, bool retry,
                     const GpsLostPayload& d) {
  const uint8_t payloadLen = 11;
  const size_t total = beginPacket(out, capacity, MessageType::EVENT,
                                   true, retry, true, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(EventSubtype::GPS_LOST);
  writeI32BE(p + 1, d.lastPosition.lat);
  writeI32BE(p + 5, d.lastPosition.lon);
  writeU16BE(p + 9, d.positionAgeS);
  return total;
}

size_t encodeGpsRecovered(uint8_t* out, size_t capacity,
                          uint8_t collarId, uint16_t sequence, bool retry,
                          const GpsRecoveredPayload& d) {
  const uint8_t payloadLen = 12;
  const size_t total = beginPacket(out, capacity, MessageType::EVENT,
                                   true, retry, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(EventSubtype::GPS_RECOVERED);
  writeI32BE(p + 1, d.position.lat);
  writeI32BE(p + 5, d.position.lon);
  p[9] = d.satellites;
  writeU16BE(p + 10, d.hdopX100);
  return total;
}

size_t encodeBatteryEvent(uint8_t* out, size_t capacity,
                          uint8_t collarId, uint16_t sequence, bool retry,
                          EventSubtype subtype,
                          const BatteryEventPayload& d) {
  if (subtype != EventSubtype::LOW_BATTERY &&
      subtype != EventSubtype::CRITICAL_BATTERY) return 0;
  const bool urgent = subtype == EventSubtype::CRITICAL_BATTERY;
  const uint8_t payloadLen = 12;
  const size_t total = beginPacket(out, capacity, MessageType::EVENT,
                                   true, retry, urgent, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(subtype);
  p[1] = d.batteryPct;
  writeI32BE(p + 2, d.lastPosition.lat);
  writeI32BE(p + 6, d.lastPosition.lon);
  writeU16BE(p + 10, d.positionAgeS);
  return total;
}

size_t encodeDeviceStarted(uint8_t* out, size_t capacity,
                           uint8_t collarId, uint16_t sequence, bool retry,
                           const DeviceStartedPayload& d) {
  const uint8_t payloadLen = 5;
  const size_t total = beginPacket(out, capacity, MessageType::EVENT,
                                   true, retry, true, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(EventSubtype::DEVICE_STARTED);
  p[1] = static_cast<uint8_t>(d.resetReason);
  writeU16BE(p + 2, d.activeFenceVersion);
  p[4] = d.batteryPct;
  return total;
}

size_t encodeTelemetry(uint8_t* out, size_t capacity,
                       uint8_t collarId, uint16_t sequence,
                       const TelemetryPayload& d) {
  const uint8_t payloadLen = 18;
  const size_t total = beginPacket(out, capacity, MessageType::TELEMETRY,
                                   false, false, false, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(TelemetrySubtype::POSITION_STATUS);
  writeI32BE(p + 1, d.position.lat);
  writeI32BE(p + 5, d.position.lon);
  writeU16BE(p + 9, d.distanceBorderM);
  p[11] = d.batteryPct;
  p[12] = d.satellites;
  writeU16BE(p + 13, d.hdopX100);
  p[15] = d.stateFlags;
  writeU16BE(p + 16, d.fenceVersion);
  return total;
}

size_t encodeAck(uint8_t* out, size_t capacity,
                 uint8_t collarId, uint16_t sequence,
                 uint16_t ackedSequence, uint8_t status) {
  const uint8_t payloadLen = 4;
  const size_t total = beginPacket(out, capacity, MessageType::CONTROL,
                                   false, false, true, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ControlSubtype::ACK);
  writeU16BE(p + 1, ackedSequence);
  p[3] = status;
  return total;
}

size_t encodeNack(uint8_t* out, size_t capacity,
                  uint8_t collarId, uint16_t sequence,
                  uint16_t rejectedSequence, ErrorCode errorCode,
                  uint16_t detail) {
  const uint8_t payloadLen = 6;
  const size_t total = beginPacket(out, capacity, MessageType::CONTROL,
                                   false, false, true, collarId, sequence,
                                   payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(ControlSubtype::NACK);
  writeU16BE(p + 1, rejectedSequence);
  p[3] = static_cast<uint8_t>(errorCode);
  writeU16BE(p + 4, detail);
  return total;
}

size_t encodePingPong(uint8_t* out, size_t capacity,
                      uint8_t collarId, uint16_t sequence,
                      ControlSubtype subtype, uint16_t token) {
  if (subtype != ControlSubtype::PING && subtype != ControlSubtype::PONG) return 0;
  const uint8_t payloadLen = 3;
  const size_t total = beginPacket(out, capacity, MessageType::CONTROL,
                                   false, false, subtype == ControlSubtype::PONG,
                                   collarId, sequence, payloadLen);
  if (!total) return 0;
  uint8_t* p = out + HEADER_SIZE;
  p[0] = static_cast<uint8_t>(subtype);
  writeU16BE(p + 1, token);
  return total;
}

static bool checkSubtype(const PacketView& packet, MessageType type,
                         uint8_t subtype, size_t payloadLen) {
  return packet.header.type == type &&
         packet.payloadLength == payloadLen &&
         packet.payload && packet.payload[0] == subtype;
}

bool decodeFenceBegin(const PacketView& packet,
                      uint16_t& fenceVersion, uint8_t& vertexCount,
                      uint8_t& chunkCount, uint32_t& fenceCrc32) {
  if (!checkSubtype(packet, MessageType::CONFIG,
                    static_cast<uint8_t>(ConfigSubtype::FENCE_BEGIN), 9)) return false;
  fenceVersion = readU16BE(packet.payload + 1);
  vertexCount = packet.payload[3];
  chunkCount = packet.payload[4];
  fenceCrc32 = readU32BE(packet.payload + 5);
  return true;
}

bool decodeFenceChunk(const PacketView& packet,
                      uint16_t& fenceVersion, uint8_t& chunkIndex,
                      uint8_t& firstVertex, CoordinateE7* pointsOut,
                      uint8_t& pointCount) {
  if (packet.header.type != MessageType::CONFIG || !packet.payload ||
      packet.payloadLength < 14 ||
      packet.payload[0] != static_cast<uint8_t>(ConfigSubtype::FENCE_CHUNK)) return false;
  pointCount = packet.payload[5];
  if (pointCount < 1 || pointCount > POINTS_PER_CHUNK) return false;
  const size_t expected = 6 + pointCount * 8;
  if (packet.payloadLength != expected || !pointsOut) return false;
  fenceVersion = readU16BE(packet.payload + 1);
  chunkIndex = packet.payload[3];
  firstVertex = packet.payload[4];
  size_t pos = 6;
  for (uint8_t i = 0; i < pointCount; ++i) {
    pointsOut[i].lat = readI32BE(packet.payload + pos); pos += 4;
    pointsOut[i].lon = readI32BE(packet.payload + pos); pos += 4;
  }
  return true;
}

bool decodeFenceCommit(const PacketView& packet,
                       uint16_t& fenceVersion, uint32_t& fenceCrc32) {
  if (!checkSubtype(packet, MessageType::CONFIG,
                    static_cast<uint8_t>(ConfigSubtype::FENCE_COMMIT), 7)) return false;
  fenceVersion = readU16BE(packet.payload + 1);
  fenceCrc32 = readU32BE(packet.payload + 3);
  return true;
}

bool decodeParameters(const PacketView& packet, ParametersPayload& out) {
  if (!checkSubtype(packet, MessageType::CONFIG,
                    static_cast<uint8_t>(ConfigSubtype::PARAMETERS), 13)) return false;
  out.muitoPertoM = readU16BE(packet.payload + 1);
  out.pertoM = readU16BE(packet.payload + 3);
  out.telemetryIntervalS = readU32BE(packet.payload + 5);
  out.gpsTimeoutS = readU16BE(packet.payload + 9);
  out.lowBatteryPct = packet.payload[11];
  out.criticalBatteryPct = packet.payload[12];
  return true;
}

bool decodePositionEvent(const PacketView& packet, PositionEventPayload& out) {
  if (packet.header.type != MessageType::EVENT || packet.payloadLength != 17 || !packet.payload) return false;
  const uint8_t subtype = packet.payload[0];
  if (subtype != static_cast<uint8_t>(EventSubtype::OUTSIDE) &&
      subtype != static_cast<uint8_t>(EventSubtype::BACK_INSIDE)) return false;
  out.fenceVersion = readU16BE(packet.payload + 1);
  out.position.lat = readI32BE(packet.payload + 3);
  out.position.lon = readI32BE(packet.payload + 7);
  out.distanceBorderM = readU16BE(packet.payload + 11);
  out.batteryPct = packet.payload[13];
  out.satellites = packet.payload[14];
  out.hdopX100 = readU16BE(packet.payload + 15);
  return true;
}

bool decodeGpsLost(const PacketView& packet, GpsLostPayload& out) {
  if (!checkSubtype(packet, MessageType::EVENT,
                    static_cast<uint8_t>(EventSubtype::GPS_LOST), 11)) return false;
  out.lastPosition.lat = readI32BE(packet.payload + 1);
  out.lastPosition.lon = readI32BE(packet.payload + 5);
  out.positionAgeS = readU16BE(packet.payload + 9);
  return true;
}

bool decodeGpsRecovered(const PacketView& packet, GpsRecoveredPayload& out) {
  if (!checkSubtype(packet, MessageType::EVENT,
                    static_cast<uint8_t>(EventSubtype::GPS_RECOVERED), 12)) return false;
  out.position.lat = readI32BE(packet.payload + 1);
  out.position.lon = readI32BE(packet.payload + 5);
  out.satellites = packet.payload[9];
  out.hdopX100 = readU16BE(packet.payload + 10);
  return true;
}

bool decodeBatteryEvent(const PacketView& packet, BatteryEventPayload& out) {
  if (packet.header.type != MessageType::EVENT || packet.payloadLength != 12 || !packet.payload) return false;
  const uint8_t subtype = packet.payload[0];
  if (subtype != static_cast<uint8_t>(EventSubtype::LOW_BATTERY) &&
      subtype != static_cast<uint8_t>(EventSubtype::CRITICAL_BATTERY)) return false;
  out.batteryPct = packet.payload[1];
  out.lastPosition.lat = readI32BE(packet.payload + 2);
  out.lastPosition.lon = readI32BE(packet.payload + 6);
  out.positionAgeS = readU16BE(packet.payload + 10);
  return true;
}

bool decodeDeviceStarted(const PacketView& packet, DeviceStartedPayload& out) {
  if (!checkSubtype(packet, MessageType::EVENT,
                    static_cast<uint8_t>(EventSubtype::DEVICE_STARTED), 5)) return false;
  out.resetReason = static_cast<ResetReason>(packet.payload[1]);
  out.activeFenceVersion = readU16BE(packet.payload + 2);
  out.batteryPct = packet.payload[4];
  return true;
}

bool decodeTelemetry(const PacketView& packet, TelemetryPayload& out) {
  if (!checkSubtype(packet, MessageType::TELEMETRY,
                    static_cast<uint8_t>(TelemetrySubtype::POSITION_STATUS), 18)) return false;
  out.position.lat = readI32BE(packet.payload + 1);
  out.position.lon = readI32BE(packet.payload + 5);
  out.distanceBorderM = readU16BE(packet.payload + 9);
  out.batteryPct = packet.payload[11];
  out.satellites = packet.payload[12];
  out.hdopX100 = readU16BE(packet.payload + 13);
  out.stateFlags = packet.payload[15];
  out.fenceVersion = readU16BE(packet.payload + 16);
  return true;
}

bool decodeAck(const PacketView& packet, AckPayload& out) {
  if (!checkSubtype(packet, MessageType::CONTROL,
                    static_cast<uint8_t>(ControlSubtype::ACK), 4)) return false;
  out.ackedSequence = readU16BE(packet.payload + 1);
  out.status = packet.payload[3];
  return true;
}

bool decodeNack(const PacketView& packet, NackPayload& out) {
  if (!checkSubtype(packet, MessageType::CONTROL,
                    static_cast<uint8_t>(ControlSubtype::NACK), 6)) return false;
  out.rejectedSequence = readU16BE(packet.payload + 1);
  out.errorCode = static_cast<ErrorCode>(packet.payload[3]);
  out.detail = readU16BE(packet.payload + 4);
  return true;
}

bool decodePingPong(const PacketView& packet, uint16_t& token) {
  if (packet.header.type != MessageType::CONTROL || packet.payloadLength != 3 || !packet.payload) return false;
  const uint8_t subtype = packet.payload[0];
  if (subtype != static_cast<uint8_t>(ControlSubtype::PING) &&
      subtype != static_cast<uint8_t>(ControlSubtype::PONG)) return false;
  token = readU16BE(packet.payload + 1);
  return true;
}

} // namespace vfence
