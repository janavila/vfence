#include <TinyGPSPlus.h>

TinyGPSPlus gps;
HardwareSerial GPSserial(1);

#define GPS_RX 23

unsigned long ultimoPrint = 0;
const unsigned long intervalo = 10000;

void setup() {
  Serial.begin(115200);
  delay(1500);

  // Descobrimos que o seu GPS está transmitindo em 115200
  GPSserial.begin(115200, SERIAL_8N1, GPS_RX, -1);

  Serial.println();
  Serial.println("================================");
  Serial.println("      VFENCE - TESTE GPS");
  Serial.println("================================");
  Serial.println("Aguardando localizacao...");
}

void loop() {

  // Lê continuamente o GPS
  while (GPSserial.available() > 0) {
    gps.encode(GPSserial.read());
  }

  // Mostra informações a cada 10 segundos
  if (millis() - ultimoPrint >= intervalo) {

    ultimoPrint = millis();

    Serial.println();
    Serial.println("------------------------------");

    if (gps.location.isValid()) {

      Serial.println("LOCALIZACAO VALIDA!");

      Serial.print("Latitude:  ");
      Serial.println(gps.location.lat(), 6);

      Serial.print("Longitude: ");
      Serial.println(gps.location.lng(), 6);

    } else {

      Serial.println("Aguardando fix do GPS...");
    }

    Serial.print("Satelites: ");
    Serial.println(gps.satellites.value());

    if (gps.hdop.isValid()) {
      Serial.print("HDOP: ");
      Serial.println(gps.hdop.hdop());
    }

    Serial.println("------------------------------");
  }
}