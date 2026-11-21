#include <Arduino.h>
#include <ctype.h>
#include <math.h>
#include <stdlib.h>
#include <SPI.h>
#include <LoRa.h>
#include <driver/uart.h>
#include <esp_random.h>

#include "gateway_config.h"
#include "gateway_serial_output.h"
#include "metric_serial_export.h"
#include "pdr_serial.h"
#include "profile_plan.h"
#include "serial_command_input.h"
#include "terminal_serial.h"
#include "vfence_protocol.h"

using namespace vfence;

// ============================================================
// IDENTIDADE / ESTADO GERAL
// ============================================================

static const uint8_t GATEWAY_ID = 0x00;
static const uint8_t MAX_RECENT_RELIABLE = 16;
static const uint8_t MAX_PENDING_RELIABLE = 8;
static const char* SERIAL_SCHEMA = "vfence.gateway.serial.v1";
static const uint32_t RADIO_WINDOW_MS = 60000UL;
static const uint8_t MAX_RECENT_RADIO_SEQUENCES = 16;
static const bool GATEWAY_TERMINAL_DETAIL = false;
static const uint8_t RADIO_SUMMARY_CAPACITY = 2;
static TerminalSerial gatewayTerminal;

struct GatewaySerialPort {
  int availableForWrite() { return Serial.availableForWrite(); }

  size_t write(const uint8_t* data, size_t size) {
    // Heltec WiFi LoRa 32 V2 uses UART0 for Serial. HardwareSerial::write
    // can return the requested size without reporting the driver's result.
    const int accepted = uart_write_bytes(UART_NUM_0, reinterpret_cast<const char*>(data), size);
    return accepted > 0 ? static_cast<size_t>(accepted) : 0;
  }
};

static GatewaySerialPort gatewaySerialPort;
static FramedSerial<GatewaySerialPort> framedSerial(gatewaySerialPort);
static GatewaySerialOutput<GatewaySerialPort> gatewayOutput(framedSerial, gatewayTerminal);

uint16_t nextGatewaySequence = 0;
uint16_t nextPingToken = 1;
uint32_t lastOfflineSweepMs = 0;
static uint32_t radioWindowStartMs = 0;

struct RadioWindowStats {
  int32_t rssiSumX100;
  int32_t snrSumX100;
  uint16_t recentSequences[MAX_RECENT_RADIO_SEQUENCES];
  uint16_t windowSampleCount;
  int16_t rssiMeanX100;
  int16_t snrMeanX100;
  uint16_t radioSampleCount;
  uint8_t recentSequenceCount;
  uint8_t nextRecentSequence;
  bool rssiMeanValid;
  bool snrMeanValid;
};

struct CollarState {
  bool seen;
  bool online;
  uint32_t lastSeenMs;
  int lastRssi;
  float lastSnr;
  uint16_t lastTelemetrySequence;
  bool haveTelemetrySequence;
  bool terminalHasRadioMetrics;
  uint16_t recentReliable[MAX_RECENT_RELIABLE];
  uint8_t recentReliableCount;
  uint8_t recentReliableNext;
  RadioWindowStats radio;
};

// IDs validos de coleira: 0x01..0xFE. O indice 0 existe, mas nao e usado.
CollarState collars[255];

struct RadioSummaryEntry {
  int16_t rssiMeanX100;
  int16_t snrMeanX100;
  uint16_t radioSampleCount;
  bool pending;
  bool serialAttempted;
};

// Metadados compartilhados por todas as coleiras da mesma janela do gateway.
struct RadioSummaryFrame {
  uint64_t windowId;
  uint32_t startedAtMs;
  RadioSummaryEntry entries[255];
};

struct RadioWindowSummary {
  uint64_t sessionId;
  uint64_t windowId;
  uint32_t startedAtMs;
  uint32_t durationMs;
  int16_t rssiMeanX100;
  int16_t snrMeanX100;
  uint16_t radioSampleCount;
  uint8_t collarId;
  bool rssiMeanValid;
  bool snrMeanValid;
  bool serialAttempted;
};

static uint64_t radioSessionId = 0;
static uint64_t radioNextWindowId = 1;
static RadioSummaryFrame radioSummaryFrames[RADIO_SUMMARY_CAPACITY];
static uint8_t radioSummaryTail = 0;
static uint8_t radioSummaryFrameCount = 0;
static uint64_t radioDroppedSummaries = 0;
static uint16_t radioTerminalNextId = 255;
static uint8_t radioTerminalFrame = 0;
static uint32_t radioTerminalLastMs = 0;
static uint32_t radioTerminalSkipped = 0;
static_assert(RADIO_WINDOW_MS == PDR_WINDOW_MS, "PDR and RADIO use the same clock boundaries");

struct PdrSlot {
  PositionPdr tracker;
  PdrReport latest;
  bool pending;
  bool hasReport;
};

static PdrSlot pdrSlots[255]{};
static bool pdrTxReady = false;
static unsigned pdrLastId = 0;
static unsigned pdrPendingCount = 0;
static uint32_t pdrLastPollMs = 0;
static uint32_t pdrOverwritten = 0;
static uint32_t pdrShortWrites = 0;
static ProfilePlan profilePlan;

static RadioSummaryFrame* findPendingRadioFrame(uint8_t collarId);
static bool peekPendingRadioSummary(uint8_t collarId, RadioWindowSummary& out);

static const uint8_t HDOP_TRACKED_COLLARS = 64;
static const uint8_t HDOP_PENDING_PER_COLLAR = 2;
static const uint8_t HDOP_DEDUP_WINDOWS = 32;
static const uint32_t HDOP_IDLE_RECLAIM_MS = 600000UL;

struct ReceivedHdopSummary {
  HdopSummaryPayload summary;
  uint32_t receivedAtMs;
  bool serialAttempted;
};

struct HdopSessionHistory {
  uint64_t sessionId;
  uint64_t newestWindowId;
  uint32_t windowsSeen;
};

struct HdopCollarStore {
  ReceivedHdopSummary pending[HDOP_PENDING_PER_COLLAR];
  HdopSessionHistory sessions[2];
  uint32_t lastAcceptedAtMs;
  uint8_t collarId;
  uint8_t pendingCount;
};

static HdopCollarStore hdopStores[HDOP_TRACKED_COLLARS];
static uint64_t hdopDroppedPending = 0;
static uint64_t hdopRejectedNoSlot = 0;
static uint64_t hdopRejectedOlder = 0;
static uint64_t hdopRejectedDuplicateOrStale = 0;
static uint64_t hdopRejectedUnorderedSession = 0;

static HdopCollarStore* findHdopCollarStore(uint8_t collarId);
static HdopCollarStore* acquireHdopCollarStore(uint8_t collarId, uint32_t now);
static bool retainHdopSummary(HdopCollarStore& store, const HdopSummaryPayload& summary, uint32_t now);
static bool peekPendingReceivedHdopSummary(uint8_t collarId, ReceivedHdopSummary& out);

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
  bool hasRequestId;
  uint32_t requestId;
};

FenceTxSession fenceTx;
static const uint32_t FENCE_CHUNK_GAP_MS = 120UL;

void onFenceReliableAck(uint8_t collarId, uint16_t sequence);
void onFenceReliableNack(uint8_t collarId, uint16_t sequence,
                         ErrorCode errorCode, uint16_t detail);
void onFenceDeliveryFailed(uint8_t collarId, uint16_t sequence);

void emitCommandResponse(uint32_t requestId, const char* command,
                         bool ok, const char* detail = nullptr);
void emitFenceTransfer(const char* state, const char* detail = nullptr);
void emitAppHeader(const char* kind, bool reserved = false);

// ============================================================
// UTILITARIOS
// ============================================================

void printHexPacket(const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    if (data[i] < 0x10) gatewayOutput.print('0');
    gatewayOutput.print(data[i], HEX);
    if (i + 1 < len) gatewayOutput.print(' ');
  }
}

static void terminalCoordinate(char (&text)[24], int32_t e7) {
  if (e7 == COORD_UNAVAILABLE) strcpy(text, "N/A");
  else snprintf(text, sizeof(text), "%.7f", e7ToDegrees(e7));
}

static const char* gatewayZone(uint8_t flags) {
  if (!(flags & STATE_GPS_VALID)) return "N/A";
  if (!(flags & STATE_INSIDE)) return "FORA";
  if (flags & STATE_VERY_NEAR) return "CRITICO";
  if (flags & STATE_NEAR) return "ATENCAO";
  return "SEGURO";
}

static void printPositionStatus(const PacketView& packet, const TelemetryPayload& d,
                                bool physical, int rssi, float snr) {
  char hdop[12] = "N/A", rssiText[20] = "N/A", snrText[16] = "N/A";
  const bool gpsValid = (d.stateFlags & STATE_GPS_VALID) != 0;
  if (gpsValid && d.hdopX100 != U16_UNAVAILABLE)
    snprintf(hdop, sizeof(hdop), "%.2f", d.hdopX100 / 100.0);
  if (physical) snprintf(rssiText, sizeof(rssiText), "%ddBm", rssi);
  if (physical && isfinite(snr)) snprintf(snrText, sizeof(snrText), "%.2fdB", static_cast<double>(snr));
  gatewayTerminal.print(framedSerial, "[POS] [GATEWAY] [COL %03u] seq=%u GPS=%s HDOP=%s RSSI=%s SNR=%s ZONA=%s\n",
                unsigned(packet.header.collarId), unsigned(packet.header.sequence),
                gpsValid ? "VALIDO" : "INVALIDO", hdop, rssiText, snrText, gatewayZone(d.stateFlags));
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

void emitAppHeader(const char* kind, bool reserved) {
  if (reserved) gatewayOutput.reserveFrame();
  gatewayOutput.print("[APP] {\"schema\":\"");
  gatewayOutput.print(SERIAL_SCHEMA);
  gatewayOutput.print("\",\"kind\":\"");
  gatewayOutput.print(kind);
  gatewayOutput.print('"');
}

void emitCommandResponse(uint32_t requestId, const char* command,
                         bool ok, const char* detail) {
  emitAppHeader("command_response", true);
  gatewayOutput.print(",\"requestId\":"); gatewayOutput.print(requestId);
  gatewayOutput.print(",\"command\":\""); gatewayOutput.print(command); gatewayOutput.print('"');
  gatewayOutput.print(",\"ok\":"); gatewayOutput.print(ok ? "true" : "false");
  if (detail && detail[0]) {
    gatewayOutput.print(",\"detail\":\""); gatewayOutput.print(detail); gatewayOutput.print('"');
  }
  gatewayOutput.println('}');
  if (!ok || strcmp(command, "FENCE_POINT") != 0 || GATEWAY_TERMINAL_DETAIL) {
    gatewayTerminal.print(framedSerial, "[%s] [GATEWAY] %.32s requestId=%lu%s%s\n", ok ? "OK" : "ERRO", command,
                  static_cast<unsigned long>(requestId), detail ? " motivo=" : "", detail ? detail : "");
  }
}

void emitFenceTransfer(const char* state, const char* detail) {
  if (!fenceTx.hasRequestId) return;
  emitAppHeader("fence_transfer", true);
  gatewayOutput.print(",\"requestId\":"); gatewayOutput.print(fenceTx.requestId);
  gatewayOutput.print(",\"collarId\":"); gatewayOutput.print(fenceTx.collarId);
  gatewayOutput.print(",\"fenceVersion\":"); gatewayOutput.print(fenceTx.version);
  gatewayOutput.print(",\"state\":\""); gatewayOutput.print(state); gatewayOutput.print('"');
  if (detail && detail[0]) {
    gatewayOutput.print(",\"detail\":\""); gatewayOutput.print(detail); gatewayOutput.print('"');
  }
  gatewayOutput.println('}');
}

void emitCollarState(uint8_t collarId, const char* state) {
  emitAppHeader("collar_state");
  gatewayOutput.print(",\"collarId\":"); gatewayOutput.print(collarId);
  gatewayOutput.print(",\"state\":\""); gatewayOutput.print(state); gatewayOutput.println("\"}");
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

  if (GATEWAY_TERMINAL_DETAIL) {
    char hex[MAX_PACKET_SIZE * 3];
    terminalHex(data, len, hex);
    gatewayTerminal.print(framedSerial, "[TX LoRa] [GATEWAY] bytes=%u %s\n", unsigned(len), hex);
  }

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
    gatewayOutput.println("[PROTO] tentativa de envio confiavel invalida");
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
    gatewayOutput.println("[PROTO] fila de mensagens confiaveis cheia");
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
    gatewayOutput.println("[RADIO] falha no envio inicial");
  }
  return true;
}

void acknowledgeReliable(uint8_t collarId, uint16_t sequence) {
  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (tx.active && tx.collarId == collarId && tx.sequence == sequence) {
      tx.active = false;
      gatewayTerminal.print(framedSerial, "[ACK LoRa] [GATEWAY] [COL %03u] confirmado seq=%u\n", unsigned(collarId), unsigned(sequence));
      onFenceReliableAck(collarId, sequence);
      return;
    }
  }

  if (GATEWAY_TERMINAL_DETAIL)
    gatewayTerminal.print(framedSerial, "[DIAG] [GATEWAY] [COL %03u] ACK LoRa sem pendencia seq=%u\n", unsigned(collarId), unsigned(sequence));
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

  gatewayTerminal.print(framedSerial, "[NACK LoRa] [GATEWAY] [COL %03u] seq=%u erro=%s detalhe=0x%04X\n",
                unsigned(collarId), unsigned(sequence), errorName(errorCode), unsigned(detail));
  onFenceReliableNack(collarId, sequence, errorCode, detail);
}

void serviceReliableTx() {
  const uint32_t now = millis();

  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (!tx.active) continue;
    if (static_cast<int32_t>(now - tx.nextActionMs) < 0) continue;

    if (tx.transmissions >= MAX_TRANSMISSIONS) {
      gatewayTerminal.print(framedSerial, "[ERRO] [GATEWAY] [COL %03u] ENTREGA LoRa ESGOTADA seq=%u tentativas=%u\n",
                    unsigned(tx.collarId), unsigned(tx.sequence), unsigned(tx.transmissions));
      const uint8_t failedCollar = tx.collarId;
      const uint16_t failedSequence = tx.sequence;
      tx.active = false;
      onFenceDeliveryFailed(failedCollar, failedSequence);
      continue;
    }

    tx.packet[0] |= FLAG_RETRY;
    ++tx.transmissions;

    gatewayTerminal.print(framedSerial, "[RETRY LoRa] [GATEWAY] [COL %03u] seq=%u tentativa=%u\n",
                  unsigned(tx.collarId), unsigned(tx.sequence), unsigned(tx.transmissions));

    transportSend(tx.packet, tx.length);
    tx.nextActionMs = scheduleRetryFromNow();
  }
}

// ============================================================
// RESPOSTAS CONTROL
// ============================================================

bool sendAckResponse(uint8_t collarId, uint16_t ackedSequence) {
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeAck(packet, sizeof(packet), collarId,
                               allocateSequence(), ackedSequence, 0x00);
  return len && transportSend(packet, len);
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

void abortFenceTransfer(const char* reason, const char* detail = nullptr) {
  if (fenceTx.active) {
    gatewayTerminal.print(framedSerial, "[ERRO] [GATEWAY] [COL %03u] CERCA v%u abortada motivo=%s\n",
                  unsigned(fenceTx.collarId), unsigned(fenceTx.version), reason);
    emitFenceTransfer("failed", detail ? detail : reason);
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
  gatewayOutput.print("[FENCE TX] FENCE_BEGIN enviado. seq=");
  gatewayOutput.println(seq);
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

  if (GATEWAY_TERMINAL_DETAIL) {
    gatewayTerminal.print(framedSerial, "[DIAG] [GATEWAY] [COL %03u] CERCA v%u chunk=%u/%u pontos=%u\n",
                  unsigned(fenceTx.collarId), unsigned(fenceTx.version), unsigned(chunkIndex),
                  unsigned(fenceTx.chunkCount - 1), unsigned(pointCount));
  }
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
  gatewayOutput.print("[FENCE TX] FENCE_COMMIT enviado. seq=");
  gatewayOutput.println(seq);
  return true;
}

bool startFenceTransfer() {
  if (!fenceTx.staged) {
    gatewayOutput.println("[FENCE TX] nenhuma cerca preparada. Use FENCE_NEW/FENCE_POINT.");
    return false;
  }
  if (fenceTx.active) {
    gatewayOutput.println("[FENCE TX] ja existe uma transferencia em andamento.");
    return false;
  }
  if ((fenceTx.pointMask & requiredPointMask(fenceTx.vertexCount)) !=
      requiredPointMask(fenceTx.vertexCount)) {
    gatewayOutput.println("[FENCE TX] faltam pontos na cerca preparada.");
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

  gatewayTerminal.print(framedSerial, "==== VFENCE | GATEWAY | CERCA | COL %03u ====\n"
                " inicio v%u vertices=%u chunks=%u CRC32=0x%08lX\n",
                unsigned(fenceTx.collarId), unsigned(fenceTx.version), unsigned(fenceTx.vertexCount),
                unsigned(fenceTx.chunkCount), static_cast<unsigned long>(fenceTx.crc32));

  if (!sendFenceBeginReliable()) {
    abortFenceTransfer("falha ao enviar FENCE_BEGIN", "radio_send_failed");
    return false;
  }
  return true;
}

void onFenceReliableAck(uint8_t collarId, uint16_t sequence) {
  if (!fenceTx.active || fenceTx.collarId != collarId) return;

  if (fenceTx.phase == FenceTxPhase::WAIT_BEGIN_ACK &&
      sequence == fenceTx.beginSequence) {
    gatewayOutput.println("[FENCE TX] FENCE_BEGIN confirmado; enviando chunks.");
    fenceTx.phase = FenceTxPhase::SEND_ALL_CHUNKS;
    fenceTx.nextChunk = 0;
    fenceTx.nextChunkAtMs = millis();
    return;
  }

  if (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK &&
      sequence == fenceTx.commitSequence) {
    gatewayTerminal.print(framedSerial, "[OK] [GATEWAY] [COL %03u] CERCA v%u confirmada pela coleira seq=%u\n",
                  unsigned(collarId), unsigned(fenceTx.version), unsigned(sequence));
    emitFenceTransfer("confirmed");
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
    abortFenceTransfer("FENCE_BEGIN rejeitado", errorName(errorCode));
    return;
  }

  if (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK &&
      sequence == fenceTx.commitSequence) {
    if (errorCode == ErrorCode::MISSING_CHUNKS) {
      const uint16_t missing = detail & validChunkMask(fenceTx.chunkCount);
      if (missing == 0) {
        abortFenceTransfer("NACK MISSING_CHUNKS sem bitmap valido", "MISSING_CHUNKS");
        return;
      }
      fenceTx.missingChunkMask = missing;
      fenceTx.phase = FenceTxPhase::RESEND_MISSING_CHUNKS;
      fenceTx.nextChunkAtMs = millis();
      gatewayOutput.print("[FENCE TX] reenviando chunks ausentes. bitmap=0x");
      gatewayOutput.println(missing, HEX);
      return;
    }

    if (errorCode == ErrorCode::CRC_ERROR) {
      abortFenceTransfer("CRC_ERROR; reinicie a transferencia com FENCE_SEND", "CRC_ERROR");
      return;
    }

    abortFenceTransfer("FENCE_COMMIT rejeitado", errorName(errorCode));
  }
}

void onFenceDeliveryFailed(uint8_t collarId, uint16_t sequence) {
  if (!fenceTx.active || fenceTx.collarId != collarId) return;
  if ((fenceTx.phase == FenceTxPhase::WAIT_BEGIN_ACK && sequence == fenceTx.beginSequence) ||
      (fenceTx.phase == FenceTxPhase::WAIT_COMMIT_ACK && sequence == fenceTx.commitSequence)) {
    abortFenceTransfer("ACK nao recebido apos 4 transmissoes", "ACK_TIMEOUT");
  }
}

void serviceFenceTransfer() {
  if (!fenceTx.active) return;
  const uint32_t now = millis();

  if (fenceTx.phase == FenceTxPhase::SEND_ALL_CHUNKS) {
    if (static_cast<int32_t>(now - fenceTx.nextChunkAtMs) < 0) return;

    if (fenceTx.nextChunk < fenceTx.chunkCount) {
      if (!sendFenceChunkIndex(fenceTx.nextChunk)) {
        abortFenceTransfer("falha ao enviar chunk", "radio_send_failed");
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
          abortFenceTransfer("falha ao reenviar chunk", "radio_send_failed");
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
      abortFenceTransfer("falha ao enviar FENCE_COMMIT", "radio_send_failed");
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

static int16_t radioMeanX100(int32_t sum, uint16_t count) {
  const int32_t halfCount = count / 2;
  return static_cast<int16_t>((sum + (sum < 0 ? -halfCount : halfCount)) / count);
}

static RadioSummaryFrame* findPendingRadioFrame(uint8_t collarId) {
  if (!validCollarId(collarId)) return nullptr;
  const uint8_t head = (radioSummaryTail + RADIO_SUMMARY_CAPACITY - radioSummaryFrameCount)
      % RADIO_SUMMARY_CAPACITY;
  for (uint8_t offset = 0; offset < radioSummaryFrameCount; ++offset) {
    RadioSummaryFrame& frame = radioSummaryFrames[(head + offset) % RADIO_SUMMARY_CAPACITY];
    if (frame.entries[collarId].pending) return &frame;
  }
  return nullptr;
}

static bool peekPendingRadioSummary(uint8_t collarId, RadioWindowSummary& out) {
  const RadioSummaryFrame* frame = findPendingRadioFrame(collarId);
  if (!frame) return false;
  const RadioSummaryEntry& entry = frame->entries[collarId];
  const bool valid = entry.radioSampleCount != 0;
  out = {radioSessionId, frame->windowId, frame->startedAtMs, RADIO_WINDOW_MS,
         entry.rssiMeanX100, entry.snrMeanX100, entry.radioSampleCount, collarId, valid, valid,
         entry.serialAttempted};
  return true;
}

static bool consumePendingRadioSummary(uint8_t collarId, uint64_t sessionId, uint64_t windowId) {
  if (sessionId != radioSessionId) return false;
  RadioSummaryFrame* frame = findPendingRadioFrame(collarId);
  if (!frame || frame->windowId != windowId) return false;
  frame->entries[collarId].pending = false;
  return true;
}

static void publishRadioWindow(uint32_t startedAtMs, bool useSamples, bool report) {
  if (report) {
    const auto& previous = radioSummaryFrames[radioTerminalFrame];
    for (uint16_t id = radioTerminalNextId; id <= 254; ++id) {
      if (previous.entries[id].pending || previous.entries[id].serialAttempted) ++radioTerminalSkipped;
    }
    radioTerminalFrame = radioSummaryTail;
    radioTerminalNextId = 1;
  }
  RadioSummaryFrame& frame = radioSummaryFrames[radioSummaryTail];
  frame.windowId = radioNextWindowId++;
  frame.startedAtMs = startedAtMs;
  radioSummaryTail = (radioSummaryTail + 1) % RADIO_SUMMARY_CAPACITY;
  if (radioSummaryFrameCount < RADIO_SUMMARY_CAPACITY) ++radioSummaryFrameCount;

  for (uint16_t id = 1; id <= 0xFE; ++id) {
    auto& pdr = pdrSlots[id];
    if (pdr.tracker.known()) {
      const auto result = pdr.tracker.closeWindow();
      const auto published = useSamples ? result : PdrWindowResult{};
      if (!pdr.hasReport || published.reason != PdrReason::NO_SAMPLES ||
          pdr.latest.result.reason != PdrReason::NO_SAMPLES) {
        if (pdr.pending) ++pdrOverwritten;
        else ++pdrPendingCount;
        pdr.latest = {published, frame.windowId, startedAtMs};
        pdr.hasReport = pdr.pending = true;
      }
    }
    RadioSummaryEntry& entry = frame.entries[id];
    if (entry.pending) ++radioDroppedSummaries;
    entry.pending = false;
    CollarState& collar = collars[id];
    if (!collar.seen) continue;
    RadioWindowStats& radio = collar.radio;

    radio.radioSampleCount = useSamples ? radio.windowSampleCount : 0;
    radio.rssiMeanValid = radio.snrMeanValid = radio.radioSampleCount != 0;
    radio.rssiMeanX100 = 0;
    radio.snrMeanX100 = 0;
    if (radio.radioSampleCount != 0) {
      radio.rssiMeanX100 = radioMeanX100(radio.rssiSumX100, radio.radioSampleCount);
      radio.snrMeanX100 = radioMeanX100(radio.snrSumX100, radio.radioSampleCount);
    }
    radio.rssiSumX100 = 0;
    radio.snrSumX100 = 0;
    radio.windowSampleCount = 0;
    entry = {radio.rssiMeanX100, radio.snrMeanX100, radio.radioSampleCount, true, false};

  }
}

static void serviceRadioWindows(uint32_t now) {
  const uint32_t elapsed = now - radioWindowStartMs;
  if (elapsed < RADIO_WINDOW_MS) return;
  const uint32_t completedWindows = elapsed / RADIO_WINDOW_MS;
  // Pular apenas as janelas antigas que nao caberiam na retencao.
  const uint32_t skipped = completedWindows > RADIO_SUMMARY_CAPACITY
      ? completedWindows - RADIO_SUMMARY_CAPACITY : 0;
  if (skipped != 0) {
    uint16_t seenCollars = 0;
    for (uint16_t id = 1; id <= 0xFE; ++id) {
      if (collars[id].seen) ++seenCollars;
    }
    radioNextWindowId += skipped;
    radioDroppedSummaries += static_cast<uint64_t>(skipped) * seenCollars;
  }
  for (uint32_t offset = skipped; offset < completedWindows; ++offset) {
    publishRadioWindow(radioWindowStartMs + offset * RADIO_WINDOW_MS,
                       offset == 0, offset + 1 == completedWindows);
  }
  radioWindowStartMs += completedWindows * RADIO_WINDOW_MS;
}

static bool gatewayTerminalIdle() {
  if (fenceTx.active || Serial.available()) return false;
  for (const PendingReliableTx& tx : pendingTx) {
    if (tx.active) return false;
  }
  return true;
}

static void serviceRadioTerminal(uint32_t now) {
  if (radioTerminalNextId > 254 || now - radioTerminalLastMs < 100 || !gatewayTerminalIdle()) return;
  const auto& frame = radioSummaryFrames[radioTerminalFrame];
  while (radioTerminalNextId <= 254) {
    const unsigned id = radioTerminalNextId++;
    const auto& entry = frame.entries[id];
    if (!entry.pending && !entry.serialAttempted) continue;
    const bool valid = entry.radioSampleCount != 0;
    const bool expanded = framedSerial.availableForWrite() >= 240;
    char rssi[20] = "N/A", snr[20] = "N/A";
    if (valid) {
      snprintf(rssi, sizeof(rssi), "%.2f%s", entry.rssiMeanX100 / 100.0, expanded ? " dBm" : "");
      snprintf(snr, sizeof(snr), "%.2f%s", entry.snrMeanX100 / 100.0, expanded ? " dB" : "");
    }
    gatewayTerminal.print(framedSerial,
        expanded ? "==== VFENCE | GATEWAY | RADIO 60s | COL %03u ====\n"
                   " sessao=%016llx janela=%016llx inicioGateway=%lu ms duracao=60000 ms\n"
                   " amostras=%u RSSI=%s SNR=%s validade=%s | fila=%s\n"
                 : "[RADIO 60s] [COL %03u] s=%016llx j=%016llx t=%lu n=%u RSSI=%s SNR=%s %s %s\n",
        id, static_cast<unsigned long long>(radioSessionId), static_cast<unsigned long long>(frame.windowId),
        static_cast<unsigned long>(frame.startedAtMs), unsigned(entry.radioSampleCount),
        rssi, snr, valid ? (expanded ? "VALIDA" : "V") : (expanded ? "INVALIDA" : "INV"),
        entry.pending ? "RETIDA" : (expanded ? "JA CONSUMIDA" : "CONSUMIDA"));
    radioTerminalLastMs = now;
    return;
  }
}

static void servicePdrOutput(uint32_t now) {
  if (!pdrPendingCount || now - pdrLastPollMs < 100 || fenceTx.active || Serial.available()) return;
  for (const auto& tx : pendingTx) {
    if (tx.active) return;
  }
  pdrLastPollMs = now;
  for (unsigned i = 1; i <= 254; ++i) {
    const unsigned id = (pdrLastId + i - 1) % 254 + 1;
    auto& slot = pdrSlots[id];
    if (!slot.pending) continue;
    if (PDR_SERIAL_EXPORT_ENABLED && !pdrTxReady) return;
    if (pdrTxReady) {
      char line[512];
      const size_t length = formatPdrReport(slot.latest, radioSessionId, id, line);
      if (!length) return;
      const auto sent = framedSerial.tryWrite(reinterpret_cast<const uint8_t*>(line), length);
      if (sent == SerialWriteResult::REFUSED) return;
      // PDR keeps its best-effort policy. Only framing recovery is shared.
      if (sent == SerialWriteResult::PARTIAL) ++pdrShortWrites;
    }

    const auto& result = slot.latest.result;
    char estimate[48] = "PDR=N/A";
    if (result.reason == PdrReason::OK)
      snprintf(estimate, sizeof(estimate), "tx~%u perda~%u PDR~%.2f%%",
               unsigned(result.estimatedTx), unsigned(result.estimatedTx - result.received),
               result.pdrX100 / 100.0);
    char profile[6] = "N/A";
    if (result.profileId < LORA_PROFILE_COUNT) snprintf(profile, sizeof(profile), "P%u", unsigned(result.profileId));
    char human[152];
    const int length = snprintf(human, sizeof(human),
        "==== VFENCE | GATEWAY | PDR 60s | COL %03u ====\nrx=%u %s %s %s\n",
        id, unsigned(result.received), estimate, pdrReasonName(result.reason), profile);
    if (length <= 0 || length >= static_cast<int>(sizeof(human))) return;
    if (!pdrTxReady && framedSerial.availableForWrite() < length) return;
    gatewayTerminal.write(framedSerial, reinterpret_cast<const uint8_t*>(human), length);
    pdrLastId = id;
    slot.pending = false;
    --pdrPendingCount;
    if (GATEWAY_TERMINAL_DETAIL && (pdrOverwritten || pdrShortWrites)) {
      char diag[112];
      const int bytes = snprintf(diag, sizeof(diag), "[DIAG] [GATEWAY] PDR sobrescritas=%lu escritasParciais=%lu\n",
          static_cast<unsigned long>(pdrOverwritten), static_cast<unsigned long>(pdrShortWrites));
      if (bytes > 0 && bytes < int(sizeof(diag)) &&
          gatewayTerminal.write(framedSerial, reinterpret_cast<const uint8_t*>(diag), bytes, false)) {
        pdrOverwritten = pdrShortWrites = 0;
      }
    }
    return;
  }
}

static void accumulateRadioSample(RadioWindowStats& radio, uint16_t sequence, int rssi, float snr) {
  for (uint8_t i = 0; i < radio.recentSequenceCount; ++i) {
    if (radio.recentSequences[i] == sequence) return;
  }

  // Cada amostra cabe em int16_t na escala x100. Mesmo com UINT16_MAX
  // amostras, a soma e o ajuste de arredondamento cabem em int32_t.
  if (radio.windowSampleCount == UINT16_MAX || rssi < INT16_MIN / 100 || rssi > INT16_MAX / 100) return;
  const float snrX100 = snr * 100.0f;
  if (!isfinite(snrX100) || snrX100 < INT16_MIN || snrX100 > INT16_MAX) return;

  radio.recentSequences[radio.nextRecentSequence] = sequence;
  radio.nextRecentSequence = (radio.nextRecentSequence + 1) % MAX_RECENT_RADIO_SEQUENCES;
  if (radio.recentSequenceCount < MAX_RECENT_RADIO_SEQUENCES) ++radio.recentSequenceCount;
  radio.rssiSumX100 += static_cast<int32_t>(rssi) * 100;
  radio.snrSumX100 += static_cast<int32_t>(lroundf(snrX100));
  ++radio.windowSampleCount;
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
    c.terminalHasRadioMetrics = isfinite(snr);
  }

  if (firstSeen || wasOffline) {
    gatewayTerminal.print(framedSerial, "[OK] [GATEWAY] [COL %03u] %s origem=%s\n", unsigned(collarId),
                  wasOffline ? "ONLINE NOVAMENTE" : "ONLINE", hasRadioMetrics ? "LoRa" : "injetado");
    emitCollarState(collarId, "ONLINE");
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
      gatewayTerminal.print(framedSerial, "[ALERTA] [GATEWAY] [COL %03u] OFFLINE contato=%lu s\n",
                    unsigned(id), static_cast<unsigned long>((now - c.lastSeenMs) / 1000UL));

      emitCollarState(static_cast<uint8_t>(id), "OFFLINE");
    }
  }
}

// ============================================================
// SAIDA PARA A APLICACAO (JSON EM UMA LINHA)
// Nesta fase e apenas uma ponte via Serial. Futuramente pode ser
// substituida por Wi-Fi/MQTT/HTTP sem alterar o protocolo LoRa.
// ============================================================

void appPrintCoordinate(const char* key, int32_t value, bool comma = true) {
  gatewayOutput.print('"');
  gatewayOutput.print(key);
  gatewayOutput.print("\":");
  if (value == COORD_UNAVAILABLE) gatewayOutput.print("null");
  else gatewayOutput.print(e7ToDegrees(value), 7);
  if (comma) gatewayOutput.print(',');
}

void appPrintBattery(uint8_t value, bool comma = true) {
  gatewayOutput.print("\"battery\":");
  if (value == U8_UNAVAILABLE) gatewayOutput.print("null");
  else gatewayOutput.print(value);
  if (comma) gatewayOutput.print(',');
}

void appPrintHdop(uint16_t value, bool comma = true) {
  gatewayOutput.print("\"hdop\":");
  if (value == U16_UNAVAILABLE) gatewayOutput.print("null");
  else gatewayOutput.print(static_cast<double>(value) / 100.0, 2);
  if (comma) gatewayOutput.print(',');
}

// ============================================================
// DECODIFICACAO TELEMETRY
// ============================================================

static HdopCollarStore* findHdopCollarStore(uint8_t collarId) {
  if (!validCollarId(collarId)) return nullptr;
  for (HdopCollarStore& store : hdopStores) {
    if (store.collarId == collarId) return &store;
  }
  return nullptr;
}

static HdopCollarStore* acquireHdopCollarStore(uint8_t collarId, uint32_t now) {
  if (!validCollarId(collarId)) return nullptr;
  HdopCollarStore* freeSlot = nullptr;
  HdopCollarStore* idleSlot = nullptr;
  for (HdopCollarStore& store : hdopStores) {
    if (store.collarId == collarId) return &store;
    if (store.collarId == 0 && !freeSlot) freeSlot = &store;
    if (store.pendingCount == 0 && now - store.lastAcceptedAtMs >= HDOP_IDLE_RECLAIM_MS &&
        (!idleSlot || now - store.lastAcceptedAtMs > now - idleSlot->lastAcceptedAtMs)) {
      idleSlot = &store;
    }
  }
  HdopCollarStore* slot = freeSlot ? freeSlot : idleSlot;
  if (!slot) return nullptr;
  *slot = {};
  slot->collarId = collarId;
  return slot;
}

static bool isHdopWindowUnseen(const HdopCollarStore& store, const HdopSessionHistory* history,
                              const HdopSummaryPayload& summary) {
  for (uint8_t i = 0; i < store.pendingCount; ++i) {
    const HdopSummaryPayload& pending = store.pending[i].summary;
    if (pending.sessionId == summary.sessionId && pending.windowId == summary.windowId) return false;
  }
  if (!history || summary.windowId > history->newestWindowId) return true;
  const uint64_t age = history->newestWindowId - summary.windowId;
  return age < HDOP_DEDUP_WINDOWS && !(history->windowsSeen & (uint32_t{1} << age));
}

static HdopSessionHistory* findHdopSession(HdopCollarStore& store, uint64_t sessionId) {
  for (HdopSessionHistory& session : store.sessions) {
    if (session.windowsSeen != 0 && session.sessionId == sessionId) return &session;
  }
  return nullptr;
}

static void markHdopWindowSeen(HdopCollarStore& store, HdopSessionHistory* history,
                               const HdopSummaryPayload& summary) {
  const uint64_t windowId = summary.windowId;
  if (!history) {
    // Uma sessao desconhecida so entra com espaco livre. Se ha outra
    // pendencia, preserve o historico dela antes de reciclar uma sessao.
    if (store.pendingCount != HDOP_PENDING_PER_COLLAR ||
        store.sessions[1].windowsSeen == 0 ||
        store.sessions[1].sessionId != store.pending[0].summary.sessionId) {
      store.sessions[1] = store.sessions[0];
    }
    store.sessions[0] = {summary.sessionId, windowId, 1};
    return;
  }
  if (windowId > history->newestWindowId) {
    const uint64_t delta = windowId - history->newestWindowId;
    history->windowsSeen = delta >= HDOP_DEDUP_WINDOWS
        ? 1U : (history->windowsSeen << static_cast<uint8_t>(delta)) | 1U;
    history->newestWindowId = windowId;
    return;
  }
  history->windowsSeen |= uint32_t{1} << (history->newestWindowId - windowId);
}

static bool retainHdopSummary(HdopCollarStore& store, const HdopSummaryPayload& summary, uint32_t now) {
  if (store.pendingCount == HDOP_PENDING_PER_COLLAR) {
    uint8_t victim = HDOP_PENDING_PER_COLLAR;
    for (uint8_t i = 0; i < store.pendingCount; ++i) {
      const HdopSummaryPayload& pending = store.pending[i].summary;
      if (pending.sessionId == summary.sessionId &&
          (victim == HDOP_PENDING_PER_COLLAR || pending.windowId < store.pending[victim].summary.windowId)) {
        victim = i;
      }
    }
    // IDs de sessao sao aleatorios. So substitua uma janela da mesma sessao.
    if (victim == HDOP_PENDING_PER_COLLAR) {
      ++hdopRejectedUnorderedSession;
      return false;
    }
    if (summary.windowId <= store.pending[victim].summary.windowId) {
      ++hdopRejectedOlder;
      return false;
    }
    if (victim == 0) store.pending[0] = store.pending[1];
    --store.pendingCount;
    ++hdopDroppedPending;
  }
  store.pending[store.pendingCount++] = {summary, now, false};
  store.lastAcceptedAtMs = now;
  return true;
}

static bool peekPendingReceivedHdopSummary(uint8_t collarId, ReceivedHdopSummary& out) {
  const HdopCollarStore* store = findHdopCollarStore(collarId);
  if (!store || store->pendingCount == 0) return false;
  out = store->pending[0];
  return true;
}

static bool consumePendingReceivedHdopSummary(uint8_t collarId, uint64_t sessionId, uint64_t windowId) {
  HdopCollarStore* store = findHdopCollarStore(collarId);
  if (!store || store->pendingCount == 0) return false;
  const HdopSummaryPayload& head = store->pending[0].summary;
  if (head.sessionId != sessionId || head.windowId != windowId) return false;
  if (store->pendingCount == HDOP_PENDING_PER_COLLAR) store->pending[0] = store->pending[1];
  --store->pendingCount;
  return true;
}

struct GatewayMetricSerialIo {
  bool peek(MetricSerialKind kind, uint8_t collarId, MetricSerialSummary& out) const {
    if (kind == MetricSerialKind::HDOP) {
      ReceivedHdopSummary received;
      if (!peekPendingReceivedHdopSummary(collarId, received)) return false;
      const HdopSummaryPayload& hdop = received.summary;
      out = {{hdop.sessionId, hdop.windowId, collarId, kind}, hdop.startedAtMs,
             received.receivedAtMs, hdop.durationMs, hdop.hdopSampleCount, hdop.hdopMeanX100,
             0, hdop.hdopMeanValid, false, received.serialAttempted};
    } else {
      RadioWindowSummary radio;
      if (!peekPendingRadioSummary(collarId, radio)) return false;
      out = {{radio.sessionId, radio.windowId, collarId, kind}, radio.startedAtMs, 0,
             radio.durationMs, radio.radioSampleCount, radio.rssiMeanX100, radio.snrMeanX100,
             radio.rssiMeanValid, radio.snrMeanValid, radio.serialAttempted};
    }
    return true;
  }

  bool write(const char* line, size_t length) const {
    return framedSerial.tryWrite(reinterpret_cast<const uint8_t*>(line), length) ==
           SerialWriteResult::COMPLETE;
  }

  void markAttempted(const MetricSerialId& id) const {
    // A leitura da cabeca, a escrita e esta marcacao executam no mesmo loop.
    if (id.kind == MetricSerialKind::HDOP) {
      findHdopCollarStore(id.collarId)->pending[0].serialAttempted = true;
    } else {
      findPendingRadioFrame(id.collarId)->entries[id.collarId].serialAttempted = true;
    }
  }

  bool consume(const MetricSerialId& id) const {
    if (id.kind == MetricSerialKind::HDOP) {
      return consumePendingReceivedHdopSummary(id.collarId, id.sessionId, id.windowId);
    }
    return consumePendingRadioSummary(id.collarId, id.sessionId, id.windowId);
  }
};

static MetricSerialExporter metricSerialExporter;
static GatewayMetricSerialIo metricSerialIo;
static bool metricSerialTxReady = false;
static void printMetricSerialNotice(MetricSerialNotice notice, const MetricSerialId& id) {
  const char* label = "TX SERIAL";
  const char* result = "aguardando METRIC_COMMITTED";
  const char* rule = "";
  switch (notice) {
    case MetricSerialNotice::SENT: break;
    case MetricSerialNotice::RETRIED: label = "RETRY"; break;
    case MetricSerialNotice::EXPIRED: label = "AVISO"; result = "turno expirado, pendencia preservada se retida"; break;
    case MetricSerialNotice::OVERWRITTEN: label = "AVISO"; result = "cabeca alterada, ACK nao aplicado"; break;
    case MetricSerialNotice::CONFIRMED:
      label = "ACK SQLITE";
      result = "identidade validada, pendencia consumida";
      rule = "============================================================\n";
      break;
  }
  gatewayTerminal.print(framedSerial,
      "%s[%s] [GATEWAY] [COL %03u] tipo=%s sessao=%016llx janela=%016llx %s\n",
      framedSerial.availableForWrite() >= 240 ? rule : "", label, unsigned(id.collarId),
      id.kind == MetricSerialKind::HDOP ? "HDOP" : "RADIO",
      static_cast<unsigned long long>(id.sessionId), static_cast<unsigned long long>(id.windowId), result);
}

static void serviceMetricSerialExport() {
  if (!METRIC_SERIAL_EXPORT_ENABLED || !metricSerialTxReady) return;
  bool ready = !fenceTx.active && !Serial.available() &&
      framedSerial.availableForWrite() >= static_cast<int>(METRIC_SERIAL_MAX_LINE);
  for (const PendingReliableTx& tx : pendingTx) {
    if (tx.active) {
      ready = false;
      break;
    }
  }
  const uint32_t now = millis();
  metricSerialExporter.service(now, radioSessionId, ready, metricSerialIo, printMetricSerialNotice);
}

static void serviceTerminalSummary(uint32_t now) {
  static uint32_t attemptedAt = 0;
  static uint64_t reported[9]{};
  if (now - attemptedAt < 60000UL || !gatewayTerminalIdle()) return;
  attemptedAt = now;
  const auto& metrics = metricSerialExporter.counts;
  if (GATEWAY_TERMINAL_DETAIL && metricSerialTxReady) {
    gatewayTerminal.tryPrint(framedSerial, "[DIAG] [GATEWAY] Serial tentativas=%llu retries=%llu confirmados=%llu\n",
        static_cast<unsigned long long>(metrics.attempts), static_cast<unsigned long long>(metrics.retries),
        static_cast<unsigned long long>(metrics.confirmed));
  }
  const uint64_t counts[] = {gatewayTerminal.omitted(), metrics.localRefusals, metrics.expiredTurns,
                             radioTerminalSkipped, hdopDroppedPending, radioDroppedSummaries,
                             metrics.overwrittenInFlight, framedSerial.operationalRefusals(),
                             framedSerial.shortWrites()};
  bool changed = false;
  for (size_t i = 0; i < sizeof(counts) / sizeof(counts[0]); ++i) {
    if (counts[i] != reported[i]) changed = true;
  }
  if (!changed) return;
  if (gatewayTerminal.tryPrint(framedSerial,
        "[AVISO] [GATEWAY] logsOmitidos=%lu recusasUART=%llu expirados=%llu radioPuladas=%lu"
        " hdopDesc=%llu radioDesc=%llu trocas=%llu appRecusados=%lu txParciais=%lu\n",
        static_cast<unsigned long>(counts[0]), static_cast<unsigned long long>(counts[1]),
        static_cast<unsigned long long>(counts[2]), static_cast<unsigned long>(counts[3]),
        static_cast<unsigned long long>(counts[4]), static_cast<unsigned long long>(counts[5]),
        static_cast<unsigned long long>(counts[6]), static_cast<unsigned long>(counts[7]),
        static_cast<unsigned long>(counts[8]))) {
    memcpy(reported, counts, sizeof(counts));
  }
}

static void processHdopSummary(const PacketView& packet, uint32_t receivedAtMs) {
  HdopSummaryPayload summary;
  const unsigned id = packet.header.collarId;
  if (!decodeHdopSummary(packet, summary)) {
    gatewayTerminal.print(framedSerial, "[ERRO] [GATEWAY] [COL %03u] HDOP_WINDOW_SUMMARY invalido seq=%u\n",
                  id, unsigned(packet.header.sequence));
    return;
  }
  HdopCollarStore* store = acquireHdopCollarStore(packet.header.collarId, receivedAtMs);
  if (!store) {
    ++hdopRejectedNoSlot;
    gatewayTerminal.print(framedSerial, "[AVISO] [GATEWAY] [COL %03u] HDOP 60s sessao=%016llx janela=%016llx recusado: sem slot\n",
                  id, static_cast<unsigned long long>(summary.sessionId),
                  static_cast<unsigned long long>(summary.windowId));
    return;
  }
  HdopSessionHistory* history = findHdopSession(*store, summary.sessionId);
  if (!isHdopWindowUnseen(*store, history, summary)) {
    ++hdopRejectedDuplicateOrStale;
    if (GATEWAY_TERMINAL_DETAIL)
      gatewayTerminal.print(framedSerial, "[DIAG] [GATEWAY] [COL %03u] HDOP 60s sessao=%016llx janela=%016llx duplicado ou antigo\n",
                    id, static_cast<unsigned long long>(summary.sessionId),
                    static_cast<unsigned long long>(summary.windowId));
    return;
  }
  const auto older = hdopRejectedOlder;
  const auto dropped = hdopDroppedPending;
  if (!retainHdopSummary(*store, summary, receivedAtMs)) {
    gatewayTerminal.print(framedSerial, "[AVISO] [GATEWAY] [COL %03u] HDOP 60s sessao=%016llx janela=%016llx recusado: %s\n",
                  id, static_cast<unsigned long long>(summary.sessionId),
                  static_cast<unsigned long long>(summary.windowId),
                  older != hdopRejectedOlder ? "janela antiga" : "fila cheia de outra sessao");
    return;
  }
  markHdopWindowSeen(*store, history, summary);
  char mean[12] = "N/A";
  if (summary.hdopMeanValid) snprintf(mean, sizeof(mean), "%.2f", summary.hdopMeanX100 / 100.0);
  const bool expanded = framedSerial.availableForWrite() >= 240;
  gatewayTerminal.print(framedSerial,
                expanded ? "==== VFENCE | GATEWAY | HDOP 60s | COL %03u ====\n"
                           " sessao=%016llx janela=%016llx inicioColeira=%lu ms duracao=%u ms\n"
                           " amostras=%u HDOP=%s validade=%s | RX LoRa retido%s\n"
                         : "[HDOP] [COL %03u] s=%016llx j=%016llx t=%lu d=%u n=%u HDOP=%s %s RETIDA%s\n",
                id, static_cast<unsigned long long>(summary.sessionId),
                static_cast<unsigned long long>(summary.windowId),
                static_cast<unsigned long>(summary.startedAtMs), unsigned(summary.durationMs),
                unsigned(summary.hdopSampleCount), mean,
                summary.hdopMeanValid ? (expanded ? "VALIDA" : "V") : (expanded ? "INVALIDA" : "INV"),
                dropped != hdopDroppedPending ? (expanded ? " | janela anterior sobrescrita" : "/SUBST") : "");
}

void processTelemetry(const PacketView& packet, int rssi, float snr,
                      bool hasRadioMetrics) {
  if (packet.payloadLength < 1) return;

  if (packet.payload[0] != static_cast<uint8_t>(TelemetrySubtype::POSITION_STATUS)) {
    gatewayOutput.print("[TELEMETRY] subtype desconhecido 0x");
    gatewayOutput.println(packet.payload[0], HEX);
    return;
  }

  TelemetryPayload d;
  if (!decodeTelemetry(packet, d)) {
    gatewayOutput.println("[TELEMETRY] payload POSITION_STATUS invalido");
    return;
  }

  CollarState& c = collars[packet.header.collarId];
  if (hasRadioMetrics && !packet.header.retry) {
    accumulateRadioSample(c.radio, packet.header.sequence, rssi, snr);
  }
  // A sequence e global por coleira e tambem e consumida por EVENT/CONTROL.
  // Portanto, saltos entre duas TELEMETRY nao significam necessariamente perda.
  c.lastTelemetrySequence = packet.header.sequence;
  c.haveTelemetrySequence = true;

  gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
  gatewayOutput.print("\",\"kind\":\"telemetry\",\"collarId\":");
  gatewayOutput.print(packet.header.collarId);
  gatewayOutput.print(",\"sequence\":"); gatewayOutput.print(packet.header.sequence); gatewayOutput.print(',');
  appPrintCoordinate("latitude", d.position.lat);
  appPrintCoordinate("longitude", d.position.lon);
  gatewayOutput.print("\"distanceBorderM\":");
  if (d.distanceBorderM == U16_UNAVAILABLE) gatewayOutput.print("null");
  else gatewayOutput.print(d.distanceBorderM);
  gatewayOutput.print(',');
  appPrintBattery(d.batteryPct);
  gatewayOutput.print("\"satellites\":");
  if (d.satellites == U8_UNAVAILABLE) gatewayOutput.print("null");
  else gatewayOutput.print(d.satellites);
  gatewayOutput.print(',');
  appPrintHdop(d.hdopX100);
  gatewayOutput.print("\"inside\":"); gatewayOutput.print((d.stateFlags & STATE_INSIDE) ? "true" : "false");
  gatewayOutput.print(",\"near\":"); gatewayOutput.print((d.stateFlags & STATE_NEAR) ? "true" : "false");
  gatewayOutput.print(",\"veryNear\":"); gatewayOutput.print((d.stateFlags & STATE_VERY_NEAR) ? "true" : "false");
  gatewayOutput.print(",\"gpsValid\":"); gatewayOutput.print((d.stateFlags & STATE_GPS_VALID) ? "true" : "false");
  gatewayOutput.print(",\"fenceVersion\":"); gatewayOutput.print(d.fenceVersion);
  if (hasRadioMetrics) {
    gatewayOutput.print(",\"rssi\":"); gatewayOutput.print(rssi);
    gatewayOutput.print(",\"snr\":"); gatewayOutput.print(snr, 2);
  }
  gatewayOutput.println("}");
  printPositionStatus(packet, d, hasRadioMetrics, rssi, snr);
  if (GATEWAY_TERMINAL_DETAIL) {
    char lat[24], lon[24], border[12] = "N/A", battery[8] = "N/A", sats[8] = "N/A";
    terminalCoordinate(lat, d.position.lat);
    terminalCoordinate(lon, d.position.lon);
    if (d.distanceBorderM != U16_UNAVAILABLE) snprintf(border, sizeof(border), "%um", unsigned(d.distanceBorderM));
    if (d.batteryPct != U8_UNAVAILABLE) snprintf(battery, sizeof(battery), "%u%%", unsigned(d.batteryPct));
    if (d.satellites != U8_UNAVAILABLE) snprintf(sats, sizeof(sats), "%u", unsigned(d.satellites));
    gatewayTerminal.print(framedSerial,
        "[DIAG] [GATEWAY] [COL %03u] seq=%u lat=%s lon=%s borda=%s bateria=%s sat=%s"
        " cerca=v%u flags=0x%02X buzzer=%s atuador2=%s\n",
        unsigned(packet.header.collarId), unsigned(packet.header.sequence), lat, lon, border, battery, sats,
        unsigned(d.fenceVersion), unsigned(d.stateFlags), (d.stateFlags & STATE_BUZZER_ACTIVE) ? "ON" : "OFF",
        (d.stateFlags & STATE_SECOND_ACTUATOR_ACTIVE) ? "ON" : "OFF");
  }
}

// ============================================================
// DECODIFICACAO EVENT
// ============================================================

bool processEventPayload(const PacketView& packet, uint16_t& fenceVersion, bool& hasFenceVersion) {
  const uint8_t subtype = packet.payload[0];

  if (subtype == static_cast<uint8_t>(EventSubtype::OUTSIDE) ||
      subtype == static_cast<uint8_t>(EventSubtype::BACK_INSIDE)) {
    PositionEventPayload d;
    if (!decodePositionEvent(packet, d)) return false;

    fenceVersion = d.fenceVersion;
    hasFenceVersion = true;
    if (GATEWAY_TERMINAL_DETAIL) {
      char lat[24], lon[24], battery[8] = "N/A", hdop[12] = "N/A";
      terminalCoordinate(lat, d.position.lat);
      terminalCoordinate(lon, d.position.lon);
      if (d.batteryPct != U8_UNAVAILABLE) snprintf(battery, sizeof(battery), "%u%%", unsigned(d.batteryPct));
      if (d.hdopX100 != U16_UNAVAILABLE) snprintf(hdop, sizeof(hdop), "%.2f", d.hdopX100 / 100.0);
      gatewayTerminal.print(framedSerial, "[DIAG] [COL %03u] %s lat=%s lon=%s borda=%um bat=%s sat=%u HDOP=%s\n",
          unsigned(packet.header.collarId), eventName(subtype), lat, lon, unsigned(d.distanceBorderM),
          battery, unsigned(d.satellites), hdop);
    }

    gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
    gatewayOutput.print("\",\"kind\":\"event\",\"subtype\":\"");
    gatewayOutput.print(eventName(subtype));
    gatewayOutput.print("\",\"collarId\":"); gatewayOutput.print(packet.header.collarId);
    gatewayOutput.print(",\"sequence\":"); gatewayOutput.print(packet.header.sequence); gatewayOutput.print(',');
    appPrintCoordinate("latitude", d.position.lat);
    appPrintCoordinate("longitude", d.position.lon);
    gatewayOutput.print("\"distanceBorderM\":"); gatewayOutput.print(d.distanceBorderM);
    gatewayOutput.print(",\"fenceVersion\":"); gatewayOutput.print(d.fenceVersion);
    gatewayOutput.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::GPS_LOST)) {
    GpsLostPayload d;
    if (!decodeGpsLost(packet, d)) return false;
    if (GATEWAY_TERMINAL_DETAIL) {
      char lat[24], lon[24], age[12] = "N/A";
      terminalCoordinate(lat, d.lastPosition.lat);
      terminalCoordinate(lon, d.lastPosition.lon);
      if (d.positionAgeS != U16_UNAVAILABLE) snprintf(age, sizeof(age), "%us", unsigned(d.positionAgeS));
      gatewayTerminal.print(framedSerial, "[DIAG] [COL %03u] GPS_LOST ultimaLat=%s ultimaLon=%s idade=%s\n",
          unsigned(packet.header.collarId), lat, lon, age);
    }

    gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
    gatewayOutput.print("\",\"kind\":\"event\",\"subtype\":\"GPS_LOST\",\"collarId\":");
    gatewayOutput.print(packet.header.collarId); gatewayOutput.print(',');
    appPrintCoordinate("lastLatitude", d.lastPosition.lat);
    appPrintCoordinate("lastLongitude", d.lastPosition.lon);
    gatewayOutput.print("\"positionAgeS\":");
    if (d.positionAgeS == U16_UNAVAILABLE) gatewayOutput.print("null");
    else gatewayOutput.print(d.positionAgeS);
    gatewayOutput.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::GPS_RECOVERED)) {
    GpsRecoveredPayload d;
    if (!decodeGpsRecovered(packet, d)) return false;
    if (GATEWAY_TERMINAL_DETAIL) {
      char lat[24], lon[24], hdop[12] = "N/A";
      terminalCoordinate(lat, d.position.lat);
      terminalCoordinate(lon, d.position.lon);
      if (d.hdopX100 != U16_UNAVAILABLE) snprintf(hdop, sizeof(hdop), "%.2f", d.hdopX100 / 100.0);
      gatewayTerminal.print(framedSerial, "[DIAG] [COL %03u] GPS_RECOVERED lat=%s lon=%s sat=%u HDOP=%s\n",
          unsigned(packet.header.collarId), lat, lon, unsigned(d.satellites), hdop);
    }

    gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
    gatewayOutput.print("\",\"kind\":\"event\",\"subtype\":\"GPS_RECOVERED\",\"collarId\":");
    gatewayOutput.print(packet.header.collarId); gatewayOutput.print(',');
    appPrintCoordinate("latitude", d.position.lat);
    appPrintCoordinate("longitude", d.position.lon);
    gatewayOutput.println("\"recovered\":true}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::LOW_BATTERY) ||
      subtype == static_cast<uint8_t>(EventSubtype::CRITICAL_BATTERY)) {
    BatteryEventPayload d;
    if (!decodeBatteryEvent(packet, d)) return false;
    if (GATEWAY_TERMINAL_DETAIL) {
      char lat[24], lon[24], battery[8] = "N/A";
      terminalCoordinate(lat, d.lastPosition.lat);
      terminalCoordinate(lon, d.lastPosition.lon);
      if (d.batteryPct != U8_UNAVAILABLE) snprintf(battery, sizeof(battery), "%u%%", unsigned(d.batteryPct));
      gatewayTerminal.print(framedSerial, "[DIAG] [COL %03u] %s bateria=%s ultimaLat=%s ultimaLon=%s idade=%us\n",
          unsigned(packet.header.collarId), eventName(subtype), battery, lat, lon, unsigned(d.positionAgeS));
    }

    gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
    gatewayOutput.print("\",\"kind\":\"event\",\"subtype\":\"");
    gatewayOutput.print(eventName(subtype));
    gatewayOutput.print("\",\"collarId\":"); gatewayOutput.print(packet.header.collarId); gatewayOutput.print(',');
    appPrintBattery(d.batteryPct);
    appPrintCoordinate("lastLatitude", d.lastPosition.lat);
    appPrintCoordinate("lastLongitude", d.lastPosition.lon);
    gatewayOutput.print("\"positionAgeS\":"); gatewayOutput.print(d.positionAgeS);
    gatewayOutput.println("}");
    return true;
  }

  if (subtype == static_cast<uint8_t>(EventSubtype::DEVICE_STARTED)) {
    DeviceStartedPayload d;
    if (!decodeDeviceStarted(packet, d)) return false;
    fenceVersion = d.activeFenceVersion;
    hasFenceVersion = true;
    if (GATEWAY_TERMINAL_DETAIL) {
      char battery[8] = "N/A";
      if (d.batteryPct != U8_UNAVAILABLE) snprintf(battery, sizeof(battery), "%u%%", unsigned(d.batteryPct));
      gatewayTerminal.print(framedSerial, "[DIAG] [COL %03u] DEVICE_STARTED reset=0x%02X bateria=%s\n",
          unsigned(packet.header.collarId), unsigned(d.resetReason), battery);
    }

    gatewayOutput.print("[APP] {\"schema\":\""); gatewayOutput.print(SERIAL_SCHEMA);
    gatewayOutput.print("\",\"kind\":\"event\",\"subtype\":\"DEVICE_STARTED\",\"collarId\":");
    gatewayOutput.print(packet.header.collarId);
    gatewayOutput.print(",\"resetReason\":"); gatewayOutput.print(static_cast<uint8_t>(d.resetReason));
    gatewayOutput.print(",\"activeFenceVersion\":"); gatewayOutput.print(d.activeFenceVersion); gatewayOutput.print(',');
    appPrintBattery(d.batteryPct, false);
    gatewayOutput.println("}");
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
    const bool sent = sendAckResponse(packet.header.collarId, packet.header.sequence);
    gatewayTerminal.print(framedSerial, "[%s] [GATEWAY] [COL %03u] evento duplicado seq=%u ACK-LoRa=%s\n",
                  sent ? "AVISO" : "ERRO", unsigned(packet.header.collarId),
                  unsigned(packet.header.sequence), sent ? "REENVIADO LOCAL" : "FALHA LOCAL");
    return;
  }

  uint16_t fenceVersion = 0;
  bool hasFenceVersion = false;
  if (!processEventPayload(packet, fenceVersion, hasFenceVersion)) {
    if (packet.header.ackReq) {
      // Se o subtype existe mas o tamanho nao bate, e INVALID_LENGTH.
      const bool known = subtype >= static_cast<uint8_t>(EventSubtype::OUTSIDE) &&
                         subtype <= static_cast<uint8_t>(EventSubtype::DEVICE_STARTED);
      sendNackResponse(packet.header.collarId, packet.header.sequence,
                       known ? ErrorCode::INVALID_LENGTH : ErrorCode::UNKNOWN_SUBTYPE);
    }
    gatewayTerminal.print(framedSerial, "[ERRO] [GATEWAY] [COL %03u] evento invalido seq=%u subtipo=0x%02X\n",
                  unsigned(packet.header.collarId), unsigned(packet.header.sequence), unsigned(subtype));
    return;
  }

  const char* ackStatus = "N/A";
  if (packet.header.ackReq) {
    rememberReliable(packet.header.collarId, packet.header.sequence);
    ackStatus = sendAckResponse(packet.header.collarId, packet.header.sequence)
        ? "TX LOCAL ACEITO" : "FALHA LOCAL";
  }
  char fence[16] = "N/A";
  if (hasFenceVersion) snprintf(fence, sizeof(fence), "v%u", unsigned(fenceVersion));
  const bool alert = subtype == static_cast<uint8_t>(EventSubtype::OUTSIDE) ||
                     subtype == static_cast<uint8_t>(EventSubtype::GPS_LOST) ||
                     subtype == static_cast<uint8_t>(EventSubtype::LOW_BATTERY) ||
                     subtype == static_cast<uint8_t>(EventSubtype::CRITICAL_BATTERY);
  const char* rule = "";
  if (framedSerial.availableForWrite() >= 200)
    rule = alert ? "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n" : "----------------------------------------\n";
  gatewayTerminal.print(framedSerial, "%s[%s] [GATEWAY] [COL %03u] %s seq=%u cerca=%s ACK-LoRa=%s\n%s",
                rule, alert ? "ALERTA" : "EVENTO", unsigned(packet.header.collarId),
                eventName(subtype), unsigned(packet.header.sequence), fence, ackStatus, rule);
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
      gatewayOutput.print("[PING] collar="); gatewayOutput.print(packet.header.collarId);
      gatewayOutput.print(" token="); gatewayOutput.println(token);
      sendPong(packet.header.collarId, token);
    }
    return;
  }

  if (subtype == static_cast<uint8_t>(ControlSubtype::PONG)) {
    uint16_t token;
    if (decodePingPong(packet, token)) {
      gatewayOutput.print("[PONG] collar="); gatewayOutput.print(packet.header.collarId);
      gatewayOutput.print(" token="); gatewayOutput.println(token);
      emitAppHeader("control");
      gatewayOutput.print(",\"subtype\":\"PONG\",\"collarId\":");
      gatewayOutput.print(packet.header.collarId);
      gatewayOutput.print(",\"token\":"); gatewayOutput.print(token);
      gatewayOutput.println('}');
    }
    return;
  }

  gatewayOutput.print("[CONTROL] subtype desconhecido 0x");
  gatewayOutput.println(subtype, HEX);
}

// ============================================================
// PROCESSAMENTO CENTRAL DE RX
// ============================================================

void processIncomingPacket(const uint8_t* data, size_t len,
                           int rssi = 0, float snr = 0.0f,
                           bool hasRadioMetrics = false, uint32_t receivedAtMs = 0) {
  PacketView packet;
  if (!decodePacket(data, len, packet)) {
    gatewayTerminal.print(framedSerial, "[RX] pacote invalido: header/tamanho\n");
    return;
  }

  const uint8_t collarId = packet.header.collarId;
  if (!validCollarId(collarId)) {
    gatewayTerminal.print(framedSerial, "[RX] collarId invalido para uplink: 0x%X\n", unsigned(collarId));
    return;
  }

  if (packet.header.type == MessageType::TELEMETRY && packet.payloadLength > 0 &&
      packet.payload[0] == static_cast<uint8_t>(TelemetrySubtype::HDOP_WINDOW_SUMMARY)) {
    if (hasRadioMetrics) processHdopSummary(packet, receivedAtMs);
    return;
  }

  markCollarSeen(collarId, rssi, snr, hasRadioMetrics);

  if (packet.header.version != PROTOCOL_VERSION) {
    gatewayTerminal.print(framedSerial, "[RX] versao nao suportada: %u\n", unsigned(packet.header.version));
    if (packet.header.ackReq) {
      sendNackResponse(collarId, packet.header.sequence,
                       ErrorCode::UNSUPPORTED_VERSION);
    }
    return;
  }

  switch (packet.header.type) {
    case MessageType::TELEMETRY: {
      if (packet.payloadLength && packet.payload[0] == POSITION_STATUS_COUNTED) {
        PositionDeliveryTag tag;
        uint8_t payload[LEGACY_POSITION_PAYLOAD_SIZE];
        PacketView legacy;
        if (!unpackCountedPosition(packet, tag, legacy, payload)) return;
        if (hasRadioMetrics) pdrSlots[collarId].tracker.observe(tag, receivedAtMs, INITIAL_LORA_PROFILE);
        processTelemetry(legacy, rssi, snr, hasRadioMetrics);
      } else {
        processTelemetry(packet, rssi, snr, hasRadioMetrics);
      }
      break;
    }

    case MessageType::EVENT:
      if (hasRadioMetrics && !packet.header.retry) {
        DeviceStartedPayload started;
        if (decodeDeviceStarted(packet, started)) {
          RadioWindowStats& radio = collars[collarId].radio;
          radio.recentSequenceCount = 0;
          radio.nextRecentSequence = 0;
        }
      }
      processEvent(packet);
      break;

    case MessageType::CONTROL:
      processControl(packet);
      break;

    case MessageType::CONFIG:
      gatewayTerminal.print(framedSerial, "[RX] CONFIG vindo da coleira nao e esperado na v1\n");
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
  const uint32_t receivedAtMs = millis();

  if (oversized) {
    gatewayTerminal.print(framedSerial, "[RX LoRa] pacote maior que MAX_PACKET_SIZE: %d\n", packetSize);
    return;
  }

  // Atribuir pelo instante da leitura do radio, antes dos logs e do primeiro contato.
  serviceRadioWindows(receivedAtMs);

  // Resumos novos so tem diagnostico opt-in. Logs dos pacotes anteriores seguem iguais.
  const bool hdopSummary = count > HEADER_SIZE &&
      ((data[0] >> 4) & 0x03) == static_cast<uint8_t>(MessageType::TELEMETRY) &&
      data[HEADER_SIZE] == static_cast<uint8_t>(TelemetrySubtype::HDOP_WINDOW_SUMMARY);
  if (hdopSummary) {
    processIncomingPacket(data, count, rssi, snr, true, receivedAtMs);
    return;
  }

  if (GATEWAY_TERMINAL_DETAIL) {
    char hex[MAX_PACKET_SIZE * 3];
    terminalHex(data, count, hex);
    gatewayTerminal.print(framedSerial, "[RX LoRa] [GATEWAY] bytes=%u RSSI=%ddBm SNR=%.2fdB\n%s\n",
                          unsigned(count), rssi, static_cast<double>(snr), hex);
  }

  processIncomingPacket(data, count, rssi, snr, true, receivedAtMs);
}

// ============================================================
// COMANDOS SERIAL PARA TESTE DO GATEWAY
// ============================================================

void printHelp() {
  gatewayOutput.println("Comandos:");
  gatewayOutput.println("  HELP");
  gatewayOutput.println("  STATUS");
  gatewayOutput.println("  RADIO");
  gatewayOutput.println("  PING <collarId> [token]");
  gatewayOutput.println("  PARAM <id> <muitoPertoM> <pertoM> <telemetryS> <gpsTimeoutS> <lowBat> <critBat>");
  gatewayOutput.println("  FENCE_NEW <id> <version> <vertexCount>");
  gatewayOutput.println("  FENCE_POINT <index> <latitude> <longitude>");
  gatewayOutput.println("  FENCE_SHOW");
  gatewayOutput.println("  FENCE_SEND");
  gatewayOutput.println("  FENCE_CANCEL");
  gatewayOutput.println("  RX <bytes hex...>   (injeta pacote sem radio para teste)");
}

void printRadioConfig() {
  gatewayOutput.println("--- RADIO CONFIG ---");
  gatewayOutput.print("Frequency: "); gatewayOutput.print(LORA_FREQUENCY_HZ); gatewayOutput.println(" Hz");
  gatewayOutput.print("SF: "); gatewayOutput.println(LORA_SPREADING_FACTOR);
  gatewayOutput.print("BW: "); gatewayOutput.print(LORA_SIGNAL_BANDWIDTH); gatewayOutput.println(" Hz");
  gatewayOutput.print("CR: 4/"); gatewayOutput.println(LORA_CODING_RATE_DENOMINATOR);
  gatewayOutput.print("Preamble: "); gatewayOutput.println(LORA_PREAMBLE_LENGTH);
  gatewayOutput.print("SyncWord: 0x"); gatewayOutput.println(LORA_SYNC_WORD, HEX);
  gatewayOutput.print("TX power: "); gatewayOutput.print(LORA_TX_POWER_DBM); gatewayOutput.println(" dBm");
  gatewayOutput.println("CRC: ON");
}

void printStatus() {
  gatewayTerminal.print(framedSerial, "--- COLLAR STATUS ---\n");
  bool any = false;
  const uint32_t now = millis();
  for (uint16_t id = 1; id <= 0xFE; ++id) {
    const CollarState& c = collars[id];
    if (!c.seen) continue;
    any = true;
    char rssi[20] = "N/A", snr[20] = "N/A";
    if (c.terminalHasRadioMetrics) {
      snprintf(rssi, sizeof(rssi), "%ddBm", c.lastRssi);
      snprintf(snr, sizeof(snr), "%.2fdB", static_cast<double>(c.lastSnr));
    }
    gatewayTerminal.print(framedSerial,
        "[ESTADO] [GATEWAY] [COL %03u] %s ultimoContato=%lu s ultimoRSSI=%s ultimoSNR=%s\n",
        unsigned(id), c.online ? "ONLINE" : "OFFLINE",
        static_cast<unsigned long>((now - c.lastSeenMs) / 1000UL), rssi, snr);
  }
  if (!any) gatewayTerminal.print(framedSerial, "Nenhuma coleira vista ainda.\n");
}

void emitRadioConfig() {
  emitAppHeader("radio_config", true);
  gatewayOutput.print(",\"frequencyHz\":"); gatewayOutput.print(LORA_FREQUENCY_HZ);
  gatewayOutput.print(",\"spreadingFactor\":"); gatewayOutput.print(LORA_SPREADING_FACTOR);
  gatewayOutput.print(",\"bandwidthHz\":"); gatewayOutput.print(LORA_SIGNAL_BANDWIDTH);
  gatewayOutput.print(",\"codingRateDenominator\":"); gatewayOutput.print(LORA_CODING_RATE_DENOMINATOR);
  gatewayOutput.print(",\"preambleLength\":"); gatewayOutput.print(LORA_PREAMBLE_LENGTH);
  gatewayOutput.print(",\"syncWord\":"); gatewayOutput.print(LORA_SYNC_WORD);
  gatewayOutput.print(",\"txPowerDbm\":"); gatewayOutput.print(LORA_TX_POWER_DBM);
  gatewayOutput.println(",\"crcEnabled\":true}");
}

static uint16_t nextStatusCollar = 0;
static bool statusHadCollar = false;

static void serviceCollarStatus() {
  if (!nextStatusCollar) return;
  const uint32_t now = millis();
  // Leave space for an asynchronous fence result and bound the work per call.
  for (uint8_t emitted = 0; emitted < 8 && framedSerial.canQueue(2);) {
    if (nextStatusCollar > 254) {
      if (!statusHadCollar) {
        emitAppHeader("collar_status", true);
        gatewayOutput.println(",\"empty\":true}");
      }
      nextStatusCollar = 0;
      return;
    }
    const uint16_t id = nextStatusCollar++;
    const CollarState& c = collars[id];
    if (!c.seen) continue;
    statusHadCollar = true;
    ++emitted;
    emitAppHeader("collar_status", true);
    gatewayOutput.print(",\"collarId\":"); gatewayOutput.print(id);
    gatewayOutput.print(",\"state\":\""); gatewayOutput.print(c.online ? "ONLINE" : "OFFLINE");
    gatewayOutput.print("\",\"lastSeenS\":"); gatewayOutput.print((now - c.lastSeenMs) / 1000UL);
    gatewayOutput.print(",\"rssi\":"); gatewayOutput.print(c.lastRssi);
    gatewayOutput.print(",\"snr\":"); gatewayOutput.print(c.lastSnr, 2);
    gatewayOutput.println('}');
  }
}

void emitGatewayAndCollarStatus() {
  emitAppHeader("gateway_state", true);
  gatewayOutput.print(",\"state\":\"ONLINE\",\"fenceTransferActive\":");
  gatewayOutput.print(fenceTx.active ? "true" : "false");
  gatewayOutput.println('}');

  statusHadCollar = false;
  nextStatusCollar = 1;
  serviceCollarStatus();
}

bool sendPingCommand(uint8_t collarId, uint16_t token) {
  if (!validCollarId(collarId)) {
    gatewayOutput.println("[ERRO] [GATEWAY] PING collarId invalido");
    return false;
  }

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodePingPong(packet, sizeof(packet), collarId,
                                    allocateSequence(), ControlSubtype::PING,
                                    token);
  return len && transportSend(packet, len);
}

bool sendParametersCommand(uint8_t collarId, const ParametersPayload& p) {
  if (!validCollarId(collarId)) {
    gatewayOutput.println("[ERRO] [GATEWAY] PARAM collarId invalido");
    return false;
  }

  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeParameters(packet, sizeof(packet), collarId,
                                      allocateSequence(), false, p);
  return len && sendReliable(packet, len);
}

bool stageFenceNew(uint8_t collarId, uint16_t version, uint8_t vertexCount) {
  if (!validCollarId(collarId)) {
    gatewayOutput.println("[FENCE] collarId invalido");
    return false;
  }
  if (vertexCount < 3 || vertexCount > MAX_VERTICES) {
    gatewayOutput.println("[FENCE] vertexCount deve estar entre 3 e 32");
    return false;
  }
  if (fenceTx.active) {
    gatewayOutput.println("[FENCE] cancele ou aguarde a transferencia atual");
    return false;
  }

  memset(&fenceTx, 0, sizeof(fenceTx));
  fenceTx.staged = true;
  fenceTx.collarId = collarId;
  fenceTx.version = version;
  fenceTx.vertexCount = vertexCount;
  fenceTx.phase = FenceTxPhase::STAGED;

  gatewayTerminal.print(framedSerial, "[CERCA] [GATEWAY] [COL %03u] v%u preparada vertices=%u\n",
                unsigned(collarId), unsigned(version), unsigned(vertexCount));
  return true;
}

bool stageFencePoint(uint8_t index, double lat, double lon) {
  if (!fenceTx.staged) {
    gatewayOutput.println("[FENCE] use FENCE_NEW primeiro");
    return false;
  }
  if (fenceTx.active) {
    gatewayOutput.println("[FENCE] nao altere pontos durante uma transferencia");
    return false;
  }
  if (index >= fenceTx.vertexCount) {
    gatewayOutput.println("[FENCE] indice fora da faixa");
    return false;
  }
  if (lat < -90.0 || lat > 90.0 || lon < -180.0 || lon > 180.0) {
    gatewayOutput.println("[FENCE] latitude/longitude invalidas");
    return false;
  }

  fenceTx.points[index].lat = degreesToE7(lat);
  fenceTx.points[index].lon = degreesToE7(lon);
  fenceTx.pointMask |= (1UL << index);

  if (GATEWAY_TERMINAL_DETAIL)
    gatewayTerminal.print(framedSerial, "[DIAG] [GATEWAY] [COL %03u] CERCA v%u ponto=%u lat=%.7f lon=%.7f\n",
                  unsigned(fenceTx.collarId), unsigned(fenceTx.version), unsigned(index), lat, lon);
  return true;
}

void showStagedFence() {
  if (!fenceTx.staged) {
    gatewayOutput.println("[FENCE] nenhuma cerca preparada");
    return;
  }

  gatewayOutput.println("--- STAGED FENCE ---");
  gatewayOutput.print("Collar: "); gatewayOutput.println(fenceTx.collarId);
  gatewayOutput.print("Version: "); gatewayOutput.println(fenceTx.version);
  gatewayOutput.print("Vertex count: "); gatewayOutput.println(fenceTx.vertexCount);
  gatewayOutput.print("Transfer active: "); gatewayOutput.println(fenceTx.active ? "SIM" : "NAO");
  for (uint8_t i = 0; i < fenceTx.vertexCount; ++i) {
    gatewayOutput.print(i); gatewayOutput.print(": ");
    if ((fenceTx.pointMask & (1UL << i)) == 0) {
      gatewayOutput.println("<NAO DEFINIDO>");
    } else {
      gatewayOutput.print(e7ToDegrees(fenceTx.points[i].lat), 7);
      gatewayOutput.print(", ");
      gatewayOutput.println(e7ToDegrees(fenceTx.points[i].lon), 7);
    }
  }

  if ((fenceTx.pointMask & requiredPointMask(fenceTx.vertexCount)) ==
      requiredPointMask(fenceTx.vertexCount)) {
    const uint32_t crc = computeFenceCrc32(fenceTx.version,
                                           fenceTx.points,
                                           fenceTx.vertexCount);
    gatewayOutput.print("CRC32: 0x"); gatewayOutput.println(crc, HEX);
  } else {
    gatewayOutput.println("CRC32: indisponivel (faltam pontos)");
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
      gatewayOutput.println("[RX SERIAL] token hexadecimal invalido");
      return false;
    }

    data[count++] = static_cast<uint8_t>(value);
    start = end + 1;
  }

  gatewayOutput.print("[RX SERIAL "); gatewayOutput.print(count); gatewayOutput.print(" bytes] ");
  printHexPacket(data, count); gatewayOutput.println();
  processIncomingPacket(data, count, 0, 0.0f, false);
  return true;
}

const char* stagedFenceProblem() {
  if (!fenceTx.staged) return "fence_not_staged";
  if (fenceTx.active) return "BUSY";
  if ((fenceTx.pointMask & requiredPointMask(fenceTx.vertexCount)) !=
      requiredPointMask(fenceTx.vertexCount)) return "MISSING_CHUNKS";
  return nullptr;
}

static void emitProfileResponse(uint32_t requestId, const String& command, bool ok, const char* detail) {
  emitAppHeader("command_response", true);
  gatewayOutput.print(",\"requestId\":"); gatewayOutput.print(requestId);
  gatewayOutput.print(",\"command\":\""); gatewayOutput.print(command.c_str());
  gatewayOutput.print("\",\"ok\":"); gatewayOutput.print(ok ? "true" : "false");
  gatewayOutput.print(",\"detail\":\""); gatewayOutput.print(detail);
  gatewayOutput.printf("\",\"profileControlVersion\":1,\"gatewaySessionId\":\"%016llx\","
                "\"activeProfileId\":%u,\"operationId\":%lu,\"state\":\"%s\","
                "\"switchEnabled\":false,\"requestedProfileId\":",
                static_cast<unsigned long long>(radioSessionId), unsigned(INITIAL_LORA_PROFILE),
                static_cast<unsigned long>(profilePlan.operation()), profilePlan.pending() ? "PLANNED" : "IDLE");
  if (profilePlan.pending()) gatewayOutput.print(profilePlan.target());
  else gatewayOutput.print("null");
  if (command == "LIST_PROFILES") {
    gatewayOutput.print(",\"profiles\":[");
    for (const auto& p : LORA_PROFILES) {
      if (p.id != INITIAL_LORA_PROFILE) gatewayOutput.print(',');
      gatewayOutput.printf("{\"id\":%u,\"frequencyHz\":%lu,\"bandwidthHz\":%lu,\"sf\":%u,"
                    "\"crDenominator\":%u,\"txDbm\":%u,\"preambleSymbols\":%u,\"syncWord\":%u,"
                    "\"crc\":%s,\"explicitHeader\":%s,\"invertedIq\":%s,\"candidate\":%s}",
                    unsigned(p.id), static_cast<unsigned long>(p.frequencyHz),
                    static_cast<unsigned long>(p.bandwidthHz), unsigned(p.spreadingFactor),
                    unsigned(p.codingRateDenominator), unsigned(p.txPowerDbm), unsigned(p.preambleSymbols),
                    unsigned(p.syncWord), p.crcEnabled ? "true" : "false", p.explicitHeader ? "true" : "false",
                    p.invertedIq ? "true" : "false", p.id == INITIAL_LORA_PROFILE ? "false" : "true");
    }
    gatewayOutput.print(']');
  }
  gatewayOutput.println('}');
}

static bool processProfileCommand(const GatewayCommandJson& json, const String& command, uint32_t requestId) {
  if (command == "GET_PROFILE" || command == "LIST_PROFILES" || command == "GET_PROFILE_TRANSITION") {
    emitProfileResponse(requestId, command, true, "profile_status");
    return true;
  }
  if (command != "REQUEST_PROFILE" && command != "CANCEL_PROFILE") return false;
  const char* sessionField = nullptr;
  size_t sessionLength = 0;
  uint64_t session;
  uint32_t operation, target;
  ProfileDecision decision = ProfileDecision::INVALID;
  if (json.stringField("gatewaySessionId", sessionField, sessionLength) && sessionLength == 16 &&
      readMetricId(sessionField, session) && json.unsignedField("operationId", operation, UINT32_MAX)) {
    if (command == "CANCEL_PROFILE") {
      decision = profilePlan.cancel(radioSessionId, session, operation);
    } else if (json.unsignedField("profileId", target, LORA_PROFILE_COUNT - 1)) {
      bool busy = fenceTx.active;
      for (const auto& tx : pendingTx) if (tx.active) busy = true;
      decision = profilePlan.request(radioSessionId, session, operation, static_cast<uint8_t>(target), busy);
    }
  }
  const bool ok = decision == ProfileDecision::PLANNED || decision == ProfileDecision::CANCELLED ||
                  decision == ProfileDecision::DUPLICATE || decision == ProfileDecision::UNCHANGED;
  emitProfileResponse(requestId, command, ok, profileDecisionName(decision));
  char requested[6] = "N/A";
  if (profilePlan.pending()) snprintf(requested, sizeof(requested), "P%u", unsigned(profilePlan.target()));
  gatewayTerminal.print(framedSerial, "[PERFIL] [GATEWAY] atual=P%u pedido=%s estado=%s resultado=%s operacao=%lu\n",
                unsigned(INITIAL_LORA_PROFILE), requested,
                profilePlan.pending() ? "PLANNED" : "IDLE", profileDecisionName(decision),
                static_cast<unsigned long>(profilePlan.operation()));
  return true;
}

void processJsonCommand(const String& line) {
  const GatewayCommandJson json(line.c_str() + 6);
  if (!json.isValid()) {
    emitCommandResponse(0, "UNKNOWN", false, "invalid_json");
    return;
  }
  uint32_t requestId = 0;
  const char* field = nullptr;
  size_t length = 0;

  if (!json.unsignedField("requestId", requestId, UINT32_MAX) ||
      requestId == 0) {
    emitCommandResponse(0, "UNKNOWN", false, "request_id_invalid");
    return;
  }
  if (!json.stringField("command", field, length) || length == 0) {
    emitCommandResponse(requestId, "UNKNOWN", false, "command_invalid");
    return;
  }
  const unsigned commandOffset = field - line.c_str();
  const String command = line.substring(commandOffset, commandOffset + length);
  if (command == "METRIC_COMMITTED") {
    MetricCommitAck ack;
    if (METRIC_SERIAL_EXPORT_ENABLED) {
      if (!parseMetricCommitAck(line.c_str(), ack)) {
        gatewayTerminal.print(framedSerial, "[AVISO] [GATEWAY] METRIC_COMMITTED invalido ignorado\n");
      } else if (!metricSerialExporter.confirm(millis(), ack, radioSessionId, metricSerialIo,
                                              printMetricSerialNotice)) {
        gatewayTerminal.print(framedSerial, "[AVISO] [GATEWAY] [COL %03u] METRIC_COMMITTED ignorado sessao=%016llx janela=%016llx\n",
                      unsigned(ack.id.collarId), static_cast<unsigned long long>(ack.id.sessionId),
                      static_cast<unsigned long long>(ack.id.windowId));
      }
    }
    return;
  }
  if (!json.stringField("schema", field, length) || length != strlen(SERIAL_SCHEMA) ||
      strncmp(field, SERIAL_SCHEMA, length) != 0) {
    emitCommandResponse(requestId, command.c_str(), false, "schema_invalid");
    return;
  }
  if (processProfileCommand(json, command, requestId)) return;

  if (command == "GET_STATUS") {
    emitCommandResponse(requestId, command.c_str(), true);
    emitGatewayAndCollarStatus();
    return;
  }
  if (command == "GET_RADIO") {
    emitCommandResponse(requestId, command.c_str(), true);
    emitRadioConfig();
    return;
  }

  uint32_t collar = 0;
  if (command == "PING") {
    uint32_t token = 0;
    if (!json.unsignedField("collarId", collar, 254) || collar < 1) {
      emitCommandResponse(requestId, command.c_str(), false, "collar_id_invalid");
      return;
    }
    const bool hasToken = json.field("token") != nullptr;
    if (!json.unsignedField("token", token, 0xFFFF, false)) {
      emitCommandResponse(requestId, command.c_str(), false, "token_invalid");
      return;
    }
    if (!hasToken) token = nextPingToken++;
    const bool ok = sendPingCommand(static_cast<uint8_t>(collar),
                                    static_cast<uint16_t>(token));
    emitCommandResponse(requestId, command.c_str(), ok,
                        ok ? nullptr : "radio_send_failed");
    return;
  }

  if (command == "SET_PARAMETERS") {
    uint32_t veryNear = 0, nearM = 0, telemetry = 0, gpsTimeout = 0;
    uint32_t lowBat = 0, criticalBat = 0;
    const bool valid =
        json.unsignedField("collarId", collar, 254) && collar >= 1 &&
        json.unsignedField("veryNearM", veryNear, 0xFFFF) &&
        json.unsignedField("nearM", nearM, 0xFFFF) &&
        json.unsignedField("telemetryIntervalS", telemetry, UINT32_MAX) &&
        json.unsignedField("gpsTimeoutS", gpsTimeout, 0xFFFF) &&
        json.unsignedField("lowBatteryPct", lowBat, 100) &&
        json.unsignedField("criticalBatteryPct", criticalBat, 100) &&
        veryNear <= nearM && criticalBat <= lowBat;
    if (!valid) {
      emitCommandResponse(requestId, command.c_str(), false, "parameters_invalid");
      return;
    }
    ParametersPayload p;
    p.muitoPertoM = static_cast<uint16_t>(veryNear);
    p.pertoM = static_cast<uint16_t>(nearM);
    p.telemetryIntervalS = telemetry;
    p.gpsTimeoutS = static_cast<uint16_t>(gpsTimeout);
    p.lowBatteryPct = static_cast<uint8_t>(lowBat);
    p.criticalBatteryPct = static_cast<uint8_t>(criticalBat);
    const bool ok = sendParametersCommand(static_cast<uint8_t>(collar), p);
    emitCommandResponse(requestId, command.c_str(), ok,
                        ok ? nullptr : "radio_queue_failed");
    return;
  }

  if (command == "FENCE_NEW") {
    uint32_t version = 0, vertexCount = 0;
    if (!json.unsignedField("collarId", collar, 254) || collar < 1) {
      emitCommandResponse(requestId, command.c_str(), false, "collar_id_invalid");
      return;
    }
    if (!json.unsignedField("fenceVersion", version, 0xFFFF) || version < 1) {
      emitCommandResponse(requestId, command.c_str(), false, "INVALID_FENCE_VERSION");
      return;
    }
    if (!json.unsignedField("vertexCount", vertexCount, MAX_VERTICES) ||
        vertexCount < 3) {
      emitCommandResponse(requestId, command.c_str(), false, "vertex_count_invalid");
      return;
    }
    if (fenceTx.active) {
      emitCommandResponse(requestId, command.c_str(), false, "BUSY");
      return;
    }
    const bool ok = stageFenceNew(static_cast<uint8_t>(collar),
                                  static_cast<uint16_t>(version),
                                  static_cast<uint8_t>(vertexCount));
    emitCommandResponse(requestId, command.c_str(), ok,
                        ok ? nullptr : "fence_stage_failed");
    return;
  }

  if (command == "FENCE_POINT") {
    uint32_t index = 0;
    double latitude = 0.0, longitude = 0.0;
    if (!json.unsignedField("index", index, MAX_VERTICES - 1) ||
        !json.doubleField("latitude", latitude) ||
        !json.doubleField("longitude", longitude) ||
        latitude < -90.0 || latitude > 90.0 ||
        longitude < -180.0 || longitude > 180.0) {
      emitCommandResponse(requestId, command.c_str(), false, "point_invalid");
      return;
    }
    if (!fenceTx.staged) {
      emitCommandResponse(requestId, command.c_str(), false, "fence_not_staged");
      return;
    }
    if (fenceTx.active) {
      emitCommandResponse(requestId, command.c_str(), false, "BUSY");
      return;
    }
    if (index >= fenceTx.vertexCount) {
      emitCommandResponse(requestId, command.c_str(), false, "point_index_invalid");
      return;
    }
    const bool ok = stageFencePoint(static_cast<uint8_t>(index), latitude, longitude);
    emitCommandResponse(requestId, command.c_str(), ok,
                        ok ? nullptr : "point_stage_failed");
    return;
  }

  if (command == "FENCE_SEND") {
    const char* problem = stagedFenceProblem();
    if (problem) {
      emitCommandResponse(requestId, command.c_str(), false, problem);
      return;
    }
    fenceTx.hasRequestId = true;
    fenceTx.requestId = requestId;
    const bool ok = startFenceTransfer();
    emitCommandResponse(requestId, command.c_str(), ok,
                        ok ? nullptr : "radio_queue_failed");
    if (ok) emitFenceTransfer("started");
    return;
  }

  if (command == "FENCE_CANCEL") {
    if (fenceTx.active) abortFenceTransfer("cancelada pela Base", "CANCELLED");
    else {
      memset(&fenceTx, 0, sizeof(fenceTx));
      fenceTx.phase = FenceTxPhase::IDLE;
    }
    emitCommandResponse(requestId, command.c_str(), true);
    return;
  }

  emitCommandResponse(requestId, command.c_str(), false, "command_unknown");
}

static void processSerialCommandLine(String line) {
  line.trim();
  if (line.length() == 0) return;

  if (line.length() > SERIAL_COMMAND_MAX_LENGTH) {
    gatewayOutput.println("[SERIAL] linha excede limite de 512 caracteres");
    return;
  }
  if (line.startsWith("[CMD] ")) {
    processJsonCommand(line);
    return;
  }

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
    else gatewayOutput.println("[FENCE] nenhuma transferencia ativa");
    return;
  }

  gatewayOutput.println("[ERRO] [GATEWAY] comando desconhecido. Digite HELP.");
}

void pollSerialCommands() {
  // A command can emit two reserved lines. Leave one more for the fence result.
  if (nextStatusCollar || !framedSerial.canQueue(3)) return;
  static SerialCommandInput input;
  for (size_t budget = 0; budget < 256 && Serial.available(); ++budget) {
    const int byte = Serial.read();
    if (byte < 0) return;
    const auto result = input.feed(static_cast<uint8_t>(byte));
    if (result == SerialCommandInput::Result::INCOMPLETE) continue;
    if (result == SerialCommandInput::Result::LINE) processSerialCommandLine(input.line());
    else if (result == SerialCommandInput::Result::TOO_LONG)
      gatewayOutput.println("[SERIAL] linha excede limite de 512 caracteres");
    return;
  }
}

// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  const bool buffered = Serial.setTxBufferSize(1024) >= 1024;
  metricSerialTxReady = METRIC_SERIAL_EXPORT_ENABLED && buffered;
  pdrTxReady = PDR_SERIAL_EXPORT_ENABLED && buffered;
  Serial.begin(115200);
  esp_fill_random(&radioSessionId, sizeof(radioSessionId));
  delay(1500);

  memset(collars, 0, sizeof(collars));
  memset(pendingTx, 0, sizeof(pendingTx));
  memset(&fenceTx, 0, sizeof(fenceTx));
  fenceTx.phase = FenceTxPhase::IDLE;
  randomSeed(static_cast<uint32_t>(micros()));

  gatewayOutput.println();
  gatewayOutput.println("========================================");
  gatewayOutput.println(" VFENCE | GATEWAY | INICIALIZACAO");
  gatewayOutput.println("========================================");

  if (!initLoRa()) {
    gatewayOutput.println("[FATAL] Falha ao iniciar LoRa.");
    gatewayOutput.println("Verifique placa, pinos, antena e frequencia.");
    while (true) delay(1000);
  }

  printRadioConfig();
  gatewayOutput.println("[INFO] USB Serial: 115200 baud");
  gatewayOutput.printf("[INFO] Exportacao de medias: %s\n", !METRIC_SERIAL_EXPORT_ENABLED
                    ? "DESATIVADA" : metricSerialTxReady ? "HABILITADA" : "BUFFER TX INDISPONIVEL");
  if (PDR_SERIAL_EXPORT_ENABLED)
    gatewayOutput.printf("[INFO] Exportacao PDR: %s\n", pdrTxReady ? "HABILITADA" : "BUFFER TX INDISPONIVEL");
  gatewayOutput.println("[OK] Gateway inicializado | HELP para comandos");
  gatewayOutput.println("============================================================");
  if (GATEWAY_TERMINAL_DETAIL) printHelp();
  radioWindowStartMs = millis();
}

void loop() {
  pollLoRaReceive();
  serviceReliableTx();
  serviceFenceTransfer();
  serviceOfflineDetection();
  framedSerial.service();
  serviceCollarStatus();
  pollSerialCommands();
  serviceRadioWindows(millis());
  serviceMetricSerialExport();
  serviceRadioTerminal(millis());
  servicePdrOutput(millis());
  serviceTerminalSummary(millis());
}
