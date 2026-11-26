#pragma once

#include <Arduino.h>
#include "lora_profiles.h"

// ============================================================
// VFENCE COLLAR - HARDWARE / RADIO
// Heltec WiFi LoRa 32 (V2) / SX1276
// ============================================================

static const uint8_t COLLAR_ID = 0x01;
// Habilitar somente depois de atualizar o Gateway para o subtipo contado.
static const bool COUNTED_POSITION_ENABLED = false;
static const bool PROVEN_METRICS_ENABLED = true;
static const bool PROFILE_SWITCH_ENABLED = false;

// GPS NEO-6M: TX do GPS -> GPIO23 da Heltec.
static const int GPS_RX = 23;
static const uint32_t GPS_BAUD = 115200;

// LoRa - mesmos parametros do VFenceGateway.
static const long LORA_FREQUENCY_HZ = vfence::LORA_PROFILES[0].frequencyHz;

static const int LORA_SCK  = 5;
static const int LORA_MISO = 19;
static const int LORA_MOSI = 27;
static const int LORA_SS   = 18;
static const int LORA_RST  = 14;
static const int LORA_DIO0 = 26;

static const int  LORA_SPREADING_FACTOR = vfence::LORA_PROFILES[0].spreadingFactor;
static const long LORA_SIGNAL_BANDWIDTH = vfence::LORA_PROFILES[0].bandwidthHz;
static const int  LORA_CODING_RATE_DENOMINATOR = vfence::LORA_PROFILES[0].codingRateDenominator;
static const long LORA_PREAMBLE_LENGTH = vfence::LORA_PROFILES[0].preambleSymbols;
static const uint8_t LORA_SYNC_WORD = vfence::LORA_PROFILES[0].syncWord;
static const int LORA_TX_POWER_DBM = vfence::LORA_PROFILES[0].txPowerDbm;

// ============================================================
// ATUADORES - PROVA DE CONCEITO
// ============================================================
// IMPORTANTE: GPIO7 nao deve ser usado na Heltec WiFi LoRa 32 V2,
// pois pertence ao barramento da memoria flash do ESP32.
// O buzzer foi realocado para GPIO32.
static const int BUZZER_PIN = 32;
static const bool BUZZER_ACTIVE_HIGH = true;

// Microservo usado somente como segundo atuador de prova de conceito.
// GPIO13 nao conflita com GPS/LoRa usados neste firmware.
static const int SERVO_PIN = 13;
static const int SERVO_INACTIVE_ANGLE = 0;
static const int SERVO_ACTIVE_ANGLE = 90;
static const int SERVO_MIN_US = 500;
static const int SERVO_MAX_US = 2400;
