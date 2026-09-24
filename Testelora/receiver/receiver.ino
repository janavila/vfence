#include <SPI.h>
#include <LoRa.h>

// Pinos LoRa da Heltec WiFi LoRa 32 V2
#define LORA_SCK   5
#define LORA_MISO  19
#define LORA_MOSI  27
#define LORA_SS    18
#define LORA_RST   14
#define LORA_DIO0  26

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("VFence - Inicializando receptor LoRa...");

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);

  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  if (!LoRa.begin(915E6)) {
    Serial.println("ERRO: LoRa nao iniciou!");
    while (true);
  }

  Serial.println("LoRa iniciado!");
  Serial.println("RECEPTOR VFence pronto.");
}

void loop() {
  int packetSize = LoRa.parsePacket();

  if (packetSize) {
    String mensagem = "";

    while (LoRa.available()) {
      mensagem += (char)LoRa.read();
    }

    Serial.print("Recebido: ");
    Serial.print(mensagem);

    Serial.print(" | RSSI: ");
    Serial.print(LoRa.packetRssi());

    Serial.print(" dBm | SNR: ");
    Serial.println(LoRa.packetSnr());
  }
}