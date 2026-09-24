#define GPS_TX_ENTRADA 23

unsigned long ultimoTeste = 0;
unsigned long transicoes = 0;
int estadoAnterior;

void setup() {
  Serial.begin(115200);
  delay(1500);

  pinMode(GPS_TX_ENTRADA, INPUT);

  estadoAnterior = digitalRead(GPS_TX_ENTRADA);

  Serial.println();
  Serial.println("=== TESTE TXD NEO #2 - JUMPER NOVO ===");
}

void loop() {

  int estadoAtual = digitalRead(GPS_TX_ENTRADA);

  if (estadoAtual != estadoAnterior) {
    transicoes++;
    estadoAnterior = estadoAtual;
  }

  if (millis() - ultimoTeste >= 5000) {

    ultimoTeste = millis();

    Serial.print("Transicoes: ");
    Serial.println(transicoes);

    Serial.print("Estado TXD: ");
    Serial.println(estadoAtual ? "HIGH" : "LOW");

    Serial.println("------------------");

    transicoes = 0;
  }
}
