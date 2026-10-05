#include <Arduino.h>
#include <SPI.h>
#include <LoRa.h>

#include "gateway_config.h"
#include "vfence_protocol.h"

using namespace vfence;

// ============================================================
// IDENTIDADE / ESTADO GERAL
// ============================================================

static const uint8_t GATEWAY_ID = 0x00;
static const uint8_t MAX_RECENT_RELIABLE = 16;
static const uint8_t MAX_PENDING_RELIABLE = 8;

uint16_t nextGatewaySequence = 0;
uint16_t nextPingToken = 1;
uint32_t lastOfflineSweepMs = 0;

struct CollarState {
  bool seen;
  bool online;
  uint32_t lastSeenMs;
  int lastRssi;
  float lastSnr;
  uint16_t lastTelemetrySequence;
  bool haveTelemetrySequence;
  uint16_t recentReliable[MAX_RECENT_RELIABLE];
  uint8_t recentReliableCount;
  uint8_t recentReliableNext;
};

// IDs validos de coleira: 0x01..0xFE. O indice 0 existe, mas nao e usado.
CollarState collars[255];

struct PendingReliableTx {
  bool active;
  uint8_t collarId;
  uint16_t sequence;
  uint8_t transmissions;
  uint32_t nextActionMs;
  size_t length;
  uint8_t packet[MAX_PACKET_SIZE];
};

PendingReliableTx pendingTx[MAX_PENDING_RELIABLE];

// ============================================================
// TRANSFERENCIA DE CERCA - GATEWAY -> COLEIRA
// ============================================================

enum class FenceTxPhase : uint8_t {
  IDLE,
  STAGED,
  WAIT_BEGIN_ACK,
  SEND_ALL_CHUNKS,
  RESEND_MISSING_CHUNKS,
  SEND_COMMIT,
  WAIT_COMMIT_ACK
};

struct FenceTxSession {
  bool staged;
  bool active;
  uint8_t collarId;
  uint16_t version;
  uint8_t vertexCount;
  CoordinateE7 points[MAX_VERTICES];
  uint32_t pointMask;
  uint8_t chunkCount;
  uint32_t crc32;
  FenceTxPhase phase;
  uint8_t nextChunk;
  uint16_t missingChunkMask;
  uint16_t beginSequence;
  uint16_t commitSequence;
  uint32_t nextChunkAtMs;
};

FenceTxSession fenceTx;
static const uint32_t FENCE_CHUNK_GAP_MS = 120UL;

void onFenceReliableAck(uint8_t collarId, uint16_t sequence);
void onFenceReliableNack(uint8_t collarId, uint16_t sequence,
                         ErrorCode errorCode, uint16_t detail);
void onFenceDeliveryFailed(uint8_t collarId, uint16_t sequence);

// ============================================================
// UTILITARIOS
// ============================================================

void printHexPacket(const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    if (data[i] < 0x10) Serial.print('0');
    Serial.print(data[i], HEX);
    if (i + 1 < len) Serial.print(' ');
  }
}

void printCoordinate(int32_t e7) {
  if (e7 == COORD_UNAVAILABLE) {
    Serial.print("N/A");
  } else {
    Serial.print(e7ToDegrees(e7), 7);
  }
}

void printBattery(uint8_t value) {
  if (value == U8_UNAVAILABLE) Serial.print("N/A");
  else {
    Serial.print(value);
    Serial.print('%');
  }
}

void printHdop(uint16_t value) {
  if (value == U16_UNAVAILABLE) Serial.print("N/A");
  else Serial.print(static_cast<double>(value) / 100.0, 2);
}

const char* eventName(uint8_t subtype) {
  switch (static_cast<EventSubtype>(subtype)) {
    case EventSubtype::OUTSIDE: return "OUTSIDE";
    case EventSubtype::BACK_INSIDE: return "BACK_INSIDE";
    case EventSubtype::GPS_LOST: return "GPS_LOST";
    case EventSubtype::GPS_RECOVERED: return "GPS_RECOVERED";
    case EventSubtype::LOW_BATTERY: return "LOW_BATTERY";
    case EventSubtype::CRITICAL_BATTERY: return "CRITICAL_BATTERY";
    case EventSubtype::DEVICE_STARTED: return "DEVICE_STARTED";
  }
  return "UNKNOWN_EVENT";
}

const char* errorName(ErrorCode code) {
  switch (code) {
    case ErrorCode::INVALID_LENGTH: return "INVALID_LENGTH";
    case ErrorCode::UNKNOWN_SUBTYPE: return "UNKNOWN_SUBTYPE";
    case ErrorCode::INVALID_COLLAR_ID: return "INVALID_COLLAR_ID";
    case ErrorCode::UNSUPPORTED_VERSION: return "UNSUPPORTED_VERSION";
    case ErrorCode::INVALID_PARAMETER: return "INVALID_PARAMETER";
    case ErrorCode::INVALID_FENCE_VERSION: return "INVALID_FENCE_VERSION";
    case ErrorCode::INVALID_CHUNK: return "INVALID_CHUNK";
    case ErrorCode::MISSING_CHUNKS: return "MISSING_CHUNKS";
    case ErrorCode::CRC_ERROR: return "CRC_ERROR";
    case ErrorCode::COMMIT_REJECTED: return "COMMIT_REJECTED";
    case ErrorCode::BUSY: return "BUSY";
    case ErrorCode::PERSISTENCE_ERROR: return "PERSISTENCE_ERROR";
  }
  return "UNKNOWN_ERROR";
}

uint16_t allocateSequence() {
  return nextGatewaySequence++;
}

bool validCollarId(uint8_t id) {
  return id >= 0x01 && id <= 0xFE;
}

// ============================================================
// RADIO / TRANSPORTE
// ============================================================

bool initLoRa() {
  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);
  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  if (!LoRa.begin(LORA_FREQUENCY_HZ)) {
    return false;
  }

  LoRa.setSpreadingFactor(LORA_SPREADING_FACTOR);
  LoRa.setSignalBandwidth(LORA_SIGNAL_BANDWIDTH);
  LoRa.setCodingRate4(LORA_CODING_RATE_DENOMINATOR);
  LoRa.setPreambleLength(LORA_PREAMBLE_LENGTH);
  LoRa.setSyncWord(LORA_SYNC_WORD);
  LoRa.setTxPower(LORA_TX_POWER_DBM);
  LoRa.enableCrc();
  LoRa.receive();
  return true;
}

bool transportSend(const uint8_t* data, size_t len) {
  if (!data || len == 0 || len > MAX_PACKET_SIZE) return false;

  Serial.print("[TX LoRa ");
  Serial.print(len);
  Serial.print(" bytes] ");
  printHexPacket(data, len);
  Serial.println();

  LoRa.idle();
  if (LoRa.beginPacket() == 0) {
    LoRa.receive();
    return false;
  }

  const size_t written = LoRa.write(data, len);
  const int result = LoRa.endPacket();
  LoRa.receive();

  return written == len && result == 1;
}

// ============================================================
// CONFIABILIDADE GATEWAY -> COLEIRA
// ============================================================

uint32_t scheduleRetryFromNow() {
  return millis() + ACK_TIMEOUT_MS + random(0, RETRY_JITTER_MS + 1);
}

bool sendReliable(const uint8_t* packet, size_t len) {
  PacketView view;
  if (!decodePacket(packet, len, view) || !view.header.ackReq ||
      !validCollarId(view.header.collarId)) {
    Serial.println("[PROTO] tentativa de envio confiavel invalida");
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
  tx.collarId = view.header.collarId;
  tx.sequence = view.header.sequence;
  tx.transmissions = 1;
  tx.nextActionMs = scheduleRetryFromNow();
  tx.length = len;
  memcpy(tx.packet, packet, len);

  if (!transportSend(tx.packet, tx.length)) {
    Serial.println("[RADIO] falha no envio inicial");
  }
  return true;
}

void acknowledgeReliable(uint8_t collarId, uint16_t sequence) {
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (tx.active && tx.collarId == collarId && tx.sequence == sequence) {
      tx.active = false;
      Serial.print("[ACK] collar=");
      Serial.print(collarId);
      Serial.print(" confirmou seq=");
      Serial.println(sequence);
      onFenceReliableAck(collarId, sequence);
      return;
    }
  }

  Serial.print("[ACK] sem pendencia correspondente: collar=");
  Serial.print(collarId);
  Serial.print(" seq=");
  Serial.println(sequence);
}

void rejectReliable(uint8_t collarId, uint16_t sequence,
                    ErrorCode errorCode, uint16_t detail) {
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (tx.active && tx.collarId == collarId && tx.sequence == sequence) {
      tx.active = false;
      break;
    }
  }

  Serial.print("[NACK] collar=");
  Serial.print(collarId);
  Serial.print(" seq=");
  Serial.print(sequence);
  Serial.print(" error=");
  Serial.print(errorName(errorCode));
  Serial.print(" (0x");
  Serial.print(static_cast<uint8_t>(errorCode), HEX);
  Serial.print(") detail=0x");
  Serial.println(detail, HEX);
  onFenceReliableNack(collarId, sequence, errorCode, detail);
}

void serviceReliableTx() {
  const uint32_t now = millis();

  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (!tx.active) continue;
    if (static_cast<int32_t>(now - tx.nextActionMs) < 0) continue;

    if (tx.transmissions >= MAX_TRANSMISSIONS) {
      Serial.print("[PROTO] falha definitiva: collar=");
      Serial.print(tx.collarId);
      Serial.print(" seq=");
      Serial.println(tx.sequence);
      const uint8_t failedCollar = tx.collarId;
      const uint16_t failedSequence = tx.sequence;
      tx.active = false;
      onFenceDeliveryFailed(failedCollar, failedSequence);
      continue;
    }

    tx.packet[0] |= FLAG_RETRY;
    ++tx.transmissions;

    Serial.print("[RETRY] collar=");
    Serial.print(tx.collarId);
    Serial.print(" seq=");
    Serial.print(tx.sequence);
    Serial.print(" tentativa=");
    Serial.println(tx.transmissions);

    transportSend(tx.packet, tx.length);
    tx.nextActionMs = scheduleRetryFromNow();
  }
}

// ============================================================
// RESPOSTAS CONTROL
// ============================================================

void sendAckResponse(uint8_t collarId, uint16_t ackedSequence) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeAck(packet, sizeof(packet), collarId,
                               allocateSequence(), ackedSequence, 0x00);
  if (len) transportSend(packet, len);
}

void sendNackResponse(uint8_t collarId, uint16_t rejectedSequence,
                      ErrorCode error, uint16_t detail = 0) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeNack(packet, sizeof(packet), collarId,
                                allocateSequence(), rejectedSequence,
                                error, detail);
  if (len) transportSend(packet, len);
}

void sendPong(uint8_t collarId, uint16_t token) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodePingPong(packet, sizeof(packet), collarId,
                                    allocateSequence(), ControlSubtype::PONG,
                                    token);
  if (len) transportSend(packet, len);
}

// ============================================================
// TRANSFERENCIA DE CERCA
// ============================================================

uint32_t requiredPointMask(uint8_t vertexCount) {
  if (vertexCount >= 32) return 0xFFFFFFFFUL;
  return (1UL << vertexCount) - 1UL;
}

uint16_t validChunkMask(uint8_t chunkCount) {
  if (chunkCount >= 16) return 0xFFFFu;
  return static_cast<uint16_t>((1u << chunkCount) - 1u);
}

void abortFenceTransfer(const char* reason) {
  if (fenceTx.active) {
    Serial.print("[FENCE TX] transferencia abortada: ");
    Serial.println(reason);
  }
  fenceTx.active = false;
  fenceTx.phase = fenceTx.staged ? FenceTxPhase::STAGED : FenceTxPhase::IDLE;
}

bool sendFenceBeginReliable() {
  uint8_t packet[MAX_PACKET_SIZE];
  const uint16_t seq = allocateSequence();
  const size_t len = encodeFenceBegin(packet, sizeof(packet),
                                      fenceTx.collarId, seq, false,
                                      fenceTx.version, fenceTx.vertexCount,
                                      fenceTx.chunkCount, fenceTx.crc32);
  if (!len || !sendReliable(packet, len)) return false;
  fenceTx.beginSequence = seq;
  fenceTx.phase = FenceTxPhase::WAIT_BEGIN_ACK;
  Serial.print("[FENCE TX] FENCE_BEGIN enviado. seq=");
  Serial.println(seq);
  return true;
}

bool sendFenceChunkIndex(uint8_t chunkIndex) {
  if (chunkIndex >= fenceTx.chunkCount) return false;

  const uint8_t firstVertex = static_cast<uint8_t>(chunkIndex * POINTS_PER_CHUNK);
  const uint8_t remaining = static_cast<uint8_t>(fenceTx.vertexCount - firstVertex);
  const uint8_t pointCount = remaining >= POINTS_PER_CHUNK ? POINTS_PER_CHUNK : remaining;

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeFenceChunk(packet, sizeof(packet),
                                      fenceTx.collarId, allocateSequence(), false,
                                      fenceTx.version, chunkIndex, firstVertex,
                                      &fenceTx.points[firstVertex], pointCount);
  if (!len) return false;

  Serial.print("[FENCE TX] chunk ");
  Serial.print(chunkIndex);
  Serial.print("/");
  Serial.print(fenceTx.chunkCount - 1);
  Serial.print(" pontos=");
  Serial.println(pointCount);
  return transportSend(packet, len);
}

bool sendFenceCommitReliable() {
  uint8_t packet[MAX_PACKET_SIZE];
  const uint16_t seq = allocateSequence();
  const size_t len = encodeFenceCommit(packet, sizeof(packet),
                                       fenceTx.collarId, seq, false,
                                       fenceTx.version, fenceTx.crc32);
  if (!len || !sendReliable(packet, len)) return false;
  fenceTx.commitSequence = seq;
  fenceTx.phase = FenceTxPhase::WAIT_COMMIT_ACK;
  Serial.print("[FENCE TX] FENCE_COMMIT enviado. seq=");
  Serial.println(seq);
  return true;
}

bool startFenceTransfer() {
  if (!fenceTx.staged) {
    Serial.println("[FENCE TX] nenhuma cerca preparada. Use FENCE_NEW/FENCE_POINT.");
    return false;
  }
  if (fenceTx.active) {
    Serial.println("[FENCE TX] ja existe uma transferencia em andamento.");
    return false;
  }
  if ((fenceTx.pointMask & requiredPointMask(fenceTx.vertexCount)) !=
      requiredPointMask(fenceTx.vertexCount)) {
    Serial.println("[FENCE TX] faltam pontos na cerca preparada.");
    return false;
  }

  fenceTx.chunkCount = static_cast<uint8_t>(
      (fenceTx.vertexCount + POINTS_PER_CHUNK - 1) / POINTS_PER_CHUNK);
  fenceTx.crc32 = computeFenceCrc32(fenceTx.version,
                                    fenceTx.points,
                                    fenceTx.vertexCount);
  fenceTx.active = true;
  fenceTx.nextChunk = 0;
  fenceTx.missingChunkMask = 0;

  Serial.print("[FENCE TX] iniciando transferencia para collar=");
  Serial.print(fenceTx.collarId);
  Serial.print(" version=");
  Serial.print(fenceTx.version);
  Serial.print(" vertices=");
  Serial.print(fenceTx.vertexCount);
  Serial.print(" chunks=");
  Serial.print(fenceTx.chunkCount);
  Serial.print(" CRC32=0x");
  Serial.println(fenceTx.crc32, HEX);

  if (!sendFenceBeginReliable()) {
    abortFenceTransfer("falha ao enviar FENCE_BEGIN");
    return false;
  }
  return true;
}

void onFenceReliableAck(uint8_t collarId, uint16_t sequence) {
  if (!fenceTx.active || fenceTx.collarId != collarId) return;

  if (fenceTx.phase == FenceTxPhase::WAIT_BEGIN_ACK &&
      sequence == fenceTx.beginSequence) {
    Serial.println("[FENCE TX] FENCE_BEGIN confirmado; enviando chunks.");
    fenceTx.phase = FenceTxPhase::SEND_ALL_CHUNKS;
    fenceTx.nextChunk = 0;
    fenceTx.nextChunkAtMs = millis();
    return;
  }

  if (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK &&
      sequence == fenceTx.commitSequence) {
    Serial.println("[FENCE TX] SUCESSO: nova cerca confirmada pela coleira.");
    fenceTx.active = false;
    fenceTx.phase = FenceTxPhase::STAGED;
    return;
  }
}

void onFenceReliableNack(uint8_t collarId, uint16_t sequence,
                         ErrorCode errorCode, uint16_t detail) {
  if (!fenceTx.active || fenceTx.collarId != collarId) return;

  if (fenceTx.phase == FenceTxPhase::WAIT_BEGIN_ACK &&
      sequence == fenceTx.beginSequence) {
    abortFenceTransfer("FENCE_BEGIN rejeitado");
    return;
  }

  if (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK &&
      sequence == fenceTx.commitSequence) {
    if (errorCode == ErrorCode::MISSING_CHUNKS) {
      const uint16_t missing = detail & validChunkMask(fenceTx.chunkCount);
      if (missing == 0) {
        abortFenceTransfer("NACK MISSING_CHUNKS sem bitmap valido");
        return;
      }
      fenceTx.missingChunkMask = missing;
      fenceTx.phase = FenceTxPhase::RESEND_MISSING_CHUNKS;
      fenceTx.nextChunkAtMs = millis();
      Serial.print("[FENCE TX] reenviando chunks ausentes. bitmap=0x");
      Serial.println(missing, HEX);
      return;
    }

    if (errorCode == ErrorCode::CRC_ERROR) {
      abortFenceTransfer("CRC_ERROR; reinicie a transferencia com FENCE_SEND");
      return;
    }

    abortFenceTransfer("FENCE_COMMIT rejeitado");
  }
}

void onFenceDeliveryFailed(uint8_t collarId, uint16_t sequence) {
  if (!fenceTx.active || fenceTx.collarId != collarId) return;
  if ((fenceTx.phase == FenceTxPhase::WAIT_BEGIN_ACK && sequence == fenceTx.beginSequence) ||
      (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK && sequence == fenceTx.commitSequence)) {
    abortFenceTransfer("ACK nao recebido apos 4 transmissoes");
  }
}

void serviceFenceTransfer() {
  if (!fenceTx.active) return;
  const uint32_t now = millis();

  if (fenceTx.phase == FenceTxPhase::SEND_ALL_CHUNKS) {
    if (static_cast<int32_t>(now - fenceTx.nextChunkAtMs) < 0) return;

    if (fenceTx.nextChunk < fenceTx.chunkCount) {
      if (!sendFenceChunkIndex(fenceTx.nextChunk)) {
        abortFenceTransfer("falha ao enviar chunk");
        return;
      }
      ++fenceTx.nextChunk;
      fenceTx.nextChunkAtMs = millis() + FENCE_CHUNK_GAP_MS;
      return;
    }

    fenceTx.phase = FenceTxPhase::SEND_COMMIT;
    fenceTx.nextChunkAtMs = millis() + FENCE_CHUNK_GAP_MS;
    return;
  }

  if (fenceTx.phase == FenceTxPhase::RESEND_MISSING_CHUNKS) {
    if (static_cast<int32_t>(now - fenceTx.nextChunkAtMs) < 0) return;

    if (fenceTx.missingChunkMask != 0) {
      uint8_t idx = 0;
      while (idx < fenceTx.chunkCount &&
             (fenceTx.missingChunkMask & (1u << idx)) == 0) {
        ++idx;
      }
      if (idx >= fenceTx.chunkCount) {
        fenceTx.missingChunkMask = 0;
      } else {
        if (!sendFenceChunkIndex(idx)) {
          abortFenceTransfer("falha ao reenviar chunk");
          return;
        }
        fenceTx.missingChunkMask &= static_cast<uint16_t>(~(1u << idx));
        fenceTx.nextChunkAtMs = millis() + FENCE_CHUNK_GAP_MS;
        return;
      }
    }

    fenceTx.phase = FenceTxPhase::SEND_COMMIT;
    fenceTx.nextChunkAtMs = millis() + FENCE_CHUNK_GAP_MS;
    return;
  }

  if (fenceTx.phase == FenceTxPhase::SEND_COMMIT) {
    if (static_cast<int32_t>(now - fenceTx.nextChunkAtMs) < 0) return;
    if (!sendFenceCommitReliable()) {
      abortFenceTransfer("falha ao enviar FENCE_COMMIT");
    }
  }
}

// ============================================================
// CONTROLE DE COLEIRAS / DUPLICATAS
// ============================================================

void clearReliableHistory(uint8_t collarId) {
  CollarState& c = collars[collarId];
  c.recentReliableCount = 0;
  c.recentReliableNext = 0;
  memset(c.recentReliable, 0, sizeof(c.recentReliable));
}

bool isDuplicateReliable(uint8_t collarId, uint16_t sequence) {
  const CollarState& c = collars[collarId];
  for (uint8_t i = 0; i < c.recentReliableCount; ++i) {
    if (c.recentReliable[i] == sequence) return true;
  }
  return false;
}

void rememberReliable(uint8_t collarId, uint16_t sequence) {
  CollarState& c = collars[collarId];
  if (c.recentReliableCount < MAX_RECENT_RELIABLE) {
    c.recentReliable[c.recentReliableCount++] = sequence;
    return;
  }

  c.recentReliable[c.recentReliableNext] = sequence;
  c.recentReliableNext = (c.recentReliableNext + 1) % MAX_RECENT_RELIABLE;
}

void markCollarSeen(uint8_t collarId, int rssi, float snr, bool hasRadioMetrics) {
  CollarState& c = collars[collarId];
  const bool wasOffline = c.seen && !c.online;
  const bool firstSeen = !c.seen;

  c.seen = true;
  c.online = true;
  c.lastSeenMs = millis();
  if (hasRadioMetrics) {
    c.lastRssi = rssi;
    c.lastSnr = snr;
  }

  if (firstSeen || wasOffline) {
    Serial.print("[STATE] collar ");
    Serial.print(collarId);
    Serial.println(" ONLINE");
  }
}

void serviceOfflineDetection() {
  const uint32_t now = millis();
  if (now - lastOfflineSweepMs < 1000UL) return;
  lastOfflineSweepMs = now;

  for (uint16_t id = 1; id <= 0xFE; ++id) {
    CollarState& c = collars[id];
    if (!c.seen || !c.online) continue;

    if (now - c.lastSeenMs >= COLLAR_OFFLINE_TIMEOUT_MS) {
      c.online = false;
      Serial.print("[STATE] collar ");
      Serial.print(id);
      Serial.println(" OFFLINE (180 s sem pacotes)");

      Serial.print("[APP] {\"type\":\"offline\",\"collarId\":");
      Serial.print(id);
      Serial.println("}");
    }
  }
}

// ============================================================
// SAIDA PARA A APLICACAO (JSON EM UMA LINHA)
// Nesta fase e apenas uma ponte via Serial. Futuramente pode ser
// substituida por Wi-Fi/MQTT/HTTP sem alterar o protocolo LoRa.
// ============================================================

void appPrintCoordinate(const char* key, int32_t value, bool comma = true) {
  Serial.print('"');
  Serial.print(key);
  Serial.print("\":");
  if (value == COORD_UNAVAILABLE) Serial.print("null");
  else Serial.print(e7ToDegrees(value), 7);
  if (comma) Serial.print(',');
}

void appPrintBattery(uint8_t value, bool comma = true) {
  Serial.print("\"battery\":");
  if (value == U8_UNAVAILABLE) Serial.print("null");
  else Serial.print(value);
  if (comma) Serial.print(',');
}

void appPrintHdop(uint16_t value, bool comma = true) {
  Serial.print("\"hdop\":");
  if (value == U16_UNAVAILABLE) Serial.print("null");
  else Serial.print(static_cast<double>(value) / 100.0, 2);
  if (comma) Serial.print(',');
}

// ============================================================
// DECODIFICACAO TELEMETRY
// ============================================================

void processTelemetry(const PacketView& packet, int rssi, float snr,
                      bool hasRadioMetrics) {
  if (packet.payloadLength < 1) return;

  if (packet.payload[0] != static_cast<uint8_t>(TelemetrySubtype::POSITION_STATUS)) {
    Serial.print("[TELEMETRY] subtype desconhecido 0x");
    Serial.println(packet.payload[0], HEX);
    return;
  }

  TelemetryPayload d;
  if (!decodeTelemetry(packet, d)) {
    Serial.println("[TELEMETRY] payload POSITION_STATUS invalido");
    return;
  }

  CollarState& c = collars[packet.header.collarId];
  // A sequence e global por coleira e tambem e consumida por EVENT/CONTROL.
  // Portanto, saltos entre duas TELEMETRY nao significam necessariamente perda.
  c.lastTelemetrySequence = packet.header.sequence;
  c.haveTelemetrySequence = true;

  Serial.println("--- TELEMETRY POSITION_STATUS ---");
  Serial.print("Collar: "); Serial.println(packet.header.collarId);
  Serial.print("Sequence: "); Serial.println(packet.header.sequence);
  Serial.print("Latitude: "); printCoordinate(d.position.lat); Serial.println();
  Serial.print("Longitude: "); printCoordinate(d.position.lon); Serial.println();
  Serial.print("Distancia da borda: ");
  if (d.distanceBorderM == U16_UNAVAILABLE) Serial.println("N/A");
  else { Serial.print(d.distanceBorderM); Serial.println(" m"); }
  Serial.print("Bateria: "); printBattery(d.batteryPct); Serial.println();
  Serial.print("Satelites: ");
  if (d.satellites == U8_UNAVAILABLE) Serial.println("N/A");
  else Serial.println(d.satellites);
  Serial.print("HDOP: "); printHdop(d.hdopX100); Serial.println();
  Serial.print("Fence version: "); Serial.println(d.fenceVersion);
  Serial.print("State flags: 0x"); Serial.println(d.stateFlags, HEX);
  Serial.print("  INSIDE: "); Serial.println((d.stateFlags & STATE_INSIDE) ? "SIM" : "NAO");
  Serial.print("  NEAR: "); Serial.println((d.stateFlags & STATE_NEAR) ? "SIM" : "NAO");
  Serial.print("  VERY_NEAR: "); Serial.println((d.stateFlags & STATE_VERY_NEAR) ? "SIM" : "NAO");
  Serial.print("  GPS_VALID: "); Serial.println((d.stateFlags & STATE_GPS_VALID) ? "SIM" : "NAO");
  Serial.print("  BUZZER: "); Serial.println((d.stateFlags & STATE_BUZZER_ACTIVE) ? "ON" : "OFF");
  Serial.print("  SECOND_ACTUATOR: "); Serial.println((d.stateFlags & STATE_SECOND_ACTUATOR_ACTIVE) ? "ON" : "OFF");
  if (hasRadioMetrics) {
    Serial.print("RSSI: "); Serial.print(rssi); Serial.println(" dBm");
    Serial.print("SNR: "); Serial.print(snr, 2); Serial.println(" dB");
  }

  Serial.print("[APP] {\"type\":\"telemetry\",\"collarId\":");
  Serial.print(packet.header.collarId);
  Serial.print(",\"sequence\":"); Serial.print(packet.header.sequence); Serial.print(',');
  appPrintCoordinate("latitude", d.position.lat);
  appPrintCoordinate("longitude", d.position.lon);
  Serial.print("\"distanceBorderM\":");
  if (d.distanceBorderM == U16_UNAVAILABLE) Serial.print("null");
  else Serial.print(d.distanceBorderM);
  Serial.print(',');
  appPrintBattery(d.batteryPct);
  Serial.print("\"satellites\":");
  if (d.satellites == U8_UNAVAILABLE) Serial.print("null");
  else Serial.print(d.satellites);
  Serial.print(',');
  appPrintHdop(d.hdopX100);
  Serial.print("\"inside\":"); Serial.print((d.stateFlags & STATE_INSIDE) ? "true" : "false");
  Serial.print(",\"near\":"); Serial.print((d.stateFlags & STATE_NEAR) ? "true" : "false");
  Serial.print(",\"veryNear\":"); Serial.print((d.stateFlags & STATE_VERY_NEAR) ? "true" : "false");
  Serial.print(",\"gpsValid\":"); Serial.print((d.stateFlags & STATE_GPS_VALID) ? "true" : "false");
  Serial.print(",\"fenceVersion\":"); Serial.print(d.fenceVersion);
  if (hasRadioMetrics) {
    Serial.print(",\"rssi\":"); Serial.print(rssi);
    Serial.print(",\"snr\":"); Serial.print(snr, 2);
  }
  Serial.println("}");
}

// ============================================================
// DECODIFICACAO EVENT
// ============================================================

bool processEventPayload(const PacketView& packet) {
  const uint8_t subtype = packet.payload[0];

  Serial.print("--- EVENT ");
  Serial.print(eventName(subtype));
  Serial.println(" ---");
  Serial.print("Collar: "); Serial.println(packet.header.collarId);
  Serial.print("Sequence: "); Serial.println(packet.header.sequence);

  if (subtype == static_cast<uint8_t>(EventSubtype::OUTSIDE) ||
      subtype == static_cast<uint8_t>(EventSubtype::BACK_INSIDE)) {
    PositionEventPayload d;
    if (!decodePositionEvent(packet, d)) return false;

    Serial.print("Fence version: "); Serial.println(d.fenceVersion);
    Serial.print("Latitude: "); printCoordinate(d.position.lat); Serial.println();
    Serial.print("Longitude: "); printCoordinate(d.position.lon); Serial.println();
    Serial.print("Distancia da borda: "); Serial.print(d.distanceBorderM); Serial.println(" m");
    Serial.print("Bateria: "); printBattery(d.batteryPct); Serial.println();
    Serial.print("Satelites: "); Serial.println(d.satellites);
    Serial.print("HDOP: "); printHdop(d.hdopX100); Serial.println();

    Serial.print("[APP] {\"type\":\"event\",\"event\":\"");
    Serial.print(eventName(subtype));
    Serial.print("\",\"collarId\":"); Serial.print(packet.header.collarId);
    Serial.print(",\"sequence\":"); Serial.print(packet.header.sequence); Serial.print(',');
    appPrintCoordinate("latitude", d.position.lat);
    appPrintCoordinate("longitude", d.position.lon);
    Serial.print("\"distanceBorderM\":"); Serial.print(d.distanceBorderM);
    Serial.print(",\"fenceVersion\":"); Serial.print(d.fenceVersion);
    Serial.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::GPS_LOST)) {
    GpsLostPayload d;
    if (!decodeGpsLost(packet, d)) return false;
    Serial.print("Ultima latitude: "); printCoordinate(d.lastPosition.lat); Serial.println();
    Serial.print("Ultima longitude: "); printCoordinate(d.lastPosition.lon); Serial.println();
    Serial.print("Idade da posicao: ");
    if (d.positionAgeS == U16_UNAVAILABLE) Serial.println("N/A");
    else { Serial.print(d.positionAgeS); Serial.println(" s"); }

    Serial.print("[APP] {\"type\":\"event\",\"event\":\"GPS_LOST\",\"collarId\":");
    Serial.print(packet.header.collarId); Serial.print(',');
    appPrintCoordinate("lastLatitude", d.lastPosition.lat);
    appPrintCoordinate("lastLongitude", d.lastPosition.lon);
    Serial.print("\"positionAgeS\":");
    if (d.positionAgeS == U16_UNAVAILABLE) Serial.print("null");
    else Serial.print(d.positionAgeS);
    Serial.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::GPS_RECOVERED)) {
    GpsRecoveredPayload d;
    if (!decodeGpsRecovered(packet, d)) return false;
    Serial.print("Latitude: "); printCoordinate(d.position.lat); Serial.println();
    Serial.print("Longitude: "); printCoordinate(d.position.lon); Serial.println();
    Serial.print("Satelites: "); Serial.println(d.satellites);
    Serial.print("HDOP: "); printHdop(d.hdopX100); Serial.println();

    Serial.print("[APP] {\"type\":\"event\",\"event\":\"GPS_RECOVERED\",\"collarId\":");
    Serial.print(packet.header.collarId); Serial.print(',');
    appPrintCoordinate("latitude", d.position.lat);
    appPrintCoordinate("longitude", d.position.lon);
    Serial.println("\"recovered\":true}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::LOW_BATTERY) ||
      subtype == static_cast<uint8_t>(EventSubtype::CRITICAL_BATTERY)) {
    BatteryEventPayload d;
    if (!decodeBatteryEvent(packet, d)) return false;
    Serial.print("Bateria: "); printBattery(d.batteryPct); Serial.println();
    Serial.print("Ultima latitude: "); printCoordinate(d.lastPosition.lat); Serial.println();
    Serial.print("Ultima longitude: "); printCoordinate(d.lastPosition.lon); Serial.println();
    Serial.print("Idade da posicao: "); Serial.print(d.positionAgeS); Serial.println(" s");

    Serial.print("[APP] {\"type\":\"event\",\"event\":\"");
    Serial.print(eventName(subtype));
    Serial.print("\",\"collarId\":"); Serial.print(packet.header.collarId); Serial.print(',');
    appPrintBattery(d.batteryPct);
    appPrintCoordinate("lastLatitude", d.lastPosition.lat);
    appPrintCoordinate("lastLongitude", d.lastPosition.lon);
    Serial.print("\"positionAgeS\":"); Serial.print(d.positionAgeS);
    Serial.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::DEVICE_STARTED)) {
    DeviceStartedPayload d;
    if (!decodeDeviceStarted(packet, d)) return false;
    Serial.print("Reset reason: 0x"); Serial.println(static_cast<uint8_t>(d.resetReason), HEX);
    Serial.print("Active fence version: "); Serial.println(d.activeFenceVersion);
    Serial.print("Bateria: "); printBattery(d.batteryPct); Serial.println();

    Serial.print("[APP] {\"type\":\"event\",\"event\":\"DEVICE_STARTED\",\"collarId\":");
    Serial.print(packet.header.collarId);
    Serial.print(",\"resetReason\":"); Serial.print(static_cast<uint8_t>(d.resetReason));
    Serial.print(",\"activeFenceVersion\":"); Serial.print(d.activeFenceVersion); Serial.print(',');
    appPrintBattery(d.batteryPct, false);
    Serial.println("}");
    return true;
  }

  return false;
}

void processEvent(const PacketView& packet) {
  if (packet.payloadLength < 1) {
    if (packet.header.ackReq) {
      sendNackResponse(packet.header.collarId, packet.header.sequence,
                       ErrorCode::INVALID_LENGTH);
    }
    return;
  }

  const uint8_t subtype = packet.payload[0];
  const bool isDeviceStarted =
      subtype == static_cast<uint8_t>(EventSubtype::DEVICE_STARTED);

  // Um DEVICE_STARTED original (nao RETRY) define uma nova sessao da coleira.
  // Um retry de DEVICE_STARTED nao deve limpar o historico novamente.
  if (isDeviceStarted && !packet.header.retry) {
    clearReliableHistory(packet.header.collarId);
  }

  if (packet.header.ackReq &&
      isDuplicateReliable(packet.header.collarId, packet.header.sequence)) {
    Serial.print("[DUPLICATE] EVENT collar=");
    Serial.print(packet.header.collarId);
    Serial.print(" seq=");
    Serial.print(packet.header.sequence);
    Serial.println(" - nao processado novamente; ACK reenviado");
    sendAckResponse(packet.header.collarId, packet.header.sequence);
    return;
  }

  if (!processEventPayload(packet)) {
    if (packet.header.ackReq) {
      // Se o subtype existe mas o tamanho nao bate, e INVALID_LENGTH.
      const bool known = subtype >= static_cast<uint8_t>(EventSubtype::OUTSIDE) &&
                         subtype <= static_cast<uint8_t>(EventSubtype::DEVICE_STARTED);
      sendNackResponse(packet.header.collarId, packet.header.sequence,
                       known ? ErrorCode::INVALID_LENGTH : ErrorCode::UNKNOWN_SUBTYPE);
    }
    return;
  }

  if (packet.header.ackReq) {
    rememberReliable(packet.header.collarId, packet.header.sequence);
    sendAckResponse(packet.header.collarId, packet.header.sequence);
  }
}

// ============================================================
// CONTROL RECEBIDO
// ============================================================

void processControl(const PacketView& packet) {
  if (packet.payloadLength < 1) return;
  const uint8_t subtype = packet.payload[0];

  if (subtype == static_cast<uint8_t>(ControlSubtype::ACK)) {
    AckPayload ack;
    if (decodeAck(packet, ack)) {
      acknowledgeReliable(packet.header.collarId, ack.ackedSequence);
    }
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::NACK)) {
    NackPayload nack;
    if (decodeNack(packet, nack)) {
      rejectReliable(packet.header.collarId, nack.rejectedSequence,
                     nack.errorCode, nack.detail);
    }
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::PING)) {
    uint16_t token;
    if (decodePingPong(packet, token)) {
      Serial.print("[PING] collar="); Serial.print(packet.header.collarId);
      Serial.print(" token="); Serial.println(token);
      sendPong(packet.header.collarId, token);
    }
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::PONG)) {
    uint16_t token;
    if (decodePingPong(packet, token)) {
      Serial.print("[PONG] collar="); Serial.print(packet.header.collarId);
      Serial.print(" token="); Serial.println(token);
    }
    return;
  }

  Serial.print("[CONTROL] subtype desconhecido 0x");
  Serial.println(subtype, HEX);
}

// ============================================================
// PROCESSAMENTO CENTRAL DE RX
// ============================================================

void processIncomingPacket(const uint8_t* data, size_t len,
                           int rssi = 0, float snr = 0.0f,
                           bool hasRadioMetrics = false) {
  PacketView packet;
  if (!decodePacket(data, len, packet)) {
    Serial.println("[RX] pacote invalido: header/tamanho");
    return;
  }

  const uint8_t collarId = packet.header.collarId;
  if (!validCollarId(collarId)) {
    Serial.print("[RX] collarId invalido para uplink: 0x");
    Serial.println(collarId, HEX);
    return;
  }

  markCollarSeen(collarId, rssi, snr, hasRadioMetrics);

  if (packet.header.version != PROTOCOL_VERSION) {
    Serial.print("[RX] versao nao suportada: ");
    Serial.println(packet.header.version);
    if (packet.header.ackReq) {
      sendNackResponse(collarId, packet.header.sequence,
                       ErrorCode::UNSUPPORTED_VERSION);
    }
    return;
  }

  switch (packet.header.type) {
    case MessageType::TELEMETRY:
      processTelemetry(packet, rssi, snr, hasRadioMetrics);
      break;

    case MessageType::EVENT:
      processEvent(packet);
      break;

    case MessageType::CONTROL:
      processControl(packet);
      break;

    case MessageType::CONFIG:
      Serial.println("[RX] CONFIG vindo da coleira nao e esperado na v1");
      if (packet.header.ackReq) {
        sendNackResponse(collarId, packet.header.sequence,
                         ErrorCode::UNKNOWN_SUBTYPE);
      }
      break;
  }
}

void pollLoRaReceive() {
  const int packetSize = LoRa.parsePacket();
  if (packetSize <= 0) return;

  uint8_t data[MAX_PACKET_SIZE];
  size_t count = 0;
  bool oversized = packetSize > static_cast<int>(MAX_PACKET_SIZE);

  while (LoRa.available()) {
    const int b = LoRa.read();
    if (count < MAX_PACKET_SIZE && b >= 0) {
      data[count++] = static_cast<uint8_t>(b);
    }
  }

  const int rssi = LoRa.packetRssi();
  const float snr = LoRa.packetSnr();

  if (oversized) {
    Serial.print("[RX LoRa] pacote maior que MAX_PACKET_SIZE: ");
    Serial.println(packetSize);
    return;
  }

  Serial.println();
  Serial.print("[RX LoRa ");
  Serial.print(count);
  Serial.print(" bytes | RSSI=");
  Serial.print(rssi);
  Serial.print(" dBm | SNR=");
  Serial.print(snr, 2);
  Serial.println(" dB]");
  printHexPacket(data, count);
  Serial.println();

  processIncomingPacket(data, count, rssi, snr, true);
}

// ============================================================
// COMANDOS SERIAL PARA TESTE DO GATEWAY
// ============================================================

void printHelp() {
  Serial.println("Comandos:");
  Serial.println("  HELP");
  Serial.println("  STATUS");
  Serial.println("  RADIO");
  Serial.println("  PING <collarId> [token]");
  Serial.println("  PARAM <id> <muitoPertoM> <pertoM> <telemetryS> <gpsTimeoutS> <lowBat> <critBat>");
  Serial.println("  FENCE_NEW <id> <version> <vertexCount>");
  Serial.println("  FENCE_POINT <index> <latitude> <longitude>");
  Serial.println("  FENCE_SHOW");
  Serial.println("  FENCE_SEND");
  Serial.println("  FENCE_CANCEL");
  Serial.println("  RX <bytes hex...>   (injeta pacote sem radio para teste)");
}

void printRadioConfig() {
  Serial.println("--- RADIO CONFIG ---");
  Serial.print("Frequency: "); Serial.print(LORA_FREQUENCY_HZ); Serial.println(" Hz");
  Serial.print("SF: "); Serial.println(LORA_SPREADING_FACTOR);
  Serial.print("BW: "); Serial.print(LORA_SIGNAL_BANDWIDTH); Serial.println(" Hz");
  Serial.print("CR: 4/"); Serial.println(LORA_CODING_RATE_DENOMINATOR);
  Serial.print("Preamble: "); Serial.println(LORA_PREAMBLE_LENGTH);
  Serial.print("SyncWord: 0x"); Serial.println(LORA_SYNC_WORD, HEX);
  Serial.print("TX power: "); Serial.print(LORA_TX_POWER_DBM); Serial.println(" dBm");
  Serial.println("CRC: ON");
}

void printStatus() {
  Serial.println("--- COLLAR STATUS ---");
  bool any = false;
  const uint32_t now = millis();
  for (uint16_t id = 1; id <= 0xFE; ++id) {
    const CollarState& c = collars[id];
    if (!c.seen) continue;
    any = true;
    Serial.print("Collar "); Serial.print(id);
    Serial.print(" | "); Serial.print(c.online ? "ONLINE" : "OFFLINE");
    Serial.print(" | lastSeen="); Serial.print((now - c.lastSeenMs) / 1000UL); Serial.print(" s");
    Serial.print(" | RSSI="); Serial.print(c.lastRssi);
    Serial.print(" | SNR="); Serial.println(c.lastSnr, 2);
  }
  if (!any) Serial.println("Nenhuma coleira vista ainda.");
}

void sendPingCommand(uint8_t collarId, uint16_t token) {
  if (!validCollarId(collarId)) {
    Serial.println("[CMD] collarId invalido");
    return;
  }

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodePingPong(packet, sizeof(packet), collarId,
                                    allocateSequence(), ControlSubtype::PING,
                                    token);
  if (len) transportSend(packet, len);
}

void sendParametersCommand(uint8_t collarId, const ParametersPayload& p) {
  if (!validCollarId(collarId)) {
    Serial.println("[CMD] collarId invalido");
    return;
  }

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeParameters(packet, sizeof(packet), collarId,
                                      allocateSequence(), false, p);
  if (len) sendReliable(packet, len);
}

void stageFenceNew(uint8_t collarId, uint16_t version, uint8_t vertexCount) {
  if (!validCollarId(collarId)) {
    Serial.println("[FENCE] collarId invalido");
    return;
  }
  if (vertexCount < 3 || vertexCount > MAX_VERTICES) {
    Serial.println("[FENCE] vertexCount deve estar entre 3 e 32");
    return;
  }
  if (fenceTx.active) {
    Serial.println("[FENCE] cancele ou aguarde a transferencia atual");
    return;
  }

  memset(&fenceTx, 0, sizeof(fenceTx));
  fenceTx.staged = true;
  fenceTx.collarId = collarId;
  fenceTx.version = version;
  fenceTx.vertexCount = vertexCount;
  fenceTx.phase = FenceTxPhase::STAGED;

  Serial.print("[FENCE] nova cerca preparada: collar="); Serial.print(collarId);
  Serial.print(" version="); Serial.print(version);
  Serial.print(" vertices="); Serial.println(vertexCount);
}

void stageFencePoint(uint8_t index, double lat, double lon) {
  if (!fenceTx.staged) {
    Serial.println("[FENCE] use FENCE_NEW primeiro");
    return;
  }
  if (fenceTx.active) {
    Serial.println("[FENCE] nao altere pontos durante uma transferencia");
    return;
  }
  if (index >= fenceTx.vertexCount) {
    Serial.println("[FENCE] indice fora da faixa");
    return;
  }
  if (lat < -90.0 || lat > 90.0 || lon < -180.0 || lon > 180.0) {
    Serial.println("[FENCE] latitude/longitude invalidas");
    return;
  }

  fenceTx.points[index].lat = degreesToE7(lat);
  fenceTx.points[index].lon = degreesToE7(lon);
  fenceTx.pointMask |= (1UL << index);

  Serial.print("[FENCE] ponto "); Serial.print(index); Serial.print(" = ");
  Serial.print(lat, 7); Serial.print(", "); Serial.println(lon, 7);
}

void showStagedFence() {
  if (!fenceTx.staged) {
    Serial.println("[FENCE] nenhuma cerca preparada");
    return;
  }

  Serial.println("--- STAGED FENCE ---");
  Serial.print("Collar: "); Serial.println(fenceTx.collarId);
  Serial.print("Version: "); Serial.println(fenceTx.version);
  Serial.print("Vertex count: "); Serial.println(fenceTx.vertexCount);
  Serial.print("Transfer active: "); Serial.println(fenceTx.active ? "SIM" : "NAO");
  for (uint8_t i = 0; i < fenceTx.vertexCount; ++i) {
    Serial.print(i); Serial.print(": ");
    if ((fenceTx.pointMask & (1UL << i)) == 0) {
      Serial.println("<NAO DEFINIDO>");
    } else {
      Serial.print(e7ToDegrees(fenceTx.points[i].lat), 7);
      Serial.print(", ");
      Serial.println(e7ToDegrees(fenceTx.points[i].lon), 7);
    }
  }

  if ((fenceTx.pointMask & requiredPointMask(fenceTx.vertexCount)) ==
      requiredPointMask(fenceTx.vertexCount)) {
    const uint32_t crc = computeFenceCrc32(fenceTx.version,
                                           fenceTx.points,
                                           fenceTx.vertexCount);
    Serial.print("CRC32: 0x"); Serial.println(crc, HEX);
  } else {
    Serial.println("CRC32: indisponivel (faltam pontos)");
  }
}

bool parseHexInjection(const String& line) {
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
      Serial.println("[RX SERIAL] token hexadecimal invalido");
      return false;
    }

    data[count++] = static_cast<uint8_t>(value);
    start = end + 1;
  }

  Serial.print("[RX SERIAL "); Serial.print(count); Serial.print(" bytes] ");
  printHexPacket(data, count); Serial.println();
  processIncomingPacket(data, count, 0, 0.0f, false);
  return true;
}

void pollSerialCommands() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  if (line.equalsIgnoreCase("HELP")) {
    printHelp();
    return;
  }
  if (line.equalsIgnoreCase("STATUS")) {
    printStatus();
    return;
  }
  if (line.equalsIgnoreCase("RADIO")) {
    printRadioConfig();
    return;
  }
  if (line.startsWith("RX ")) {
    parseHexInjection(line);
    return;
  }

  unsigned int id = 0;
  unsigned int token = 0;
  if (sscanf(line.c_str(), "PING %u %u", &id, &token) == 2) {
    sendPingCommand(static_cast<uint8_t>(id), static_cast<uint16_t>(token));
    return;
  }
  if (sscanf(line.c_str(), "PING %u", &id) == 1) {
    sendPingCommand(static_cast<uint8_t>(id), nextPingToken++);
    return;
  }

  unsigned int muitoPerto = 0, perto = 0, gpsTimeout = 0, lowBat = 0, critBat = 0;
  unsigned long telemetry = 0;
  if (sscanf(line.c_str(), "PARAM %u %u %u %lu %u %u %u",
             &id, &muitoPerto, &perto, &telemetry,
             &gpsTimeout, &lowBat, &critBat) == 7) {
    ParametersPayload p;
    p.muitoPertoM = static_cast<uint16_t>(muitoPerto);
    p.pertoM = static_cast<uint16_t>(perto);
    p.telemetryIntervalS = static_cast<uint32_t>(telemetry);
    p.gpsTimeoutS = static_cast<uint16_t>(gpsTimeout);
    p.lowBatteryPct = static_cast<uint8_t>(lowBat);
    p.criticalBatteryPct = static_cast<uint8_t>(critBat);
    sendParametersCommand(static_cast<uint8_t>(id), p);
    return;
  }

  unsigned int version = 0, vertexCount = 0;
  if (sscanf(line.c_str(), "FENCE_NEW %u %u %u", &id, &version, &vertexCount) == 3) {
    stageFenceNew(static_cast<uint8_t>(id), static_cast<uint16_t>(version),
                  static_cast<uint8_t>(vertexCount));
    return;
  }

  unsigned int pointIndex = 0;
  double lat = 0.0, lon = 0.0;
  if (sscanf(line.c_str(), "FENCE_POINT %u %lf %lf", &pointIndex, &lat, &lon) == 3) {
    stageFencePoint(static_cast<uint8_t>(pointIndex), lat, lon);
    return;
  }

  if (line.equalsIgnoreCase("FENCE_SHOW")) {
    showStagedFence();
    return;
  }
  if (line.equalsIgnoreCase("FENCE_SEND")) {
    startFenceTransfer();
    return;
  }
  if (line.equalsIgnoreCase("FENCE_CANCEL")) {
    if (fenceTx.active) abortFenceTransfer("cancelada pelo usuario");
    else Serial.println("[FENCE] nenhuma transferencia ativa");
    return;
  }

  Serial.println("[CMD] comando desconhecido. Digite HELP.");
}

// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  Serial.begin(115200);
  delay(1500);

  memset(collars, 0, sizeof(collars));
  memset(pendingTx, 0, sizeof(pendingTx));
  memset(&fenceTx, 0, sizeof(fenceTx));
  fenceTx.phase = FenceTxPhase::IDLE;
  randomSeed(static_cast<uint32_t>(micros()));

  Serial.println();
  Serial.println("========================================");
  Serial.println("       VFENCE GATEWAY - PROTOCOL v1");
  Serial.println("========================================");

  if (!initLoRa()) {
    Serial.println("[FATAL] Falha ao iniciar LoRa.");
    Serial.println("Verifique placa, pinos, antena e frequencia.");
    while (true) delay(1000);
  }

  Serial.println("[RADIO] LoRa inicializado com sucesso.");
  printRadioConfig();
  Serial.println();
  printHelp();
  Serial.println();
}

void loop() {
  pollLoRaReceive();
  serviceReliableTx();
  serviceFenceTransfer();
  serviceOfflineDetection();
  pollSerialCommands();
}
