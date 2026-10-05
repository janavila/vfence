#pragma once

#include <Arduino.h>

// ============================================================
// VFENCE GATEWAY - CONFIGURACAO DE HARDWARE / RADIO
// Heltec WiFi LoRa 32 (V2) / SX1276
// ============================================================

static const long LORA_FREQUENCY_HZ = 915000000L;

static const int LORA_SCK  = 5;
static const int LORA_MISO = 19;
static const int LORA_MOSI = 27;
static const int LORA_SS   = 18;
static const int LORA_RST  = 14;
static const int LORA_DIO0 = 26;

// Tuning inicial para a prova de conceito.
// ESTES PARAMETROS DEVEM SER IGUAIS NO GATEWAY E NA COLEIRA.
static const int  LORA_SPREADING_FACTOR = 7;
static const long LORA_SIGNAL_BANDWIDTH = 125000L;
static const int  LORA_CODING_RATE_DENOMINATOR = 5; // 4/5
static const long LORA_PREAMBLE_LENGTH = 8;
static const uint8_t LORA_SYNC_WORD = 0x12;
static const int LORA_TX_POWER_DBM = 14;

// Gateway considera a coleira offline apos 3 minutos sem qualquer pacote.
static const uint32_t COLLAR_OFFLINE_TIMEOUT_MS = 180000UL;

// Retry para mensagens confiaveis Gateway -> Coleira.
static const uint8_t MAX_TRANSMISSIONS = 4;       // 1 original + 3 retries
static const uint32_t ACK_TIMEOUT_MS = 5000UL;
static const uint32_t RETRY_JITTER_MS = 2000UL;
