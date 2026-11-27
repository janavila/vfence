#pragma once

#include <Arduino.h>
#include "lora_profiles.h"

// ============================================================
// VFENCE GATEWAY - CONFIGURACAO DE HARDWARE / RADIO
// Heltec WiFi LoRa 32 (V2) / SX1276
// ============================================================

static const long LORA_FREQUENCY_HZ = vfence::LORA_PROFILES[0].frequencyHz;

static const int LORA_SCK  = 5;
static const int LORA_MISO = 19;
static const int LORA_MOSI = 27;
static const int LORA_SS   = 18;
static const int LORA_RST  = 14;
static const int LORA_DIO0 = 26;

// Tuning inicial para a prova de conceito.
// ESTES PARAMETROS DEVEM SER IGUAIS NO GATEWAY E NA COLEIRA.
static const int  LORA_SPREADING_FACTOR = vfence::LORA_PROFILES[0].spreadingFactor;
static const long LORA_SIGNAL_BANDWIDTH = vfence::LORA_PROFILES[0].bandwidthHz;
static const int  LORA_CODING_RATE_DENOMINATOR = vfence::LORA_PROFILES[0].codingRateDenominator;
static const long LORA_PREAMBLE_LENGTH = vfence::LORA_PROFILES[0].preambleSymbols;
static const uint8_t LORA_SYNC_WORD = vfence::LORA_PROFILES[0].syncWord;
static const int LORA_TX_POWER_DBM = vfence::LORA_PROFILES[0].txPowerDbm;

// Gateway considera a coleira offline apos 3 minutos sem qualquer pacote.
static const uint32_t COLLAR_OFFLINE_TIMEOUT_MS = 180000UL;

// Retry para mensagens confiaveis Gateway -> Coleira.
static const uint8_t MAX_TRANSMISSIONS = 4;       // 1 original + 3 retries
static const uint32_t ACK_TIMEOUT_MS = 5000UL;
static const uint32_t RETRY_JITTER_MS = 2000UL;

// Habilitar apenas em bancada apos provisionar a Base e validar o transporte.
static const bool METRIC_SERIAL_EXPORT_ENABLED = true;
static const bool PROVEN_METRICS_ENABLED = true;
static const bool PDR_SERIAL_EXPORT_ENABLED = false;
static const bool EVENT_SERIAL_COMMIT_ENABLED = false;
static const bool PDR_SERIAL_COMMIT_ENABLED = false;

// Emite uma linha [APP] packet_log para cada TX LoRa tentado e cada RX LoRa
// recebido. O PC pode persistir essas linhas em JSONL/CSV sem alterar o
// protocolo pelo radio.
static const bool PACKET_SERIAL_LOG_ENABLED = true;

// Troca coordenada P0 <-> P1/P2/P3 via PROFILE_CONTROL v2. A maquina de
// estados usa PREPARE/READY/ARM/ARMED/POLL/VERIFIED e recuperacao P0.
static const bool PROFILE_SWITCH_ENABLED = true;
