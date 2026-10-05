#pragma once

#include <stddef.h>
#include <stdint.h>

namespace vfence {

static const uint8_t PROTOCOL_VERSION = 0;
static const size_t HEADER_SIZE = 5;
static const size_t MAX_PACKET_SIZE = 64;
static const size_t MAX_VERTICES = 32;
static const size_t MAX_CHUNKS = 16;
static const uint8_t POINTS_PER_CHUNK = 2;

static const uint8_t FLAG_ACK_REQ = 0x08;
static const uint8_t FLAG_RETRY   = 0x04;
static const uint8_t FLAG_URGENT  = 0x02;
static const uint8_t FLAG_RESERVED = 0x01;

enum class MessageType : uint8_t {
  CONFIG = 0,
  EVENT = 1,
  TELEMETRY = 2,
  CONTROL = 3
};

enum class ConfigSubtype : uint8_t {
  FENCE_BEGIN = 0x01,
  FENCE_CHUNK = 0x02,
  FENCE_COMMIT = 0x03,
  PARAMETERS = 0x04
};

enum class EventSubtype : uint8_t {
  OUTSIDE = 0x01,
  BACK_INSIDE = 0x02,
  GPS_LOST = 0x03,
  GPS_RECOVERED = 0x04,
  LOW_BATTERY = 0x05,
  CRITICAL_BATTERY = 0x06,
  DEVICE_STARTED = 0x07
};

enum class TelemetrySubtype : uint8_t {
  POSITION_STATUS = 0x01
};

enum class ControlSubtype : uint8_t {
  ACK = 0x01,
  NACK = 0x02,
  PING = 0x03,
  PONG = 0x04
};

enum class ErrorCode : uint8_t {
  INVALID_LENGTH = 0x01,
  UNKNOWN_SUBTYPE = 0x02,
  INVALID_COLLAR_ID = 0x03,
  UNSUPPORTED_VERSION = 0x04,
  INVALID_PARAMETER = 0x05,
  INVALID_FENCE_VERSION = 0x06,
  INVALID_CHUNK = 0x07,
  MISSING_CHUNKS = 0x08,
  CRC_ERROR = 0x09,
  COMMIT_REJECTED = 0x0A,
  BUSY = 0x0B,
  PERSISTENCE_ERROR = 0x0C
};

enum class ResetReason : uint8_t {
  UNKNOWN = 0x00,
  POWER_ON = 0x01,
  SOFTWARE = 0x02,
  WATCHDOG = 0x03,
  BROWNOUT = 0x04,
  OTHER = 0x05
};

struct CoordinateE7 {
  int32_t lat;
  int32_t lon;
};

struct Header {
  uint8_t version;
  MessageType type;
  bool ackReq;
  bool retry;
  bool urgent;
  uint8_t collarId;
  uint16_t sequence;
  uint8_t payloadLength;
};

struct PacketView {
  Header header;
  const uint8_t* payload;
  size_t payloadLength;
};

struct ParametersPayload {
  uint16_t muitoPertoM;
  uint16_t pertoM;
  uint32_t telemetryIntervalS;
  uint16_t gpsTimeoutS;
  uint8_t lowBatteryPct;
  uint8_t criticalBatteryPct;
};

struct PositionEventPayload {
  uint16_t fenceVersion;
  CoordinateE7 position;
  uint16_t distanceBorderM;
  uint8_t batteryPct;
  uint8_t satellites;
  uint16_t hdopX100;
};

struct GpsLostPayload {
  CoordinateE7 lastPosition;
  uint16_t positionAgeS;
};

struct GpsRecoveredPayload {
  CoordinateE7 position;
  uint8_t satellites;
  uint16_t hdopX100;
};

struct BatteryEventPayload {
  uint8_t batteryPct;
  CoordinateE7 lastPosition;
  uint16_t positionAgeS;
};

struct DeviceStartedPayload {
  ResetReason resetReason;
  uint16_t activeFenceVersion;
  uint8_t batteryPct;
};

struct TelemetryPayload {
  CoordinateE7 position;
  uint16_t distanceBorderM;
  uint8_t batteryPct;
  uint8_t satellites;
  uint16_t hdopX100;
  uint8_t stateFlags;
  uint16_t fenceVersion;
};

struct AckPayload {
  uint16_t ackedSequence;
  uint8_t status;
};

struct NackPayload {
  uint16_t rejectedSequence;
  ErrorCode errorCode;
  uint16_t detail;
};

// stateFlags for POSITION_STATUS
static const uint8_t STATE_INSIDE = 0x01;
static const uint8_t STATE_NEAR = 0x02;
static const uint8_t STATE_VERY_NEAR = 0x04;
static const uint8_t STATE_GPS_VALID = 0x08;
static const uint8_t STATE_LOW_BATTERY = 0x10;
static const uint8_t STATE_CRITICAL_BATTERY = 0x20;
static const uint8_t STATE_BUZZER_ACTIVE = 0x40;
static const uint8_t STATE_SECOND_ACTUATOR_ACTIVE = 0x80;

static const int32_t COORD_UNAVAILABLE = INT32_MIN;
static const uint16_t U16_UNAVAILABLE = 0xFFFF;
static const uint8_t U8_UNAVAILABLE = 0xFF;

uint8_t makeControlByte(MessageType type, bool ackReq, bool retry, bool urgent,
                        uint8_t version = PROTOCOL_VERSION);

bool encodeHeader(uint8_t* out, size_t capacity, const Header& header);
bool decodePacket(const uint8_t* data, size_t len, PacketView& out);

void writeU16BE(uint8_t* p, uint16_t v);
void writeU32BE(uint8_t* p, uint32_t v);
void writeI32BE(uint8_t* p, int32_t v);
uint16_t readU16BE(const uint8_t* p);
uint32_t readU32BE(const uint8_t* p);
int32_t readI32BE(const uint8_t* p);

int32_t degreesToE7(double degrees);
double e7ToDegrees(int32_t value);

uint32_t crc32IsoHdlc(const uint8_t* data, size_t len);
uint32_t computeFenceCrc32(uint16_t fenceVersion,
                           const CoordinateE7* points,
                           uint8_t vertexCount);

// CONFIG encoders
size_t encodeFenceBegin(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        uint16_t fenceVersion, uint8_t vertexCount,
                        uint8_t chunkCount, uint32_t fenceCrc32);

size_t encodeFenceChunk(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        uint16_t fenceVersion, uint8_t chunkIndex,
                        uint8_t firstVertex, const CoordinateE7* points,
                        uint8_t pointCount);

size_t encodeFenceCommit(uint8_t* out, size_t capacity,
                         uint8_t collarId, uint16_t sequence, bool retry,
                         uint16_t fenceVersion, uint32_t fenceCrc32);

size_t encodeParameters(uint8_t* out, size_t capacity,
                        uint8_t collarId, uint16_t sequence, bool retry,
                        const ParametersPayload& params);

// EVENT encoders
size_t encodePositionEvent(uint8_t* out, size_t capacity,
                           uint8_t collarId, uint16_t sequence, bool retry,
                           EventSubtype subtype,
                           const PositionEventPayload& eventData);

size_t encodeGpsLost(uint8_t* out, size_t capacity,
                     uint8_t collarId, uint16_t sequence, bool retry,
                     const GpsLostPayload& data);

size_t encodeGpsRecovered(uint8_t* out, size_t capacity,
                          uint8_t collarId, uint16_t sequence, bool retry,
                          const GpsRecoveredPayload& data);

size_t encodeBatteryEvent(uint8_t* out, size_t capacity,
                          uint8_t collarId, uint16_t sequence, bool retry,
                          EventSubtype subtype,
                          const BatteryEventPayload& data);

size_t encodeDeviceStarted(uint8_t* out, size_t capacity,
                           uint8_t collarId, uint16_t sequence, bool retry,
                           const DeviceStartedPayload& data);

// TELEMETRY
size_t encodeTelemetry(uint8_t* out, size_t capacity,
                       uint8_t collarId, uint16_t sequence,
                       const TelemetryPayload& data);

// CONTROL
size_t encodeAck(uint8_t* out, size_t capacity,
                 uint8_t collarId, uint16_t sequence,
                 uint16_t ackedSequence, uint8_t status = 0x00);

size_t encodeNack(uint8_t* out, size_t capacity,
                  uint8_t collarId, uint16_t sequence,
                  uint16_t rejectedSequence, ErrorCode errorCode,
                  uint16_t detail = 0);

size_t encodePingPong(uint8_t* out, size_t capacity,
                      uint8_t collarId, uint16_t sequence,
                      ControlSubtype subtype, uint16_t token);

// Payload decoders. The PacketView must already be validated by decodePacket().
bool decodeFenceBegin(const PacketView& packet,
                      uint16_t& fenceVersion, uint8_t& vertexCount,
                      uint8_t& chunkCount, uint32_t& fenceCrc32);

bool decodeFenceChunk(const PacketView& packet,
                      uint16_t& fenceVersion, uint8_t& chunkIndex,
                      uint8_t& firstVertex, CoordinateE7* pointsOut,
                      uint8_t& pointCount);

bool decodeFenceCommit(const PacketView& packet,
                       uint16_t& fenceVersion, uint32_t& fenceCrc32);

bool decodeParameters(const PacketView& packet, ParametersPayload& out);
bool decodePositionEvent(const PacketView& packet, PositionEventPayload& out);
bool decodeGpsLost(const PacketView& packet, GpsLostPayload& out);
bool decodeGpsRecovered(const PacketView& packet, GpsRecoveredPayload& out);
bool decodeBatteryEvent(const PacketView& packet, BatteryEventPayload& out);
bool decodeDeviceStarted(const PacketView& packet, DeviceStartedPayload& out);
bool decodeTelemetry(const PacketView& packet, TelemetryPayload& out);
bool decodeAck(const PacketView& packet, AckPayload& out);
bool decodeNack(const PacketView& packet, NackPayload& out);
bool decodePingPong(const PacketView& packet, uint16_t& token);

} // namespace vfence
