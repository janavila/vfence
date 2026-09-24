#include <SPI.h>
#include <LoRa.h>

// Pinos LoRa da Heltec WiFi LoRa 32 V2
#define LORA_SCK   5
#define LORA_MISO  19
#define LORA_MOSI  27
#define LORA_SS    18
#define LORA_RST   14
#define LORA_DIO0  26

int contador = 0;

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("VFence - Inicializando transmissor LoRa...");

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);

  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  if (!LoRa.begin(915E6)) {
    Serial.println("ERRO: LoRa nao iniciou!");
    while (true);
  }

  LoRa.setTxPower(5);

  Serial.println("LoRa iniciado!");
  Serial.println("TRANSMISSOR VFence pronto.");
}

void loop() {
  contador++;

  Serial.print("Enviando: VFENCE_TESTE_");
  Serial.println(contador);

  LoRa.beginPacket();
  LoRa.print("VFENCE_TESTE_");
  LoRa.print(contador);
  LoRa.endPacket();

  delay(2000);
}