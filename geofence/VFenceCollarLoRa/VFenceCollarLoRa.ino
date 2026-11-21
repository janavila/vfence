#include <Arduino.h>
#include <TinyGPSPlus.h>
#include <Preferences.h>
#include <SPI.h>
#include <LoRa.h>
#include <ESP32Servo.h>
#include <esp_random.h>

#include "collar_config.h"
#include "counted_position.h"
#include "terminal_serial.h"

#include "vfence_protocol.h"
#include "vfence_geofence.h"
#include "vfence_fence_transfer.h"

using namespace vfence;

// ============================================================
// HARDWARE / IDENTIDADE
// ============================================================

TinyGPSPlus gps;
HardwareSerial GPSserial(1);
Preferences prefs;
Servo secondActuatorServo;

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
static uint32_t lastLoRaTxMs = 0;
static uint32_t lastConfigRxMs = 0;
static bool hdopConfigGuard = false;

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
// RADIO / TRANSPORTE LORA
// ============================================================
// O VFENCE continua independente do radio: todo envio passa por
// transportSend(), e todo pacote recebido e entregue a
// processIncomingPacket(). Assim o wire format permanece igual.
// ============================================================

void printHexPacket(const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    if (data[i] < 0x10) Serial.print('0');
    Serial.print(data[i], HEX);
    if (i + 1 < len) Serial.print(' ');
  }
}

static const bool COLLAR_TERMINAL_DETAIL = false;
static TerminalSerial collarTerminal;
static uint32_t countedPositionOrdinal = 0;

void printRadioConfig() {
  Serial.println("--- RADIO CONFIG ---");
  Serial.print("Frequency: ");
  Serial.print(LORA_FREQUENCY_HZ);
  Serial.println(" Hz");
  Serial.print("SF: ");
  Serial.println(LORA_SPREADING_FACTOR);
  Serial.print("BW: ");
  Serial.print(LORA_SIGNAL_BANDWIDTH);
  Serial.println(" Hz");
  Serial.print("CR: 4/");
  Serial.println(LORA_CODING_RATE_DENOMINATOR);
  Serial.print("Preamble: ");
  Serial.println(LORA_PREAMBLE_LENGTH);
  Serial.print("SyncWord: 0x");
  if (LORA_SYNC_WORD < 0x10) Serial.print('0');
  Serial.println(LORA_SYNC_WORD, HEX);
  Serial.print("TX power: ");
  Serial.print(LORA_TX_POWER_DBM);
  Serial.println(" dBm");
  Serial.println("CRC: ON");
}

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

bool transportSend(const uint8_t* data, size_t len, bool logPacket = true) {
  if (!data || len == 0 || len > MAX_PACKET_SIZE) return false;

  if (logPacket && COLLAR_TERMINAL_DETAIL) {
    char hex[MAX_PACKET_SIZE * 3];
    terminalHex(data, len, hex);
    collarTerminal.print(Serial, "[TX LoRa %u bytes] %s\n", unsigned(len), hex);
  }

  // SX1276 e half-duplex: sai de RX, transmite e volta imediatamente a RX.
  LoRa.idle();
  if (LoRa.beginPacket() == 0) {
    LoRa.receive();
    return false;
  }

  const size_t written = LoRa.write(data, len);
  const int result = LoRa.endPacket();
  LoRa.receive();
  lastLoRaTxMs = millis();

  return written == len && result == 1;
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
      collarTerminal.print(Serial, "[ACK LoRa] [COL %03u] confirmado seq=%u\n", unsigned(COLLAR_ID), unsigned(sequence));
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

  collarTerminal.print(Serial, "[NACK LoRa] [COL %03u] seq=%u erro=0x%02X detalhe=0x%04X\n",
                unsigned(COLLAR_ID), unsigned(sequence), unsigned(errorCode), unsigned(detail));
}

void serviceReliableTx() {
  const uint32_t now = millis();

  for (uint8_t i = 0; i < MAX_PENDING_RELIABLE; ++i) {
    PendingReliableTx& tx = pendingTx[i];
    if (!tx.active) continue;

    if (static_cast<int32_t>(now - tx.nextActionMs) < 0) continue;

    if (tx.transmissions >= MAX_TRANSMISSIONS) {
      collarTerminal.print(Serial, "[ERRO] [COL %03u] ENTREGA LoRa ESGOTADA seq=%u tentativas=%u\n",
                    unsigned(COLLAR_ID), unsigned(tx.sequence), unsigned(tx.transmissions));
      tx.active = false;
      continue;
    }

    // Mesmo sequence, mas marca RETRY no byte de controle.
    tx.packet[0] |= FLAG_RETRY;
    ++tx.transmissions;

    collarTerminal.print(Serial, "[RETRY LoRa] [COL %03u] seq=%u tentativa=%u\n",
                  unsigned(COLLAR_ID), unsigned(tx.sequence), unsigned(tx.transmissions));

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

void initActuators() {
  // Buzzer: codigo assume buzzer ativo/modulo com entrada logica.
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_HIGH ? LOW : HIGH);
  buzzerActive = false;

  // Microservo: PWM padrao de servo em 50 Hz.
  secondActuatorServo.setPeriodHertz(50);
  secondActuatorServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  secondActuatorServo.write(SERVO_INACTIVE_ANGLE);
  secondActuatorActive = false;

  Serial.print("[ATUADOR] buzzer inicializado no GPIO");
  Serial.println(BUZZER_PIN);
  Serial.print("[ATUADOR] microservo inicializado no GPIO");
  Serial.print(SERVO_PIN);
  Serial.print(" | repouso=");
  Serial.print(SERVO_INACTIVE_ANGLE);
  Serial.print(" deg | ativo=");
  Serial.print(SERVO_ACTIVE_ANGLE);
  Serial.println(" deg");
}

void setBuzzer(bool on) {
  if (buzzerActive == on) return;

  const uint8_t level =
      (on == BUZZER_ACTIVE_HIGH) ? HIGH : LOW;
  digitalWrite(BUZZER_PIN, level);

  buzzerActive = on;
  if (COLLAR_TERMINAL_DETAIL)
    collarTerminal.print(Serial, "[DIAG] [COL %03u] buzzer=%s\n", unsigned(COLLAR_ID), on ? "ON" : "OFF");
}

void setSecondActuator(bool on) {
  if (secondActuatorActive == on) return;

  const int angle = on ? SERVO_ACTIVE_ANGLE : SERVO_INACTIVE_ANGLE;
  secondActuatorServo.write(angle);

  secondActuatorActive = on;
  if (COLLAR_TERMINAL_DETAIL)
    collarTerminal.print(Serial, "[DIAG] [COL %03u] atuador2=%s\n", unsigned(COLLAR_ID), on ? "ON" : "OFF");
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

static const uint32_t HDOP_SAMPLE_INTERVAL_MS = 1000UL;
static const uint32_t HDOP_WINDOW_MS = 60000UL;
static const uint8_t HDOP_SLOTS_PER_WINDOW = HDOP_WINDOW_MS / HDOP_SAMPLE_INTERVAL_MS;
static const uint8_t HDOP_SUMMARY_CAPACITY = 4;
static_assert(HDOP_WINDOW_MS == HDOP_SUMMARY_WINDOW_MS, "HDOP window differs from wire contract");
static_assert(HDOP_SLOTS_PER_WINDOW == HDOP_SUMMARY_MAX_SAMPLES, "HDOP sample limit differs from wire contract");

static uint32_t hdopWindowStartMs = 0;
static uint32_t hdopWindowSumX100 = 0;
static uint8_t hdopWindowSampleCount = 0;
static uint8_t hdopLastSampleSlot = HDOP_SLOTS_PER_WINDOW;

// Resultado da ultima janela fechada, separado dos acumuladores em andamento.
static uint16_t hdopMeanX100 = U16_UNAVAILABLE;
static uint8_t hdopSampleCount = 0;
static bool hdopMeanValid = false;

struct HdopWindowSummary {
  uint64_t sessionId;
  uint64_t windowId;
  uint32_t startedAtMs;
  uint32_t durationMs;
  uint16_t hdopMeanX100;
  uint8_t collarId;
  uint8_t hdopSampleCount;
  bool hdopMeanValid;
};

static uint64_t hdopSessionId = 0;
static uint64_t hdopNextWindowId = 1;
static HdopWindowSummary hdopSummaries[HDOP_SUMMARY_CAPACITY];
static uint8_t hdopSummaryHead = 0;
static uint8_t hdopSummaryCount = 0;
static uint64_t hdopDroppedSummaries = 0;

static bool peekPendingHdopSummary(HdopWindowSummary& out);

static bool peekPendingHdopSummary(HdopWindowSummary& out) {
  if (hdopSummaryCount == 0) return false;
  out = hdopSummaries[hdopSummaryHead];
  return true;
}

static bool consumePendingHdopSummary(uint64_t sessionId, uint64_t windowId) {
  if (hdopSummaryCount == 0) return false;
  const HdopWindowSummary& head = hdopSummaries[hdopSummaryHead];
  if (head.sessionId != sessionId || head.windowId != windowId) return false;
  hdopSummaryHead = (hdopSummaryHead + 1) % HDOP_SUMMARY_CAPACITY;
  --hdopSummaryCount;
  return true;
}

static void enqueueHdopSummary(uint32_t startedAtMs) {
  const bool overwritten = hdopSummaryCount == HDOP_SUMMARY_CAPACITY;
  const uint64_t previous = overwritten ? hdopSummaries[hdopSummaryHead].windowId : 0;
  if (hdopSummaryCount == HDOP_SUMMARY_CAPACITY) {
    hdopSummaryHead = (hdopSummaryHead + 1) % HDOP_SUMMARY_CAPACITY;
    --hdopSummaryCount;
    ++hdopDroppedSummaries;
  }
  const uint8_t tail = (hdopSummaryHead + hdopSummaryCount) % HDOP_SUMMARY_CAPACITY;
  hdopSummaries[tail] = {hdopSessionId, hdopNextWindowId++, startedAtMs, HDOP_WINDOW_MS,
                         hdopMeanX100, COLLAR_ID, hdopSampleCount, hdopMeanValid};
  ++hdopSummaryCount;
  const auto& summary = hdopSummaries[tail];
  char mean[12] = "N/A";
  if (summary.hdopMeanValid) snprintf(mean, sizeof(mean), "%.2f", summary.hdopMeanX100 / 100.0);
  const bool expanded = Serial.availableForWrite() >= 240;
  collarTerminal.print(Serial,
      expanded ? "==== VFENCE | COLEIRA %03u | HDOP 60s ====\n"
                 " sessao=%016llx janela=%016llx inicioColeira=%lu ms duracao=%lu ms\n"
                 " amostras=%u HDOP=%s validade=%s | fila=RETIDA PARA TX LoRa\n"
               : "[HDOP] [COL %03u] s=%016llx j=%016llx t=%lu d=%lu n=%u HDOP=%s %s RETIDA\n",
      unsigned(COLLAR_ID), static_cast<unsigned long long>(summary.sessionId),
      static_cast<unsigned long long>(summary.windowId), static_cast<unsigned long>(summary.startedAtMs),
      static_cast<unsigned long>(summary.durationMs), unsigned(summary.hdopSampleCount), mean,
      summary.hdopMeanValid ? (expanded ? "VALIDA" : "V") : (expanded ? "INVALIDA" : "INV"));
  if (overwritten)
    collarTerminal.print(Serial, "[AVISO] [COL %03u] fila HDOP cheia: janela=%016llx sobrescrita\n",
                  unsigned(COLLAR_ID), static_cast<unsigned long long>(previous));
}

static void serviceHdopWindow(uint32_t now) {
  const uint32_t elapsed = now - hdopWindowStartMs;
  if (elapsed < HDOP_WINDOW_MS) return;

  const uint32_t completedWindows = elapsed / HDOP_WINDOW_MS;
  // Em atrasos longos, avancar os IDs e materializar so o que cabe na fila.
  const uint32_t skipped = completedWindows > HDOP_SUMMARY_CAPACITY
      ? completedWindows - HDOP_SUMMARY_CAPACITY : 0;
  hdopNextWindowId += skipped;
  hdopDroppedSummaries += skipped;
  if (skipped)
    collarTerminal.print(Serial, "[AVISO] [COL %03u] HDOP janelas omitidas por atraso=%lu\n",
                  unsigned(COLLAR_ID), static_cast<unsigned long>(skipped));
  for (uint32_t offset = skipped; offset < completedWindows; ++offset) {
    hdopSampleCount = offset == 0 ? hdopWindowSampleCount : 0;
    hdopMeanValid = hdopSampleCount != 0;
    hdopMeanX100 = hdopMeanValid
        ? static_cast<uint16_t>((hdopWindowSumX100 + hdopSampleCount / 2U) / hdopSampleCount)
        : U16_UNAVAILABLE;
    enqueueHdopSummary(hdopWindowStartMs + offset * HDOP_WINDOW_MS);
  }

  hdopWindowStartMs += completedWindows * HDOP_WINDOW_MS;
  hdopWindowSumX100 = 0;
  hdopWindowSampleCount = 0;
  hdopLastSampleSlot = HDOP_SLOTS_PER_WINDOW;

}

static bool hasValidGgaHdopFields(char byte) {
  // A TinyGPSPlus pode sinalizar HDOP atualizado mesmo com o campo GGA vazio.
  // Acompanhar qualidade do fix (6) e HDOP (8) ate o checksum da mesma GGA.
  static uint8_t field = 0;
  static uint8_t integerDigits = 0;
  static bool hasDigit = false;
  static bool hasDot = false;
  static bool malformed = false;
  static bool fixQualitySeen = false;
  static bool gnssFix = false;
  static bool checksumStarted = false;

  if (byte == '$') {
    field = 0;
    integerDigits = 0;
    hasDigit = hasDot = malformed = false;
    fixQualitySeen = gnssFix = checksumStarted = false;
  } else if (!checksumStarted && (byte == ',' || byte == '*')) {
    if (field <= 8) ++field;
    checksumStarted = byte == '*';
  } else if (!checksumStarted && field == 6) {
    gnssFix = !fixQualitySeen && (byte == '1' || byte == '2' || byte == '4' || byte == '5');
    fixQualitySeen = true;
  } else if (!checksumStarted && field == 8) {
    if (byte >= '0' && byte <= '9') {
      hasDigit = true;
      if (!hasDot && ++integerDigits > 3) malformed = true;
    } else if (byte == '.' && !hasDot) {
      hasDot = true;
    } else {
      malformed = true;
    }
  }
  return checksumStarted && field > 8 && gnssFix && hasDigit && !malformed;
}

static void sampleUpdatedHdop(uint32_t now, bool eligibleGga) {
  if (!gps.hdop.isValid()) return;
  // value() ja retorna HDOP x100 e consome a flag isUpdated().
  const int32_t hdopX100 = gps.hdop.value();
  if (!eligibleGga || hdopX100 < 0 || hdopX100 >= U16_UNAVAILABLE) return;

  serviceHdopWindow(now);
  const uint8_t slot = (now - hdopWindowStartMs) / HDOP_SAMPLE_INTERVAL_MS;
  // Usar a primeira amostra valida de cada segundo. As demais sao descartadas.
  if (slot == hdopLastSampleSlot) return;
  hdopLastSampleSlot = slot;
  hdopWindowSumX100 += static_cast<uint32_t>(hdopX100);
  ++hdopWindowSampleCount;
}

static void serviceHdopSummaryTx(uint32_t now) {
  static bool scheduled = false;
  static uint64_t scheduledWindowId = 0;
  static uint32_t dueAtMs = 0;
  if (hdopConfigGuard && now - lastConfigRxMs >= 8000UL) hdopConfigGuard = false;

  HdopWindowSummary summary;
  if (!peekPendingHdopSummary(summary)) {
    scheduled = false;
    return;
  }
  if (!scheduled || scheduledWindowId != summary.windowId) {
    scheduled = true;
    scheduledWindowId = summary.windowId;
    dueAtMs = now + random(500, 5001);
    return;
  }
  if (static_cast<int32_t>(now - dueAtMs) < 0 || hdopConfigGuard ||
      now - lastLoRaTxMs < 500UL ||
      static_cast<int32_t>(nextTelemetryAtMs - now) <= 1250) return;
  for (const PendingReliableTx& tx : pendingTx) {
    if (tx.active) return;
  }

  const HdopSummaryPayload payload{summary.sessionId, summary.windowId, summary.startedAtMs,
                                   static_cast<uint16_t>(summary.durationMs), summary.hdopMeanX100,
                                   summary.hdopSampleCount, summary.hdopMeanValid};
  uint8_t packet[MAX_PACKET_SIZE];
  const size_t len = encodeHdopSummary(packet, sizeof(packet), summary.collarId,
                                      allocateSequence(), payload);
  if (!len || !transportSend(packet, len, COLLAR_TERMINAL_DETAIL)) {
    dueAtMs = millis() + random(5000, 8001);
    collarTerminal.print(Serial, "[ERRO] [COL %03u] HDOP 60s sessao=%016llx janela=%016llx falha de TX local\n",
                  unsigned(COLLAR_ID), static_cast<unsigned long long>(summary.sessionId),
                  static_cast<unsigned long long>(summary.windowId));
    return;
  }
  consumePendingHdopSummary(summary.sessionId, summary.windowId);
  scheduled = false;
  collarTerminal.print(Serial, "[TX LoRa] [COL %03u] HDOP_WINDOW_SUMMARY sessao=%016llx janela=%016llx TX local aceito\n",
                unsigned(COLLAR_ID), static_cast<unsigned long long>(summary.sessionId),
                static_cast<unsigned long long>(summary.windowId));
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

static void printCollarEvent(const char* name, uint16_t seq, bool alert, bool sent) {
  const char* rule = "";
  if (Serial.availableForWrite() >= 200)
    rule = alert ? "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n" : "----------------------------------------\n";
  collarTerminal.print(Serial, "%s[%s] [COL %03u] %s seq=%u cerca=v%u TX=%s\n%s", rule, alert ? "ALERTA" : "EVENTO",
                unsigned(COLLAR_ID), name, unsigned(seq), unsigned(activeFenceVersion),
                sent ? "LOCAL ACEITO" : "FALHA INICIAL", rule);
}

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
  const uint16_t seq = allocateSequence();
  const size_t len = encodePositionEvent(packet, sizeof(packet), COLLAR_ID,
                                         seq, false,
                                         subtype, eventData);
  const bool outside = subtype == EventSubtype::OUTSIDE;
  const bool sent = len && sendReliable(packet, len);
  printCollarEvent(outside ? "OUTSIDE" : "BACK_INSIDE", seq, outside, sent);
}

void sendGpsLostEvent() {
  GpsLostPayload d;
  d.lastPosition = gpsEverValid
      ? latestPosition
      : CoordinateE7{COORD_UNAVAILABLE, COORD_UNAVAILABLE};
  d.positionAgeS = positionAgeSeconds();

  uint8_t packet[MAX_PACKET_SIZE];
  const uint16_t seq = allocateSequence();
  const size_t len = encodeGpsLost(packet, sizeof(packet), COLLAR_ID,
                                   seq, false, d);
  const bool sent = len && sendReliable(packet, len);
  printCollarEvent("GPS_LOST", seq, true, sent);
}

void sendGpsRecoveredEvent() {
  GpsRecoveredPayload d;
  d.position = latestPosition;
  d.satellites = currentSatellites();
  d.hdopX100 = currentHdopX100();

  uint8_t packet[MAX_PACKET_SIZE];
  const uint16_t seq = allocateSequence();
  const size_t len = encodeGpsRecovered(packet, sizeof(packet), COLLAR_ID,
                                        seq, false, d);
  const bool sent = len && sendReliable(packet, len);
  printCollarEvent("GPS_RECOVERED", seq, false, sent);
}

void sendDeviceStartedEvent() {
  DeviceStartedPayload d;
  d.resetReason = ResetReason::POWER_ON;  // depois podemos mapear esp_reset_reason().
  d.activeFenceVersion = activeFenceVersion;
  d.batteryPct = batteryPct;

  uint8_t packet[MAX_PACKET_SIZE];
  const uint16_t seq = allocateSequence();
  const size_t len = encodeDeviceStarted(packet, sizeof(packet), COLLAR_ID,
                                         seq, false, d);
  const bool sent = len && sendReliable(packet, len);
  printCollarEvent("DEVICE_STARTED", seq, false, sent);
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
  const uint16_t seq = allocateSequence();
  const size_t len = COUNTED_POSITION_ENABLED
      ? encodeCountedPosition(packet, sizeof(packet), COLLAR_ID, seq, d,
                              {hdopSessionId, countedPositionOrdinal + 1U, INITIAL_LORA_PROFILE})
      : encodeTelemetry(packet, sizeof(packet), COLLAR_ID, seq, d);
  const bool sent = len && transportSend(packet, len);
  if (COUNTED_POSITION_ENABLED && sent) ++countedPositionOrdinal;
  const bool gpsValid = (d.stateFlags & STATE_GPS_VALID) != 0;
  const char* zone = "N/A";
  if (gpsValid) {
    if (!(d.stateFlags & STATE_INSIDE)) zone = "FORA";
    else if (d.stateFlags & STATE_VERY_NEAR) zone = "CRITICO";
    else if (d.stateFlags & STATE_NEAR) zone = "ATENCAO";
    else zone = "SEGURO";
  }
  char hdop[12] = "N/A";
  if (gpsValid && d.hdopX100 != U16_UNAVAILABLE) snprintf(hdop, sizeof(hdop), "%.2f", d.hdopX100 / 100.0);
  collarTerminal.print(Serial, "[%s] [COL %03u] POSITION_STATUS seq=%u GPS=%s HDOP=%s ZONA=%s\n",
                sent ? "TX LoRa" : "ERRO TX LoRa", unsigned(COLLAR_ID), unsigned(seq),
                gpsValid ? "VALIDO" : "INVALIDO", hdop, zone);
  if (COLLAR_TERMINAL_DETAIL) {
    char lat[24] = "N/A", lon[24] = "N/A", sats[8] = "N/A", border[16] = "N/A", battery[8] = "N/A";
    if (d.position.lat != COORD_UNAVAILABLE) snprintf(lat, sizeof(lat), "%.7f", e7ToDegrees(d.position.lat));
    if (d.position.lon != COORD_UNAVAILABLE) snprintf(lon, sizeof(lon), "%.7f", e7ToDegrees(d.position.lon));
    if (d.satellites != U8_UNAVAILABLE) snprintf(sats, sizeof(sats), "%u", unsigned(d.satellites));
    if (d.distanceBorderM != U16_UNAVAILABLE) snprintf(border, sizeof(border), "%um", unsigned(d.distanceBorderM));
    if (d.batteryPct != U8_UNAVAILABLE) snprintf(battery, sizeof(battery), "%u%%", unsigned(d.batteryPct));
    collarTerminal.print(Serial, "[DIAG] [COL %03u] seq=%u lat=%s lon=%s sat=%s borda=%s bateria=%s cerca=v%u flags=0x%02X buzzer=%s atuador2=%s\n",
                  unsigned(COLLAR_ID), unsigned(seq), lat, lon, sats, border, battery,
                  unsigned(d.fenceVersion), unsigned(d.stateFlags),
                  (d.stateFlags & STATE_BUZZER_ACTIVE) ? "ON" : "OFF",
                  (d.stateFlags & STATE_SECOND_ACTUATOR_ACTIVE) ? "ON" : "OFF");
  }
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
    collarTerminal.print(Serial, "==== VFENCE | COLEIRA %03u | CERCA ====\n"
                  "[OK] inicio v%u vertices=%u chunks=%u CRC32=0x%08lX\n",
                  unsigned(COLLAR_ID), unsigned(version), unsigned(vertexCount), unsigned(chunkCount),
                  static_cast<unsigned long>(crc));
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

    if (COLLAR_TERMINAL_DETAIL)
      collarTerminal.print(Serial, "[DIAG] [COL %03u] CERCA v%u chunk=%u recebido\n",
                    unsigned(COLLAR_ID), unsigned(version), unsigned(chunkIndex));
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
    collarTerminal.print(Serial, "[OK] [COL %03u] CERCA v%u ativada e persistida\n", unsigned(COLLAR_ID), unsigned(version));
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
    collarTerminal.print(Serial, "[RX] pacote invalido: tamanho/cabecalho\n");
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
      lastConfigRxMs = millis();
      hdopConfigGuard = true;
      processConfig(packet);
      break;
    case MessageType::CONTROL:
      processControl(packet);
      break;
    case MessageType::EVENT:
    case MessageType::TELEMETRY:
      collarTerminal.print(Serial, "[RX] tipo nao esperado na coleira\n");
      break;
  }
}

void pollLoRaReceive() {
  const int packetSize = LoRa.parsePacket();
  if (packetSize <= 0) return;

  uint8_t data[MAX_PACKET_SIZE];
  size_t count = 0;
  const bool oversized = packetSize > static_cast<int>(MAX_PACKET_SIZE);

  while (LoRa.available()) {
    const int b = LoRa.read();
    if (count < MAX_PACKET_SIZE && b >= 0) {
      data[count++] = static_cast<uint8_t>(b);
    }
  }

  const int rssi = LoRa.packetRssi();
  const float snr = LoRa.packetSnr();

  if (oversized) {
    collarTerminal.print(Serial, "[RX LoRa] pacote maior que MAX_PACKET_SIZE: %d\n", packetSize);
    return;
  }

  if (COLLAR_TERMINAL_DETAIL) {
    char hex[MAX_PACKET_SIZE * 3];
    terminalHex(data, count, hex);
    collarTerminal.print(Serial, "[RX LoRa] [COL %03u] bytes=%u RSSI=%ddBm SNR=%.2fdB\n%s\n",
                         unsigned(COLLAR_ID), unsigned(count), rssi, static_cast<double>(snr), hex);
  }

  processIncomingPacket(data, count);
}

// Mantemos a injecao pela Serial para diagnostico, mesmo com LoRa real.
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
  const bool firstFix = !gpsEverValid;
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
  if (firstFix) {
    collarTerminal.print(Serial, "[GPS] [COL %03u] FIX ADQUIRIDO lat=%.7f lon=%.7f\n",
                  unsigned(COLLAR_ID), e7ToDegrees(latestPosition.lat), e7ToDegrees(latestPosition.lon));
  }
}

void serviceGpsTimeout() {
  if (gpsLostState) return;

  const uint32_t reference = gpsEverValid ? lastValidGpsMs : gpsMonitorStartMs;
  const uint32_t timeoutMs = static_cast<uint32_t>(params.gpsTimeoutS) * 1000UL;

  if (millis() - reference >= timeoutMs) {
    gpsLostState = true;
    sendGpsLostEvent();
  }
}

// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  Serial.begin(115200);
  esp_fill_random(&hdopSessionId, sizeof(hdopSessionId));
  delay(1500);

  randomSeed(static_cast<uint32_t>(micros()));

  // Coloca os atuadores em estado seguro antes de iniciar sensores/radio.
  initActuators();

  GPSserial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, -1);

  if (!initLoRa()) {
    Serial.printf("[FATAL] [COL %03u] falha ao iniciar LoRa\n", unsigned(COLLAR_ID));
    Serial.println("Verifique placa, pinos, antena e frequencia.");
    while (true) delay(1000);
  }

  memset(pendingTx, 0, sizeof(pendingTx));

  loadParametersFromNvs();
  if (!loadFenceFromNvs()) loadDevelopmentFence();

  geofence.setThresholds(params.muitoPertoM, params.pertoM);

  gpsMonitorStartMs = millis();
  scheduleNextTelemetry();

  Serial.println();
  Serial.println("========================================");
  Serial.printf(" VFENCE | COLEIRA %03u | INICIALIZACAO\n", unsigned(COLLAR_ID));
  Serial.println("========================================");
  Serial.print("Collar ID: 0x");
  Serial.println(COLLAR_ID, HEX);
  Serial.print("Fence version: ");
  Serial.println(activeFenceVersion);
  Serial.print("Vertices: ");
  Serial.println(activeFenceCount);
  printRadioConfig();
  Serial.printf("[INFO] GNSS UART=%lu baud, aguardando fix\n", static_cast<unsigned long>(GPS_BAUD));
  Serial.println("[OK] Inicializacao concluida");
  Serial.println("============================================================");

  // Como nextSequence inicia em zero, DEVICE_STARTED usa sequence 0.
  sendDeviceStartedEvent();
  hdopWindowStartMs = millis();
}

static void serviceCollarTerminalSummary(uint32_t now) {
  static uint32_t attemptedAt = 0, reported = 0;
  if (now - attemptedAt < 60000UL || reported == collarTerminal.omitted() || GPSserial.available()) return;
  for (const auto& tx : pendingTx) {
    if (tx.active) return;
  }
  attemptedAt = now;
  if (collarTerminal.tryPrint(Serial, "[AVISO] [COL %03u] logsOmitidos=%lu descartesHDOP=%llu\n",
      unsigned(COLLAR_ID), static_cast<unsigned long>(collarTerminal.omitted()),
      static_cast<unsigned long long>(hdopDroppedSummaries))) reported = collarTerminal.omitted();
}

void loop() {
  // GPS deve ser drenado continuamente.
  while (GPSserial.available() > 0) {
    const char byte = static_cast<char>(GPSserial.read());
    const bool eligibleGga = hasValidGgaHdopFields(byte);
    if (gps.encode(byte) && gps.hdop.isUpdated()) {
      sampleUpdatedHdop(millis(), eligibleGga);
    }
  }

  if (gps.location.isUpdated() && gps.location.isValid()) {
    processNewGpsPosition();
  }

  serviceGpsTimeout();
  pollLoRaReceive();
  serviceReliableTx();
  pollSerialPacketInjection();

  if (static_cast<int32_t>(millis() - nextTelemetryAtMs) >= 0) {
    sendTelemetryPacket();
    scheduleNextTelemetry();
  }

  serviceHdopWindow(millis());
  serviceHdopSummaryTx(millis());
  serviceCollarTerminalSummary(millis());
}
