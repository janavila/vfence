# VFenceCollarLoRa

Firmware da coleira VFENCE com transporte LoRa real para Heltec WiFi LoRa 32 (V2), mantendo o GPS, geofence e VFENCE Protocol v1 já validados.

## Dependências Arduino IDE

Instale pelo Library Manager:

- TinyGPSPlus
- LoRa by Sandeep Mistry

`Preferences` e `SPI` fazem parte do core ESP32/Arduino.

## Hardware

### GPS NEO-6M

- TX do GPS -> GPIO23 da Heltec
- GND -> GND
- alimentação conforme o módulo utilizado
- baud do GPS no projeto: 115200

### LoRa Heltec WiFi LoRa 32 V2 / SX1276

Os pinos usados pelo rádio integrado são:

- SCK: GPIO5
- MISO: GPIO19
- MOSI: GPIO27
- NSS: GPIO18
- RESET: GPIO14
- DIO0: GPIO26

## Configuração de rádio

Deve ser idêntica à do gateway:

- Frequência: 915000000 Hz
- SF: 7
- BW: 125000 Hz
- CR: 4/5
- Preamble: 8
- Sync word: 0x12
- TX power: 14 dBm
- CRC: ON

## Primeiro teste físico

1. Conecte uma antena adequada aos DOIS Heltec antes de transmitir.
2. Deixe o gateway ligado e com Monitor Serial a 115200.
3. Grave este sketch na coleira.
4. Abra o Monitor Serial da coleira a 115200.
5. No boot, a coleira envia `DEVICE_STARTED` (ACK_REQ + URGENT).
6. O gateway deve receber o evento e transmitir um `ACK`.
7. A coleira deve mostrar `[ACK] mensagem 0 confirmada` (se `DEVICE_STARTED` for sequence 0).
8. Depois, as mensagens `POSITION_STATUS` devem chegar ao gateway periodicamente.

O firmware ainda aceita `RX <hex...>` pela Serial para testes de recepção sem rádio.

## Observação

A função `transportSend()` é o único ponto que envia os bytes VFENCE pelo SX1276. O formato binário não foi alterado; somente o transporte deixou de ser simulado pela Serial.
