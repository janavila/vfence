#include <Arduino.h>
#include <TinyGPSPlus.h>
#include <Preferences.h>

#include "vfence_protocol.h"
#include "vfence_geofence.h"
#include "vfence_fence_transfer.h"

using namespace vfence;

// ============================================================
// HARDWARE / IDENTIDADE
// ============================================================

static const uint8_t COLLAR_ID = 0x01;
static const int GPS_RX = 23;
static const uint32_t GPS_BAUD = 115200;

TinyGPSPlus gps;
HardwareSerial GPSserial(1);
Preferences prefs;

// ============================================================
// ESTADO VFENCE
// ============================================================

GeofenceEngine geofence;
FenceAssembler pendingFence;

CoordinateE7 activeFence[MAX_VERTICES];
uint8_t activeFenceCount = 0;
uint16_t activeFenceVersion = 0;

ParametersPayload params = {
  10,   // muitoPertoM
  30,   // pertoM
  10,   // telemetryIntervalS
  60,   // gpsTimeoutS
  20,   // lowBatteryPct
  5     // criticalBatteryPct
};

CoordinateE7 latestPosition = {COORD_UNAVAILABLE, COORD_UNAVAILABLE};
GeofenceResult latestGeofence = {false, false, 0.0, ProximityState::FAR};

bool gpsEverValid = false;
bool gpsLostState = false;
uint32_t gpsMonitorStartMs = 0;
uint32_t lastValidGpsMs = 0;

bool haveFenceState = false;
bool lastInside = true;

bool buzzerActive = false;
bool secondActuatorActive = false;

uint16_t nextSequence = 0;
uint32_t nextTelemetryAtMs = 0;

// Bateria ainda nao integrada ao hardware.
// 0xFF = indisponivel, conforme o protocolo.
uint8_t batteryPct = U8_UNAVAILABLE;

// ============================================================
// CONFIABILIDADE: ACK / RETRY
// ============================================================

static const uint8_t MAX_PENDING_RELIABLE = 8;
static const uint8_t MAX_TRANSMISSIONS = 4;   // 1 original + 3 retries
static const uint32_t ACK_TIMEOUT_MS = 5000;
static const uint32_t RETRY_JITTER_MS = 2000;

struct PendingReliableTx {
  bool active;
  uint16_t sequence;
  uint8_t transmissions;
  uint32_t nextActionMs;
  size_t length;
  uint8_t packet[MAX_PACKET_SIZE];
};

PendingReliableTx pendingTx[MAX_PENDING_RELIABLE];

// ============================================================
// TRANSPORTE
// ============================================================
// Nesta etapa o protocolo esta implementado, mas o driver LoRa
// fica propositalmente isolado. Hoje este metodo apenas imprime
// os bytes no Monitor Serial.
//
// Quando ligarmos o SX127x, esta funcao sera o ponto de troca:
//   LoRa.beginPacket();
//   LoRa.write(data, len);
//   LoRa.endPacket();
// ============================================================

void printHexPacket(const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    if (data[i] < 0x10) Serial.print('0');
    Serial.print(data[i], HEX);
    if (i + 1 < len) Serial.print(' ');
  }
}

bool transportSend(const uint8_t* data, size_t len) {
  Serial.print("[TX ");
  Serial.print(len);
  Serial.print(" bytes] ");
  printHexPacket(data, len);
  Serial.println();
  return true;
}

uint16_t allocateSequence() {
  return nextSequence++;
}

uint32_t scheduleRetryFromNow() {
  return millis() + ACK_TIMEOUT_MS + random(0, RETRY_JITTER_MS + 1);
}

bool sendReliable(const uint8_t* packet, size_t len) {
  PacketView view;
  if (!decodePacket(packet, len, view) || !view.header.ackReq) {
    Serial.println("[PROTO] sendReliable recebeu pacote invalido/sem ACK_REQ");
    return false;
  }

  int slot = -1;
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    if (!pendingTx[i].active) {
      slot = i;
      break;
    }
  }

  if (slot < 0) {
    Serial.println("[PROTO] fila de mensagens confiaveis cheia");
    return false;
  }

  PendingReliableTx& tx = pendingTx[slot];
  tx.active = true;
  tx.sequence = view.header.sequence;
  tx.transmissions = 1;
  tx.length = len;
  memcpy(tx.packet, packet, len);
  tx.nextActionMs = scheduleRetryFromNow();

  return transportSend(tx.packet, tx.length);
}

void acknowledgeReliable(uint16_t sequence) {
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    if (pendingTx[i].active && pendingTx[i].sequence == sequence) {
      pendingTx[i].active = false;
      Serial.print("[ACK] mensagem ");
      Serial.print(sequence);
      Serial.println(" confirmada");
      return;
    }
  }
}

void rejectReliable(uint16_t sequence, ErrorCode errorCode, uint16_t detail) {
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    if (pendingTx[i].active && pendingTx[i].sequence == sequence) {
      pendingTx[i].active = false;
      break;
    }
  }

  Serial.print("[NACK] seq=");
  Serial.print(sequence);
  Serial.print(" erro=0x");
  Serial.print(static_cast<uint8_t>(errorCode), HEX);
  Serial.print(" detail=0x");
  Serial.println(detail, HEX);
}

void serviceReliableTx() {
  const uint32_t now = millis();

  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (!tx.active) continue;

    if (static_cast<int32_t>(now - tx.nextActionMs) < 0) continue;

    if (tx.transmissions >= MAX_TRANSMISSIONS) {
      Serial.print("[PROTO] falha definitiva de entrega seq=");
      Serial.println(tx.sequence);
      tx.active = false;
      continue;
    }

    // Mesmo sequence, mas marca RETRY no byte de controle.
    tx.packet[0] |= FLAG_RETRY;
    ++tx.transmissions;

    Serial.print("[RETRY] seq=");
    Serial.print(tx.sequence);
    Serial.print(" tentativa=");
    Serial.println(tx.transmissions);

    transportSend(tx.packet, tx.length);
    tx.nextActionMs = scheduleRetryFromNow();
  }
}

// ============================================================
// ACK / NACK / CONTROL
// ============================================================

void sendAckResponse(uint16_t ackedSequence) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeAck(packet, sizeof(packet), COLLAR_ID,
                               allocateSequence(), ackedSequence, 0x00);
  if (len) transportSend(packet, len);
}

void sendNackResponse(uint16_t rejectedSequence, ErrorCode error, uint16_t detail = 0) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeNack(packet, sizeof(packet), COLLAR_ID,
                                allocateSequence(), rejectedSequence,
                                error, detail);
  if (len) transportSend(packet, len);
}


void ackIncoming(const PacketView& packet) {
  // Broadcast nunca gera ACK para evitar uma tempestade de respostas.
  if (packet.header.collarId == 0xFF || !packet.header.ackReq) return;
  sendAckResponse(packet.header.sequence);
}

void nackIncoming(const PacketView& packet, ErrorCode error, uint16_t detail = 0) {
  // Mesmo para erros, broadcast e ignorado silenciosamente para nao fazer
  // varias coleiras responderem ao gateway ao mesmo tempo.
  if (packet.header.collarId == 0xFF) return;
  sendNackResponse(packet.header.sequence, error, detail);
}

void sendPong(uint16_t token) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodePingPong(packet, sizeof(packet), COLLAR_ID,
                                    allocateSequence(), ControlSubtype::PONG,
                                    token);
  if (len) transportSend(packet, len);
}

// ============================================================
// PERSISTENCIA
// ============================================================

bool saveFenceToNvs() {
  if (activeFenceCount < 3 || activeFenceCount > MAX_VERTICES) return false;

  prefs.begin("vfence", false);
  bool ok = true;
  ok &= prefs.putUShort("fver", activeFenceVersion) > 0;
  ok &= prefs.putUChar("fcnt", activeFenceCount) > 0;
  ok &= prefs.putBytes("fpts", activeFence,
                       activeFenceCount * sizeof(CoordinateE7)) ==
        activeFenceCount * sizeof(CoordinateE7);
  prefs.end();
  return ok;
}

bool loadFenceFromNvs() {
  prefs.begin("vfence", true);
  const uint8_t count = prefs.getUChar("fcnt", 0);
  const uint16_t version = prefs.getUShort("fver", 0);
  const size_t storedBytes = prefs.getBytesLength("fpts");

  if (count < 3 || count > MAX_VERTICES ||
      storedBytes != count * sizeof(CoordinateE7)) {
    prefs.end();
    return false;
  }

  CoordinateE7 temp[MAX_VERTICES];
  const size_t readBytes = prefs.getBytes("fpts", temp,
                                          count * sizeof(CoordinateE7));
  prefs.end();

  if (readBytes != count * sizeof(CoordinateE7)) return false;
  if (!geofence.setFence(temp, count, version)) return false;

  memcpy(activeFence, temp, count * sizeof(CoordinateE7));
  activeFenceCount = count;
  activeFenceVersion = version;
  return true;
}

void saveParametersToNvs() {
  prefs.begin("vfence", false);
  prefs.putUShort("vnear", params.muitoPertoM);
  prefs.putUShort("near", params.pertoM);
  prefs.putUInt("tint", params.telemetryIntervalS);
  prefs.putUShort("gtmo", params.gpsTimeoutS);
  prefs.putUChar("blow", params.lowBatteryPct);
  prefs.putUChar("bcrit", params.criticalBatteryPct);
  prefs.end();
}

void loadParametersFromNvs() {
  prefs.begin("vfence", true);
  params.muitoPertoM = prefs.getUShort("vnear", params.muitoPertoM);
  params.pertoM = prefs.getUShort("near", params.pertoM);
  params.telemetryIntervalS = prefs.getUInt("tint", params.telemetryIntervalS);
  params.gpsTimeoutS = prefs.getUShort("gtmo", params.gpsTimeoutS);
  params.lowBatteryPct = prefs.getUChar("blow", params.lowBatteryPct);
  params.criticalBatteryPct = prefs.getUChar("bcrit", params.criticalBatteryPct);
  prefs.end();

  // Protecao contra configuracao persistida invalida.
  if (params.muitoPertoM > params.pertoM) {
    params.muitoPertoM = 10;
    params.pertoM = 30;
  }
  if (params.telemetryIntervalS == 0 || params.telemetryIntervalS > 60) {
    params.telemetryIntervalS = 10;
  }
  if (params.gpsTimeoutS == 0) params.gpsTimeoutS = 60;
  if (params.lowBatteryPct > 100) params.lowBatteryPct = 20;
  if (params.criticalBatteryPct > params.lowBatteryPct) params.criticalBatteryPct = 5;

  geofence.setThresholds(params.muitoPertoM, params.pertoM);
}

// Cerca usada somente quando ainda nao existe cerca persistida.
void loadDevelopmentFence() {
  const CoordinateE7 testFence[] = {
    {-313131703, -540868848},
    {-313131703, -540867535},
    {-313133945, -540867535},
    {-313133945, -540868848}
  };

  activeFenceCount = 4;
  activeFenceVersion = 1;
  memcpy(activeFence, testFence, sizeof(testFence));
  geofence.setFence(activeFence, activeFenceCount, activeFenceVersion);
  saveFenceToNvs();

  Serial.println("[FENCE] nenhuma cerca persistida; cerca de desenvolvimento carregada");
}

// ============================================================
// ATUADORES
// ============================================================

void setBuzzer(bool on) {
  if (buzzerActive == on) return;
  buzzerActive = on;
  Serial.println(on ? "[ATUADOR] buzzer ON" : "[ATUADOR] buzzer OFF");
  // TODO: implementar GPIO do buzzer.
}

void setSecondActuator(bool on) {
  if (secondActuatorActive == on) return;
  secondActuatorActive = on;
  Serial.println(on ? "[ATUADOR] segundo atuador ON" : "[ATUADOR] segundo atuador OFF");
  // TODO: implementar motor/vibracao da prova de conceito.
}

void applyActuators(const GeofenceResult& result) {
  if (!result.valid) return;

  if (!result.inside || result.proximity == ProximityState::VERY_NEAR) {
    setBuzzer(true);
    setSecondActuator(true);
  } else if (result.proximity == ProximityState::NEAR) {
    setBuzzer(true);
    setSecondActuator(false);
  } else {
    setBuzzer(false);
    setSecondActuator(false);
  }
}

// ============================================================
// HELPERS DE SENSORES / ESTADO
// ============================================================

uint16_t currentHdopX100() {
  if (!gps.hdop.isValid()) return U16_UNAVAILABLE;
  const double v = gps.hdop.hdop() * 100.0;
  if (v < 0.0 || v > 65534.0) return U16_UNAVAILABLE;
  return static_cast<uint16_t>(lround(v));
}

uint8_t currentSatellites() {
  if (!gps.satellites.isValid()) return U8_UNAVAILABLE;
  const uint32_t s = gps.satellites.value();
  return s > 254 ? 254 : static_cast<uint8_t>(s);
}

uint16_t clampDistanceMeters(double meters) {
  if (meters < 0.0) return 0;
  if (meters > 65534.0) return 65534;
  return static_cast<uint16_t>(lround(meters));
}

uint16_t positionAgeSeconds() {
  if (!gpsEverValid) return U16_UNAVAILABLE;
  const uint32_t ageS = (millis() - lastValidGpsMs) / 1000UL;
  return ageS > 65534UL ? 65534 : static_cast<uint16_t>(ageS);
}

// ============================================================
// EVENTOS E TELEMETRIA
// ============================================================

void sendOutsideOrBackInside(EventSubtype subtype) {
  if (!latestGeofence.valid) return;

  PositionEventPayload eventData;
  eventData.fenceVersion = activeFenceVersion;
  eventData.position = latestPosition;
  eventData.distanceBorderM = clampDistanceMeters(latestGeofence.distanceBorderM);
  eventData.batteryPct = batteryPct;
  eventData.satellites = currentSatellites();
  eventData.hdopX100 = currentHdopX100();

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodePositionEvent(packet, sizeof(packet), COLLAR_ID,
                                         allocateSequence(), false,
                                         subtype, eventData);
  if (len) sendReliable(packet, len);
}

void sendGpsLostEvent() {
  GpsLostPayload d;
  d.lastPosition = gpsEverValid
      ? latestPosition
      : CoordinateE7{COORD_UNAVAILABLE, COORD_UNAVAILABLE};
  d.positionAgeS = positionAgeSeconds();

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeGpsLost(packet, sizeof(packet), COLLAR_ID,
                                   allocateSequence(), false, d);
  if (len) sendReliable(packet, len);
}

void sendGpsRecoveredEvent() {
  GpsRecoveredPayload d;
  d.position = latestPosition;
  d.satellites = currentSatellites();
  d.hdopX100 = currentHdopX100();

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeGpsRecovered(packet, sizeof(packet), COLLAR_ID,
                                        allocateSequence(), false, d);
  if (len) sendReliable(packet, len);
}

void sendDeviceStartedEvent() {
  DeviceStartedPayload d;
  d.resetReason = ResetReason::POWER_ON;  // depois podemos mapear esp_reset_reason().
  d.activeFenceVersion = activeFenceVersion;
  d.batteryPct = batteryPct;

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeDeviceStarted(packet, sizeof(packet), COLLAR_ID,
                                         allocateSequence(), false, d);
  if (len) sendReliable(packet, len);
}

uint8_t buildStateFlags() {
  uint8_t flags = 0;

  if (latestGeofence.valid && latestGeofence.inside) flags |= STATE_INSIDE;
  if (latestGeofence.valid && latestGeofence.proximity == ProximityState::NEAR) {
    flags |= STATE_NEAR;
  }
  if (latestGeofence.valid && latestGeofence.proximity == ProximityState::VERY_NEAR) {
    flags |= STATE_VERY_NEAR;
  }
  if (!gpsLostState && gpsEverValid) flags |= STATE_GPS_VALID;
  if (batteryPct != U8_UNAVAILABLE) {
    if (batteryPct <= params.criticalBatteryPct) flags |= STATE_CRITICAL_BATTERY;
    else if (batteryPct <= params.lowBatteryPct) flags |= STATE_LOW_BATTERY;
  }
  if (buzzerActive) flags |= STATE_BUZZER_ACTIVE;
  if (secondActuatorActive) flags |= STATE_SECOND_ACTUATOR_ACTIVE;

  return flags;
}

void scheduleNextTelemetry() {
  const uint32_t base = params.telemetryIntervalS * 1000UL;
  nextTelemetryAtMs = millis() + base + random(0, 3001);
}

void sendTelemetryPacket() {
  TelemetryPayload d;

  if (!gpsLostState && gpsEverValid) {
    d.position = latestPosition;
    d.distanceBorderM = latestGeofence.valid
        ? clampDistanceMeters(latestGeofence.distanceBorderM)
        : U16_UNAVAILABLE;
    d.hdopX100 = currentHdopX100();
    d.satellites = currentSatellites();
  } else {
    d.position = {COORD_UNAVAILABLE, COORD_UNAVAILABLE};
    d.distanceBorderM = U16_UNAVAILABLE;
    d.hdopX100 = U16_UNAVAILABLE;
    d.satellites = U8_UNAVAILABLE;
  }

  d.batteryPct = batteryPct;
  d.stateFlags = buildStateFlags();
  d.fenceVersion = activeFenceVersion;

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeTelemetry(packet, sizeof(packet), COLLAR_ID,
                                     allocateSequence(), d);
  if (len) transportSend(packet, len);
}

// ============================================================
// RECEPCAO DO PROTOCOLO
// ============================================================

bool validParameters(const ParametersPayload& p, uint16_t& badField) {
  // detail do NACK: 1=muitoPerto, 2=perto, 3=telemetryInterval,
  // 4=gpsTimeout, 5=lowBattery, 6=criticalBattery.
  if (p.muitoPertoM > p.pertoM) { badField = 1; return false; }
  if (p.telemetryIntervalS == 0 || p.telemetryIntervalS > 60) { badField = 3; return false; }
  if (p.gpsTimeoutS == 0) { badField = 4; return false; }
  if (p.lowBatteryPct > 100) { badField = 5; return false; }
  if (p.criticalBatteryPct > p.lowBatteryPct) { badField = 6; return false; }
  return true;
}

void processConfig(const PacketView& packet) {
  if (packet.payloadLength < 1) {
    nackIncoming(packet, ErrorCode::INVALID_LENGTH);
    return;
  }

  const uint8_t subtype = packet.payload[0];

  if (subtype == static_cast<uint8_t>(ConfigSubtype::FENCE_BEGIN)) {
    uint16_t version;
    uint8_t vertexCount, chunkCount;
    uint32_t crc;

    if (!decodeFenceBegin(packet, version, vertexCount, chunkCount, crc)) {
      nackIncoming(packet, ErrorCode::INVALID_LENGTH);
      return;
    }

    if (!pendingFence.begin(version, vertexCount, chunkCount, crc)) {
      nackIncoming(packet, ErrorCode::INVALID_PARAMETER);
      return;
    }

    ackIncoming(packet);
    Serial.println("[FENCE] inicio de nova cerca aceito");
    return;
  }

  if (subtype == static_cast<uint8_t>(ConfigSubtype::FENCE_CHUNK)) {
    uint16_t version;
    uint8_t chunkIndex, firstVertex, pointCount;
    CoordinateE7 points[POINTS_PER_CHUNK];

    if (!decodeFenceChunk(packet, version, chunkIndex, firstVertex, points, pointCount)) {
      nackIncoming(packet, ErrorCode::INVALID_LENGTH);
      return;
    }

    const ChunkResult result = pendingFence.addChunk(version, chunkIndex,
                                                     firstVertex, points, pointCount);
    if (result == ChunkResult::INVALID || result == ChunkResult::DIFFERENT_DUPLICATE) {
      nackIncoming(packet, ErrorCode::INVALID_CHUNK);
      return;
    }

    Serial.print("[FENCE] chunk recebido: ");
    Serial.println(chunkIndex);
    return;
  }

  if (subtype == static_cast<uint8_t>(ConfigSubtype::FENCE_COMMIT)) {
    uint16_t version;
    uint32_t crc;
    if (!decodeFenceCommit(packet, version, crc)) {
      nackIncoming(packet, ErrorCode::INVALID_LENGTH);
      return;
    }

    ErrorCode error = ErrorCode::COMMIT_REJECTED;
    uint16_t detail = 0;
    if (!pendingFence.validateCommit(version, crc, error, detail)) {
      nackIncoming(packet, error, detail);
      return;
    }

    if (!geofence.setFence(pendingFence.points(), pendingFence.vertexCount(), version)) {
      nackIncoming(packet, ErrorCode::COMMIT_REJECTED);
      return;
    }

    memcpy(activeFence, pendingFence.points(),
           pendingFence.vertexCount() * sizeof(CoordinateE7));
    activeFenceCount = pendingFence.vertexCount();
    activeFenceVersion = version;

    if (!saveFenceToNvs()) {
      nackIncoming(packet, ErrorCode::PERSISTENCE_ERROR);
      return;
    }

    pendingFence.clear();
    ackIncoming(packet);
    Serial.println("[FENCE] nova cerca ativada e persistida");
    return;
  }

  if (subtype == static_cast<uint8_t>(ConfigSubtype::PARAMETERS)) {
    ParametersPayload p;
    if (!decodeParameters(packet, p)) {
      nackIncoming(packet, ErrorCode::INVALID_LENGTH);
      return;
    }

    uint16_t badField = 0;
    if (!validParameters(p, badField)) {
      nackIncoming(packet, ErrorCode::INVALID_PARAMETER, badField);
      return;
    }

    params = p;
    geofence.setThresholds(params.muitoPertoM, params.pertoM);
    saveParametersToNvs();
    scheduleNextTelemetry();
    ackIncoming(packet);
    Serial.println("[CONFIG] parametros atualizados");
    return;
  }

  nackIncoming(packet, ErrorCode::UNKNOWN_SUBTYPE);
}

void processControl(const PacketView& packet) {
  if (packet.payloadLength < 1) return;
  const uint8_t subtype = packet.payload[0];

  if (subtype == static_cast<uint8_t>(ControlSubtype::ACK)) {
    AckPayload ack;
    if (decodeAck(packet, ack)) acknowledgeReliable(ack.ackedSequence);
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::NACK)) {
    NackPayload nack;
    if (decodeNack(packet, nack)) {
      rejectReliable(nack.rejectedSequence, nack.errorCode, nack.detail);
    }
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::PING)) {
    uint16_t token;
    if (decodePingPong(packet, token)) sendPong(token);
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::PONG)) {
    uint16_t token;
    if (decodePingPong(packet, token)) {
      Serial.print("[PONG] token=");
      Serial.println(token);
    }
    return;
  }
}

void processIncomingPacket(const uint8_t* data, size_t len) {
  PacketView packet;
  if (!decodePacket(data, len, packet)) {
    Serial.println("[RX] pacote invalido: tamanho/cabecalho");
    return;
  }

  if (packet.header.collarId != COLLAR_ID && packet.header.collarId != 0xFF) {
    // O radio e compartilhado: pacotes destinados a outra coleira sao
    // simplesmente ignorados. Responder NACK aqui causaria colisoes.
    return;
  }

  if (packet.header.version != PROTOCOL_VERSION) {
    nackIncoming(packet, ErrorCode::UNSUPPORTED_VERSION);
    return;
  }

  switch (packet.header.type) {
    case MessageType::CONFIG:
      processConfig(packet);
      break;
    case MessageType::CONTROL:
      processControl(packet);
      break;
    case MessageType::EVENT:
    case MessageType::TELEMETRY:
      Serial.println("[RX] tipo nao esperado na coleira");
      break;
  }
}

// Permite testar a recepcao ANTES de instalar LoRa.
// No Monitor Serial, envie algo como:
// RX 08 01 00 10 09 01 00 02 04 02 12 34 56 78
// (exemplo meramente ilustrativo; os bytes devem formar um pacote valido.)
void pollSerialPacketInjection() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (!line.startsWith("RX ")) return;

  uint8_t data[MAX_PACKET_SIZE];
  size_t count = 0;
  int start = 3;

  while (start < line.length() && count < MAX_PACKET_SIZE) {
    while (start < line.length() && line[start] == ' ') ++start;
    if (start >= line.length()) break;

    int end = line.indexOf(' ', start);
    if (end < 0) end = line.length();

    String token = line.substring(start, end);
    char* endPtr = nullptr;
    const unsigned long value = strtoul(token.c_str(), &endPtr, 16);
    if (!endPtr || *endPtr != '\0' || value > 255) {
      Serial.println("[RX] token hexadecimal invalido");
      return;
    }

    data[count++] = static_cast<uint8_t>(value);
    start = end + 1;
  }

  Serial.print("[RX SERIAL] ");
  printHexPacket(data, count);
  Serial.println();
  processIncomingPacket(data, count);
}

// ============================================================
// GPS / GEOFENCE
// ============================================================

void processNewGpsPosition() {
  latestPosition.lat = degreesToE7(gps.location.lat());
  latestPosition.lon = degreesToE7(gps.location.lng());

  const bool wasLost = gpsLostState;
  gpsEverValid = true;
  gpsLostState = false;
  lastValidGpsMs = millis();

  latestGeofence = geofence.analyze(latestPosition);
  applyActuators(latestGeofence);

  if (wasLost) sendGpsRecoveredEvent();

  if (latestGeofence.valid) {
    if (!haveFenceState) {
      haveFenceState = true;
      lastInside = latestGeofence.inside;
      if (!latestGeofence.inside) {
        sendOutsideOrBackInside(EventSubtype::OUTSIDE);
      }
    } else if (latestGeofence.inside != lastInside) {
      lastInside = latestGeofence.inside;
      sendOutsideOrBackInside(lastInside
          ? EventSubtype::BACK_INSIDE
          : EventSubtype::OUTSIDE);
    }
  }
}

void serviceGpsTimeout() {
  if (gpsLostState) return;

  const uint32_t reference = gpsEverValid ? lastValidGpsMs : gpsMonitorStartMs;
  const uint32_t timeoutMs = static_cast<uint32_t>(params.gpsTimeoutS) * 1000UL;

  if (millis() - reference >= timeoutMs) {
    gpsLostState = true;
    sendGpsLostEvent();
    Serial.println("[GPS] estado GPS_LOST ativado");
  }
}

// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  Serial.begin(115200);
  delay(1500);

  randomSeed(static_cast<uint32_t>(micros()));

  GPSserial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, -1);

  memset(pendingTx, 0, sizeof(pendingTx));

  loadParametersFromNvs();
  if (!loadFenceFromNvs()) loadDevelopmentFence();

  geofence.setThresholds(params.muitoPertoM, params.pertoM);

  gpsMonitorStartMs = millis();
  scheduleNextTelemetry();

  Serial.println();
  Serial.println("========================================");
  Serial.println(" VFENCE BETA - PROTOCOL v1 + GEOFENCE");
  Serial.println("========================================");
  Serial.print("Collar ID: 0x");
  Serial.println(COLLAR_ID, HEX);
  Serial.print("Fence version: ");
  Serial.println(activeFenceVersion);
  Serial.print("Vertices: ");
  Serial.println(activeFenceCount);
  Serial.println("Transporte atual: Monitor Serial (LoRa entra depois)");
  Serial.println();

  // Como nextSequence inicia em zero, DEVICE_STARTED usa sequence 0.
  sendDeviceStartedEvent();
}

void loop() {
  // GPS deve ser drenado continuamente.
  while (GPSserial.available() > 0) {
    gps.encode(GPSserial.read());
  }

  if (gps.location.isUpdated() && gps.location.isValid()) {
    processNewGpsPosition();
  }

  serviceGpsTimeout();
  serviceReliableTx();
  pollSerialPacketInjection();

  if (static_cast<int32_t>(millis() - nextTelemetryAtMs) >= 0) {
    sendTelemetryPacket();
    scheduleNextTelemetry();
  }
}
