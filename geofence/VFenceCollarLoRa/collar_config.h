#pragma once

#include <Arduino.h>

// ============================================================
// VFENCE COLLAR - HARDWARE / RADIO
// Heltec WiFi LoRa 32 (V2) / SX1276
// ============================================================

static const uint8_t COLLAR_ID = 0x01;

// GPS NEO-6M: TX do GPS -> GPIO23 da Heltec.
static const int GPS_RX = 23;
static const uint32_t GPS_BAUD = 115200;

// LoRa - mesmos parametros do VFenceGateway.
static const long LORA_FREQUENCY_HZ = 915000000L;

static const int LORA_SCK  = 5;
static const int LORA_MISO = 19;
static const int LORA_MOSI = 27;
static const int LORA_SS   = 18;
static const int LORA_RST  = 14;
static const int LORA_DIO0 = 26;

static const int  LORA_SPREADING_FACTOR = 7;
static const long LORA_SIGNAL_BANDWIDTH = 125000L;
static const int  LORA_CODING_RATE_DENOMINATOR = 5; // 4/5
static const long LORA_PREAMBLE_LENGTH = 8;
static const uint8_t LORA_SYNC_WORD = 0x12;
static const int LORA_TX_POWER_DBM = 14;
