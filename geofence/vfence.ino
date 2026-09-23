#include <TinyGPSPlus.h>
#include <math.h>

TinyGPSPlus gps;
HardwareSerial GPSserial(1);

#define GPS_RX 23

unsigned long ultimoPrint = 0;
const unsigned long intervalo = 10000;

// -----------------------------------------
// GEOFENCE
// -----------------------------------------

bool cercaCriada = false;

double latMin;
double latMax;
double lonMin;
double lonMax;


// Cria a cerca
void criarCerca(double latCentro, double lonCentro) {

  latMin = -31.3133945;
  latMax = -31.3131703;

  lonMin = -54.0868848;
  lonMax = -54.0867535;

  cercaCriada = true;

  Serial.println();
  Serial.println("================================");
  Serial.println("        CERCA CRIADA");
  Serial.println("================================");

  Serial.print("Latitude minima:  ");
  Serial.println(latMin, 6);

  Serial.print("Latitude maxima:  ");
  Serial.println(latMax, 6);

  Serial.print("Longitude minima: ");
  Serial.println(lonMin, 6);

  Serial.print("Longitude maxima: ");
  Serial.println(lonMax, 6);

  Serial.println("================================");
}


// Verifica se determinada coordenada está dentro da cerca
bool estaDentroDaCerca(double latitude, double longitude) {

  if (latitude >= latMin &&
      latitude <= latMax &&
      longitude >= lonMin &&
      longitude <= lonMax) {

    return true;
  }

  return false;
}


void setup() {

  Serial.begin(115200);

  delay(1500);

  GPSserial.begin(
      115200,
      SERIAL_8N1,
      GPS_RX,
      -1
  );

  Serial.println();
  Serial.println("================================");
  Serial.println("      VFENCE - TESTE GPS");
  Serial.println("================================");
  Serial.println("Aguardando localizacao...");
}


void loop() {

  // -----------------------------------------
  // Lê continuamente os dados enviados pelo GPS
  // -----------------------------------------

  while (GPSserial.available() > 0) {

    gps.encode(GPSserial.read());
  }


  // -----------------------------------------
  // Mostra informações a cada 10 segundos
  // -----------------------------------------

  if (millis() - ultimoPrint >= intervalo) {

    ultimoPrint = millis();

    Serial.println();
    Serial.println("------------------------------");

    if (gps.location.isValid()) {

      double latitude = gps.location.lat();
      double longitude = gps.location.lng();

      Serial.println("LOCALIZACAO VALIDA!");

      Serial.print("Latitude:  ");
      Serial.println(latitude, 6);

      Serial.print("Longitude: ");
      Serial.println(longitude, 6);


      // -------------------------------------
      // Primeira localização válida
      // cria a cerca
      // -------------------------------------

      if (!cercaCriada) {

        criarCerca(latitude, longitude);
      }


      // -------------------------------------
      // Verificação da geofence
      // -------------------------------------

      if (cercaCriada) {

        bool dentro =
            estaDentroDaCerca(latitude, longitude);

        Serial.println();

        if (dentro) {

          Serial.println("STATUS: DENTRO DA CERCA");

        } else {

          Serial.println("STATUS: FORA DA CERCA");
        }
      }

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
