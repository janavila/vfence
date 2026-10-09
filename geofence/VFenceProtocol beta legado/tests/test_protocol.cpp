#include <cassert>
#include <cmath>
#include <cstring>
#include <iostream>

#include "../vfence_protocol.h"
#include "../vfence_geofence.h"
#include "../vfence_fence_transfer.h"

using namespace vfence;

int main() {
  // Standard CRC-32/ISO-HDLC check vector.
  const char* s = "123456789";
  assert(crc32IsoHdlc(reinterpret_cast<const uint8_t*>(s), 9) == 0xCBF43926u);

  // Big-endian primitives.
  uint8_t tmp[4];
  writeU16BE(tmp, 0x1234);
  assert(tmp[0] == 0x12 && tmp[1] == 0x34);
  assert(readU16BE(tmp) == 0x1234);

  writeU32BE(tmp, 0x89ABCDEFu);
  assert(tmp[0] == 0x89 && tmp[1] == 0xAB && tmp[2] == 0xCD && tmp[3] == 0xEF);
  assert(readU32BE(tmp) == 0x89ABCDEFu);

  // Telemetry round trip.
  TelemetryPayload t{};
  t.position = {degreesToE7(-31.313247), degreesToE7(-54.086903)};
  t.distanceBorderM = 7;
  t.batteryPct = 83;
  t.satellites = 10;
  t.hdopX100 = 130;
  t.stateFlags = STATE_INSIDE | STATE_VERY_NEAR | STATE_GPS_VALID | STATE_BUZZER_ACTIVE;
  t.fenceVersion = 12;

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t n = encodeTelemetry(packet, sizeof(packet), 0x01, 0x1234, t);
  assert(n == 23);

  PacketView view{};
  assert(decodePacket(packet, n, view));
  assert(view.header.type == MessageType::TELEMETRY);
  assert(view.header.sequence == 0x1234);
  assert(view.header.collarId == 0x01);
  assert(view.payloadLength == 18);

  TelemetryPayload td{};
  assert(decodeTelemetry(view, td));
  assert(td.position.lat == t.position.lat);
  assert(td.position.lon == t.position.lon);
  assert(td.distanceBorderM == 7);
  assert(td.hdopX100 == 130);
  assert(td.fenceVersion == 12);

  // Fence transfer and CRC.
  const CoordinateE7 fence[4] = {
      {degreesToE7(-31.3131703), degreesToE7(-54.0868848)},
      {degreesToE7(-31.3131703), degreesToE7(-54.0867535)},
      {degreesToE7(-31.3133945), degreesToE7(-54.0867535)},
      {degreesToE7(-31.3133945), degreesToE7(-54.0868848)}
  };
  const uint16_t fenceVersion = 5;
  const uint32_t fenceCrc = computeFenceCrc32(fenceVersion, fence, 4);
  assert(fenceCrc != 0);

  FenceAssembler assembler;
  assert(assembler.begin(fenceVersion, 4, 2, fenceCrc));
  assert(assembler.missingBitmap() == 0x0003);
  assert(assembler.addChunk(fenceVersion, 0, 0, fence, 2) == ChunkResult::OK);
  assert(assembler.missingBitmap() == 0x0002);
  assert(assembler.addChunk(fenceVersion, 0, 0, fence, 2) == ChunkResult::DUPLICATE_IDENTICAL);
  assert(assembler.addChunk(fenceVersion, 1, 2, fence + 2, 2) == ChunkResult::OK);
  assert(assembler.complete());
  assert(assembler.crcValid());
  ErrorCode err = ErrorCode::BUSY;
  uint16_t detail = 999;
  assert(assembler.validateCommit(fenceVersion, fenceCrc, err, detail));

  // Geofence basic check.
  GeofenceEngine engine;
  assert(engine.setFence(fence, 4, fenceVersion));
  assert(engine.setThresholds(10.0, 30.0));

  CoordinateE7 inside{degreesToE7(-31.31328), degreesToE7(-54.08682)};
  GeofenceResult r1 = engine.analyze(inside);
  assert(r1.valid);
  assert(r1.inside);

  CoordinateE7 outside{degreesToE7(-31.31328), degreesToE7(-54.08700)};
  GeofenceResult r2 = engine.analyze(outside);
  assert(r2.valid);
  assert(!r2.inside);

  // FENCE_CHUNK encode/decode size check.
  const size_t fc = encodeFenceChunk(packet, sizeof(packet), 1, 10, false,
                                     fenceVersion, 0, 0, fence, 2);
  assert(fc == 27);
  assert(decodePacket(packet, fc, view));
  CoordinateE7 decodedPoints[2];
  uint16_t fv;
  uint8_t chunkIndex, firstVertex, pointCount;
  assert(decodeFenceChunk(view, fv, chunkIndex, firstVertex, decodedPoints, pointCount));
  assert(fv == fenceVersion && chunkIndex == 0 && firstVertex == 0 && pointCount == 2);
  assert(decodedPoints[0].lat == fence[0].lat && decodedPoints[1].lon == fence[1].lon);

  std::cout << "All VFENCE Protocol v1 tests passed.\n";
  return 0;
}
