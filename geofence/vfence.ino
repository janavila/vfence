#include <TinyGPSPlus.h>
#include <math.h>
#include <stdint.h>

// ======================================================
// GPS
// ======================================================

TinyGPSPlus gps;
HardwareSerial GPSserial(1);

#define GPS_RX 23

const uint32_t GPS_BAUD = 115200;


// ======================================================
// CONFIGURAÇÕES GERAIS
// ======================================================

// Número máximo de vértices que uma cerca pode possuir.
// Pode ser aumentado futuramente.
const size_t MAX_VERTICES = 32;

// Faixas de proximidade.
// Podem ser alteradas em tempo de execução.
double limiteMuitoPertoM = 10.0;
double limitePertoM = 30.0;

// Intervalo em que enviaremos/mostraremos telemetria.
// Futuramente será o intervalo de transmissão LoRa.
uint32_t intervaloTelemetriaMs = 10000;

uint32_t ultimoEnvioTelemetria = 0;


// ======================================================
// ESTRUTURAS DE DADOS
// ======================================================

// Coordenada normal, usada internamente nos cálculos.
struct Coordenada {
  double lat;
  double lon;
};


// Coordenada codificada para transmissão.
// Exemplo:
//
// -31.3131703
//
// vira:
//
// -313131703
//
struct CoordenadaE7 {
  int32_t lat;
  int32_t lon;
};


// Coordenada convertida para um plano em metros.
struct PontoXY {
  double x;
  double y;
};


// Estado baseado SOMENTE na distância até a borda.
enum EstadoProximidade {
  LONGE,
  PERTO,
  MUITO_PERTO
};


// Resultado completo da análise da geofence.
struct ResultadoGeofence {

  bool valido;

  bool dentro;

  double distanciaBordaM;

  EstadoProximidade proximidade;
};


// ======================================================
// ARMAZENAMENTO DA CERCA
// ======================================================

Coordenada verticesGeo[MAX_VERTICES];

PontoXY verticesXY[MAX_VERTICES];

size_t quantidadeVertices = 0;

bool cercaValida = false;


// ======================================================
// PARÂMETROS DA PROJEÇÃO GEOGRÁFICA
// ======================================================
//
// Para calcular distâncias em metros, transformamos a
// pequena região da cerca em um plano local.
//
// Usamos parâmetros do elipsoide WGS84.
//

const double WGS84_A = 6378137.0;

const double WGS84_E2 = 6.69437999014e-3;

const double DEG_TO_RAD_D =
    0.017453292519943295;


// Latitude e longitude de referência da cerca.
double latReferenciaRad = 0.0;
double lonReferenciaRad = 0.0;

// Quantos metros correspondem a um radiano
// naquela latitude.
double metrosPorRadLat = 0.0;
double metrosPorRadLon = 0.0;


// ======================================================
// CODIFICAÇÃO / DECODIFICAÇÃO
// ======================================================

int32_t codificarGrausE7(double graus) {

  return (int32_t)llround(
      graus * 10000000.0
  );
}


double decodificarGrausE7(int32_t valor) {

  return ((double)valor) / 10000000.0;
}


CoordenadaE7 codificarCoordenada(
    const Coordenada &p
) {

  CoordenadaE7 resultado;

  resultado.lat = codificarGrausE7(p.lat);
  resultado.lon = codificarGrausE7(p.lon);

  return resultado;
}


Coordenada decodificarCoordenada(
    const CoordenadaE7 &p
) {

  Coordenada resultado;

  resultado.lat =
      decodificarGrausE7(p.lat);

  resultado.lon =
      decodificarGrausE7(p.lon);

  return resultado;
}


// ======================================================
// CONFIGURAÇÕES ALTERÁVEIS
// ======================================================

bool configurarFaixas(
    double muitoPertoM,
    double pertoM
) {

  // Não aceitamos valores negativos
  // nem "muito perto" maior que "perto".

  if (muitoPertoM < 0 ||
      pertoM < 0 ||
      muitoPertoM > pertoM) {

    return false;
  }

  limiteMuitoPertoM = muitoPertoM;
  limitePertoM = pertoM;

  return true;
}


void configurarIntervaloTelemetria(
    uint32_t intervaloMs
) {

  intervaloTelemetriaMs = intervaloMs;
}


// ======================================================
// CONVERSÃO LAT/LON -> METROS
// ======================================================

void prepararSistemaCoordenadas() {

  double somaLat = 0.0;
  double somaLon = 0.0;

  for (size_t i = 0;
       i < quantidadeVertices;
       i++) {

    somaLat += verticesGeo[i].lat;
    somaLon += verticesGeo[i].lon;
  }

  // Usamos aproximadamente o centro da cerca
  // como origem do sistema local.

  double latReferencia =
      somaLat / quantidadeVertices;

  double lonReferencia =
      somaLon / quantidadeVertices;


  latReferenciaRad =
      latReferencia * DEG_TO_RAD_D;

  lonReferenciaRad =
      lonReferencia * DEG_TO_RAD_D;


  // -----------------------------
  // WGS84
  // -----------------------------

  double seno =
      sin(latReferenciaRad);

  double denominador =
      sqrt(
          1.0 -
          WGS84_E2 * seno * seno
      );


  // Raio de curvatura meridional
  double M =
      WGS84_A *
      (1.0 - WGS84_E2) /
      (denominador *
       denominador *
       denominador);


  // Raio de curvatura normal
  double N =
      WGS84_A /
      denominador;


  metrosPorRadLat = M;

  metrosPorRadLon =
      N * cos(latReferenciaRad);
}


// ------------------------------------------------------

PontoXY converterParaXY(
    const Coordenada &p
) {

  double latRad =
      p.lat * DEG_TO_RAD_D;

  double lonRad =
      p.lon * DEG_TO_RAD_D;


  PontoXY resultado;

  resultado.x =
      (lonRad - lonReferenciaRad) *
      metrosPorRadLon;

  resultado.y =
      (latRad - latReferenciaRad) *
      metrosPorRadLat;

  return resultado;
}


// ======================================================
// CARREGAMENTO DA CERCA
// ======================================================

bool carregarCercaCodificada(
    const CoordenadaE7 pontos[],
    size_t quantidade
) {

  // Um polígono precisa de pelo menos 3 pontos.

  if (quantidade < 3 ||
      quantidade > MAX_VERTICES) {

    cercaValida = false;

    return false;
  }


  quantidadeVertices = quantidade;


  // Decodifica todos os vértices.

  for (size_t i = 0;
       i < quantidadeVertices;
       i++) {

    verticesGeo[i] =
        decodificarCoordenada(
            pontos[i]
        );
  }


  // Define nosso sistema local em metros.

  prepararSistemaCoordenadas();


  // Converte os vértices apenas uma vez.
  //
  // Isso evita repetir esse cálculo
  // a cada nova posição do GPS.

  for (size_t i = 0;
       i < quantidadeVertices;
       i++) {

    verticesXY[i] =
        converterParaXY(
            verticesGeo[i]
        );
  }


  cercaValida = true;

  return true;
}


// ======================================================
// POINT IN POLYGON - RAY CASTING
// ======================================================

bool pontoDentroPoligono(
    const PontoXY &p
) {

  bool dentro = false;


  // j começa no último ponto,
  // permitindo testar a aresta:
  //
  // último -> primeiro

  size_t j =
      quantidadeVertices - 1;


  for (size_t i = 0;
       i < quantidadeVertices;
       i++) {


    const PontoXY &pi =
        verticesXY[i];

    const PontoXY &pj =
        verticesXY[j];


    // Verifica se uma linha horizontal
    // partindo do ponto cruza esta aresta.

    bool cruza =
        ((pi.y > p.y) !=
         (pj.y > p.y))
        &&
        (
          p.x <
          (pj.x - pi.x) *
          (p.y - pi.y) /
          (pj.y - pi.y)
          +
          pi.x
        );


    if (cruza) {

      dentro = !dentro;
    }


    j = i;
  }


  return dentro;
}


// ======================================================
// DISTÂNCIA PONTO -> SEGMENTO
// ======================================================

double distanciaPontoSegmento(
    const PontoXY &p,
    const PontoXY &a,
    const PontoXY &b
) {

  double dx =
      b.x - a.x;

  double dy =
      b.y - a.y;


  double comprimentoQuadrado =
      dx * dx +
      dy * dy;


  // Caso dois vértices sejam iguais.

  if (comprimentoQuadrado == 0.0) {

    return hypot(
        p.x - a.x,
        p.y - a.y
    );
  }


  // Descobre onde está a projeção
  // perpendicular do ponto sobre
  // a linha da aresta.

  double t =
      (
        (p.x - a.x) * dx +
        (p.y - a.y) * dy
      )
      /
      comprimentoQuadrado;


  // Limitamos ao próprio segmento.

  if (t < 0.0) {
    t = 0.0;
  }

  if (t > 1.0) {
    t = 1.0;
  }


  // Ponto mais próximo dentro da aresta.

  double xMaisProximo =
      a.x + t * dx;

  double yMaisProximo =
      a.y + t * dy;


  return hypot(
      p.x - xMaisProximo,
      p.y - yMaisProximo
  );
}


// ======================================================
// DISTÂNCIA ATÉ A BORDA MAIS PRÓXIMA
// ======================================================

double distanciaAteBorda(
    const PontoXY &p
) {

  double menorDistancia =
      1.0e30;


  for (size_t i = 0;
       i < quantidadeVertices;
       i++) {


    size_t proximo =
        (i + 1) %
        quantidadeVertices;


    double distancia =
        distanciaPontoSegmento(
            p,
            verticesXY[i],
            verticesXY[proximo]
        );


    if (distancia < menorDistancia) {

      menorDistancia =
          distancia;
    }
  }


  return menorDistancia;
}


// ======================================================
// CLASSIFICAÇÃO DA PROXIMIDADE
// ======================================================

EstadoProximidade classificarProximidade(
    double distanciaM
) {

  if (distanciaM <=
      limiteMuitoPertoM) {

    return MUITO_PERTO;
  }


  if (distanciaM <=
      limitePertoM) {

    return PERTO;
  }


  return LONGE;
}


// ======================================================
// ANÁLISE COMPLETA DA GEOFENCE
// ======================================================

ResultadoGeofence analisarGeofence(
    const Coordenada &posicao
) {

  ResultadoGeofence resultado;

  resultado.valido = false;
  resultado.dentro = false;
  resultado.distanciaBordaM = 0;
  resultado.proximidade = LONGE;


  if (!cercaValida) {

    return resultado;
  }


  PontoXY p =
      converterParaXY(posicao);


  resultado.distanciaBordaM =
      distanciaAteBorda(p);


  resultado.dentro =
      pontoDentroPoligono(p);


  // Caso esteja praticamente sobre a borda,
  // consideramos "dentro".
  //
  // Isto resolve pequenas ambiguidades
  // matemáticas do Ray Casting.

  if (resultado.distanciaBordaM < 0.05) {

    resultado.dentro = true;
  }


  resultado.proximidade =
      classificarProximidade(
          resultado.distanciaBordaM
      );


  resultado.valido = true;

  return resultado;
}


// ======================================================
// ATUADORES
// ======================================================
//
// São apenas funções de interface.
// O hardware real será colocado aqui depois.
//

void ativarBuzzer() {

  // TODO:
  // implementar buzzer real

  Serial.println(
      "[ATUADOR] Buzzer LIGADO"
  );
}


void desativarBuzzer() {

  // TODO

  Serial.println(
      "[ATUADOR] Buzzer DESLIGADO"
  );
}


void ativarVibracao() {

  // TODO:
  // motor de vibração ou motor
  // usado na prova de conceito

  Serial.println(
      "[ATUADOR] Vibracao LIGADA"
  );
}


void desativarVibracao() {

  // TODO

  Serial.println(
      "[ATUADOR] Vibracao DESLIGADA"
  );
}


// ======================================================
// EVITA REACIONAR O MESMO ATUADOR TODA HORA
// ======================================================

enum ModoAtuacao {

  ATUADORES_DESLIGADOS,

  SOMENTE_BUZZER,

  BUZZER_E_VIBRACAO
};


ModoAtuacao modoAtual =
    ATUADORES_DESLIGADOS;


// ------------------------------------------------------

void aplicarAtuadores(
    const ResultadoGeofence &resultado
) {

  if (!resultado.valido) {

    return;
  }


  ModoAtuacao novoModo;


  // Fora da cerca é tratado como
  // situação crítica.

  if (!resultado.dentro) {

    novoModo =
        BUZZER_E_VIBRACAO;
  }

  else if (
      resultado.proximidade ==
      MUITO_PERTO
  ) {

    novoModo =
        BUZZER_E_VIBRACAO;
  }

  else if (
      resultado.proximidade ==
      PERTO
  ) {

    novoModo =
        SOMENTE_BUZZER;
  }

  else {

    novoModo =
        ATUADORES_DESLIGADOS;
  }


  // Não faz nada se o estado
  // não mudou.

  if (novoModo == modoAtual) {

    return;
  }


  // Primeiro desliga tudo.

  desativarBuzzer();
  desativarVibracao();


  // Depois aplica o novo estado.

  if (novoModo ==
      SOMENTE_BUZZER) {

    ativarBuzzer();
  }


  else if (
      novoModo ==
      BUZZER_E_VIBRACAO
  ) {

    ativarBuzzer();
    ativarVibracao();
  }


  modoAtual = novoModo;
}


// ======================================================
// NOMES PARA DEBUG
// ======================================================

const char *nomeProximidade(
    EstadoProximidade estado
) {

  switch (estado) {

    case MUITO_PERTO:
      return "MUITO PERTO";

    case PERTO:
      return "PERTO";

    default:
      return "LONGE";
  }
}


// ======================================================
// POSIÇÃO ATUAL
// ======================================================

Coordenada posicaoAtual;

ResultadoGeofence resultadoAtual;

bool possuiPosicao = false;


// ======================================================
// TELEMETRIA
// ======================================================

void mostrarTelemetria() {

  if (!possuiPosicao) {

    Serial.println(
        "Sem posicao valida."
    );

    return;
  }


  CoordenadaE7 codificada =
      codificarCoordenada(
          posicaoAtual
      );


  Serial.println();
  Serial.println(
      "================================"
  );

  Serial.println(
      "       VFENCE TELEMETRIA"
  );

  Serial.println(
      "================================"
  );


  Serial.print("Latitude:  ");

  Serial.println(
      posicaoAtual.lat,
      7
  );


  Serial.print("Longitude: ");

  Serial.println(
      posicaoAtual.lon,
      7
  );


  Serial.println();


  Serial.print("Latitude E7:  ");

  Serial.println(
      codificada.lat
  );


  Serial.print("Longitude E7: ");

  Serial.println(
      codificada.lon
  );


  Serial.println();


  Serial.print("Status: ");

  if (resultadoAtual.dentro) {

    Serial.println("DENTRO");
  }

  else {

    Serial.println("FORA");
  }


  Serial.print(
      "Distancia da borda: "
  );

  Serial.print(
      resultadoAtual.distanciaBordaM,
      2
  );

  Serial.println(" m");


  Serial.print(
      "Proximidade: "
  );

  Serial.println(
      nomeProximidade(
          resultadoAtual.proximidade
      )
  );


  Serial.print("Satelites: ");

  Serial.println(
      gps.satellites.value()
  );


  if (gps.hdop.isValid()) {

    Serial.print("HDOP: ");

    Serial.println(
        gps.hdop.hdop()
    );
  }


  Serial.println(
      "================================"
  );


  // FUTURAMENTE:
  //
  // enviarTelemetriaLoRa(
  //     codificada,
  //     resultadoAtual
  // );
}


// ======================================================
// CERCA TEMPORÁRIA PARA TESTES
// ======================================================
//
// Futuramente este array desaparecerá.
//
// Estes valores serão recebidos da aplicação
// através do LoRa.
//
// Os pontos precisam estar em ordem ao redor
// do perímetro:
//
// A -> B -> C -> D -> ...
//

const CoordenadaE7 cercaTeste[] = {

  { -313131703, -540868848 },

  { -313131703, -540867535 },

  { -313133945, -540867535 },

  { -313133945, -540868848 }

};


const size_t quantidadeTeste =
    sizeof(cercaTeste) /
    sizeof(cercaTeste[0]);


// ======================================================
// SETUP
// ======================================================

void setup() {

  Serial.begin(115200);

  delay(1500);


  GPSserial.begin(
      GPS_BAUD,
      SERIAL_8N1,
      GPS_RX,
      -1
  );


  // Configuração inicial.
  //
  // Amanhã poderia ser:
  //
  // configurarFaixas(20, 50);

  configurarFaixas(
      10.0,
      30.0
  );


  // 10 segundos

  configurarIntervaloTelemetria(
      10000
  );


  Serial.println();
  Serial.println(
      "================================"
  );

  Serial.println(
      "          VFENCE"
  );

  Serial.println(
      "================================"
  );


  if (
    carregarCercaCodificada(
        cercaTeste,
        quantidadeTeste
    )
  ) {

    Serial.print(
        "Cerca carregada. Vertices: "
    );

    Serial.println(
        quantidadeVertices
    );
  }

  else {

    Serial.println(
        "ERRO AO CARREGAR CERCA!"
    );
  }


  Serial.println(
      "Aguardando GPS..."
  );
}


// ======================================================
// LOOP
// ======================================================

void loop() {

  // --------------------------------------------------
  // Recepção contínua do GPS
  // --------------------------------------------------

  while (
    GPSserial.available() > 0
  ) {

    gps.encode(
        GPSserial.read()
    );
  }


  // --------------------------------------------------
  // Sempre que chegar uma NOVA posição GPS
  // fazemos a análise da cerca.
  //
  // Isso é independente do intervalo LoRa.
  // --------------------------------------------------

  if (
    gps.location.isUpdated() &&
    gps.location.isValid()
  ) {

    posicaoAtual.lat =
        gps.location.lat();

    posicaoAtual.lon =
        gps.location.lng();


    resultadoAtual =
        analisarGeofence(
            posicaoAtual
        );


    possuiPosicao = true;


    // Atua imediatamente.

    aplicarAtuadores(
        resultadoAtual
    );
  }


  // --------------------------------------------------
  // TELEMETRIA
  //
  // Isso pode ser muito mais lento que
  // a análise da geofence.
  // --------------------------------------------------

  if (
    millis() -
    ultimoEnvioTelemetria
    >=
    intervaloTelemetriaMs
  ) {

    ultimoEnvioTelemetria =
        millis();


    mostrarTelemetria();
  }
}
