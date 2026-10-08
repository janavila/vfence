/* ==========================================================================
   geometry.js — espelho de geometry.py, validation.py e canonical.py.

   Por que existe uma cópia: o produtor precisa ver o problema ENQUANTO
   desenha. Esperar o servidor a cada clique seria lento e não
   funcionaria com a conexão ruim que a seção 11.1 trata como normal.

   Mas quem DECIDE é sempre o servidor. Meio segundo depois que o
   produtor para de mexer, o editor confirma com
   POST /api/fences/validate; discordando, vale a resposta do servidor.

   As duas implementações são mantidas iguais por
   `tests/casos_geometricos.json` e por `tests/test_geometry_js.py`, que
   roda os mesmos casos nos dois lados. Mexeu aqui, mexa no Python —
   o raciocínio completo de cada regra está lá.
   ========================================================================== */

/* Raio médio da Terra, o MESMO valor do servidor. */
export const EARTH_RADIUS_M = 6371008.8;

/* Tolerância para comparar com zero. Trabalhamos em metros. */
const EPSILON = 1e-9;

export const MIN_POINTS = 3;
export const MAX_POINTS = 32;
const MIN_POINT_DISTANCE_M = 1.0;

/* ---- Forma canônica: o arredondamento ---- */

/**
 * Grau para microgradus, arredondando meio para LONGE do zero.
 *
 * Espelha `to_e6()`. O `Math.round` do JavaScript arredonda meio para
 * CIMA (−0,5 vira −0), o que diverge do servidor em números negativos —
 * e todas as nossas coordenadas são negativas. Daí o `Math.abs` com o
 * sinal reaplicado.
 */
export function paraMicrograus(graus) {
  return Math.sign(graus) * Math.round(Math.abs(graus) * 1e6);
}

/**
 * Arredonda os pontos para a precisão que vai de fato para a coleira.
 *
 * O servidor faz o mesmo antes de medir, para a área e o perímetro na
 * tela serem os da cerca que a coleira recebe.
 */
export function canonizar(pontos) {
  return pontos.map((p) => ({
    lat: paraMicrograus(p.lat) / 1e6,
    lon: paraMicrograus(p.lon) / 1e6,
  }));
}

/* ---- Projeção local ---- */

/**
 * Projeção local, espelho de `LocalProjection`:
 *   x = rad(lon − lon0) · R · cos(rad(lat0))   leste
 *   y = rad(lat − lat0) · R                    norte
 */
export function criarProjecao(pontos) {
  const lat0 = pontos.reduce((soma, p) => soma + p.lat, 0) / pontos.length;
  const lon0 = pontos.reduce((soma, p) => soma + p.lon, 0) / pontos.length;
  const escalaLon = EARTH_RADIUS_M * Math.cos((lat0 * Math.PI) / 180);
  return {
    lat0,
    lon0,
    projetar(ponto) {
      return {
        x: ((ponto.lon - lon0) * Math.PI / 180) * escalaLon,
        y: ((ponto.lat - lat0) * Math.PI / 180) * EARTH_RADIUS_M,
      };
    },
  };
}

/** Projeta todos os pontos de uma vez. */
export function projetarTodos(pontos) {
  if (!pontos.length) return [];
  const projecao = criarProjecao(pontos);
  return pontos.map((p) => projecao.projetar(p));
}

/**
 * Distância entre dois pontos quaisquer da Terra (haversine).
 *
 * Usada para medir até a Base (VAL-03 e VAL-10), onde a projeção local
 * centrada na cerca perderia precisão.
 */
export function distanciaHaversine(a, b) {
  const rad = Math.PI / 180;
  const lat1 = a.lat * rad;
  const lat2 = b.lat * rad;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const interno =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(interno)));
}

/* ---- Área, perímetro e sentido ---- */

/**
 * Área COM SINAL pela fórmula do laço. Negativo = horário.
 * Conferência da seção 5.3: o quadrado (0,0)→(0,1)→(1,1)→(1,0) dá −1.
 */
export function areaComSinal(plano) {
  if (plano.length < 3) return 0;
  let total = 0;
  for (let i = 0; i < plano.length; i += 1) {
    const atual = plano[i];
    const seguinte = plano[(i + 1) % plano.length];
    total += atual.x * seguinte.y - seguinte.x * atual.y;
  }
  return total / 2;
}

export function areaM2(plano) {
  return Math.abs(areaComSinal(plano));
}

/** Área em hectares — a unidade do produtor. 1 ha = 10.000 m². */
export function areaHa(plano) {
  return areaM2(plano) / 10000;
}

/** Comprimento de cada lado. O último é o lado que fecha, de pn para p1. */
export function comprimentosDosLados(plano) {
  if (plano.length < 2) return [];
  return plano.map((atual, i) => {
    const seguinte = plano[(i + 1) % plano.length];
    return Math.hypot(seguinte.x - atual.x, seguinte.y - atual.y);
  });
}

export function perimetroM(plano) {
  return comprimentosDosLados(plano).reduce((soma, lado) => soma + lado, 0);
}

/** "clockwise", "counterclockwise", ou null quando não há giro. */
export function sentido(plano) {
  if (plano.length < 3) return null;
  const comSinal = areaComSinal(plano);
  if (Math.abs(comSinal) <= EPSILON) return null;
  return comSinal < 0 ? 'clockwise' : 'counterclockwise';
}

/* ---- Lados que se cruzam ---- */

function produtoVetorial(origem, a, b) {
  return (a.x - origem.x) * (b.y - origem.y) - (a.y - origem.y) * (b.x - origem.x);
}

function sinal(valor) {
  if (valor > EPSILON) return 1;
  if (valor < -EPSILON) return -1;
  return 0;
}

function dentroDaCaixa(ponto, inicio, fim) {
  return (
    ponto.x >= Math.min(inicio.x, fim.x) - EPSILON &&
    ponto.x <= Math.max(inicio.x, fim.x) + EPSILON &&
    ponto.y >= Math.min(inicio.y, fim.y) - EPSILON &&
    ponto.y <= Math.max(inicio.y, fim.y) + EPSILON
  );
}

/** Dois segmentos se cruzam ou se sobrepõem? */
export function segmentosSeCruzam(a1, a2, b1, b2) {
  const d1 = sinal(produtoVetorial(a1, a2, b1));
  const d2 = sinal(produtoVetorial(a1, a2, b2));
  const d3 = sinal(produtoVetorial(b1, b2, a1));
  const d4 = sinal(produtoVetorial(b1, b2, a2));

  if (d1 * d2 < 0 && d3 * d4 < 0) return true;

  // Casos colineares ou de ponta encostando no outro segmento.
  if (d1 === 0 && dentroDaCaixa(b1, a1, a2)) return true;
  if (d2 === 0 && dentroDaCaixa(b2, a1, a2)) return true;
  if (d3 === 0 && dentroDaCaixa(a1, b1, b2)) return true;
  if (d4 === 0 && dentroDaCaixa(a2, b1, b2)) return true;
  return false;
}

/**
 * Pares de lados que se cruzam, numerados a partir de 1. Só compara
 * lados NÃO adjacentes; com 32 pontos são 464 comparações.
 */
export function ladosQueSeCruzam(plano) {
  const n = plano.length;
  if (n < 4) return [];
  const encontrados = [];
  for (let i = 0; i < n; i += 1) {
    for (let j = i + 1; j < n; j += 1) {
      const vizinhos = j === i + 1 || (i === 0 && j === n - 1);
      if (vizinhos) continue;
      if (segmentosSeCruzam(plano[i], plano[(i + 1) % n], plano[j], plano[(j + 1) % n])) {
        encontrados.push([i + 1, j + 1]);
      }
    }
  }
  return encontrados;
}

/* ---- Distância ponto → lado ---- */

/** Menor distância de um ponto a um SEGMENTO (não à reta infinita). */
export function distanciaPontoLado(ponto, inicio, fim) {
  const dx = fim.x - inicio.x;
  const dy = fim.y - inicio.y;
  const comprimento = dx * dx + dy * dy;
  if (comprimento <= EPSILON) return Math.hypot(ponto.x - inicio.x, ponto.y - inicio.y);
  let t = ((ponto.x - inicio.x) * dx + (ponto.y - inicio.y) * dy) / comprimento;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(ponto.x - (inicio.x + t * dx), ponto.y - (inicio.y + t * dy));
}

/** Distância de um ponto a cada lado que NÃO o contém. */
export function distanciasAosLadosNaoAdjacentes(plano, indice) {
  const n = plano.length;
  const ponto = plano[indice];
  const ladoAnterior = (indice - 1 + n) % n;
  const distancias = [];
  for (let lado = 0; lado < n; lado += 1) {
    if (lado === indice || lado === ladoAnterior) continue;
    distancias.push(distanciaPontoLado(ponto, plano[lado], plano[(lado + 1) % n]));
  }
  return distancias;
}

/**
 * O ponto está dentro do polígono? (*ray casting*)
 *
 * Conta quantas bordas um raio horizontal atravessa: ímpar = dentro.
 * É a única conta da geometria que NÃO depende do sentido do desenho.
 * Sem contraparte no servidor, que não desenha faixas; no firmware é o
 * mesmo método que decide se o animal está dentro.
 */
export function estaDentro(ponto, plano) {
  let dentro = false;
  const n = plano.length;
  for (let i = 0; i < n; i += 1) {
    const a = plano[i];
    const b = plano[(i + 1) % n];
    if ((a.y > ponto.y) !== (b.y > ponto.y)) {
      const corte = a.x + ((ponto.y - a.y) / (b.y - a.y)) * (b.x - a.x);
      if (ponto.x < corte) dentro = !dentro;
    }
  }
  return dentro;
}

/* ---- As regras VAL ---- */

const ERRO = 'error';
const AVISO = 'warning';

/** Formata número para o produtor: vírgula decimal, sem zero sobrando. */
export function numero(valor, casas = 1) {
  const texto = valor.toFixed(casas).replace(/0+$/, '').replace(/\.$/, '');
  return (texto || '0').replace('.', ',');
}

function violacao(rule, severity, message, pontos = [], lados = []) {
  return { rule, severity, message, points: pontos, edges: lados };
}

/** A regra VAL-09, isolada porque a especificação a marca como provisória. */
export function val09CercaPequena(plano, margemAtencao) {
  if (margemAtencao <= 0) return false;
  if (areaM2(plano) < margemAtencao ** 2 * 4) return true;
  for (let i = 0; i < plano.length; i += 1) {
    const distancias = distanciasAosLadosNaoAdjacentes(plano, i);
    if (distancias.length && Math.max(...distancias) < margemAtencao) return true;
  }
  return false;
}

/**
 * Aplica todas as regras. Espelha `validate_fence()` do servidor.
 *
 * @param {{lat:number,lon:number}[]} pontosOriginais
 * @param {number} margemAtencao  dA em metros
 * @param {number} margemCritica  dC em metros
 * @param {object} config  o que veio de GET /api/settings
 * @returns {{valid:boolean, violations:object[], area_ha:number,
 *            perimeter_m:number, orientation:string|null}}
 */
export function validarCerca(pontosOriginais, margemAtencao, margemCritica, config) {
  const violacoes = [];

  // VAL-08 — margens. Não depende da geometria, então vem antes do corte.
  if (!(margemCritica > 0 && margemCritica < margemAtencao)) {
    violacoes.push(violacao('VAL-08', ERRO,
      'A margem crítica precisa ser maior que zero e menor que a margem de atenção.'));
  }

  // VAL-01 — quantidade de pontos.
  const quantidade = pontosOriginais.length;
  if (quantidade < MIN_POINTS || quantidade > MAX_POINTS) {
    violacoes.push(violacao('VAL-01', ERRO,
      `A cerca precisa ter de ${MIN_POINTS} a ${MAX_POINTS} pontos. Agora tem ${quantidade}.`));
    return resultado(violacoes, 0, 0, null);
  }

  // VAL-02 — coordenadas possíveis.
  const invalidos = [];
  pontosOriginais.forEach((p, i) => {
    if (!(p.lat >= -90 && p.lat <= 90 && p.lon >= -180 && p.lon <= 180)) invalidos.push(i + 1);
  });
  if (invalidos.length) {
    const texto = invalidos.length === 1
      ? `O ponto ${invalidos[0]} tem coordenadas inválidas.`
      : `Os pontos ${invalidos.join(', ')} têm coordenadas inválidas.`;
    violacoes.push(violacao('VAL-02', ERRO, texto, invalidos));
    return resultado(violacoes, 0, 0, null);
  }

  // Daqui para baixo, trabalhamos com os pontos já arredondados.
  const pontos = canonizar(pontosOriginais);
  const plano = projetarTodos(pontos);
  const orientacao = sentido(plano);
  const lados = comprimentosDosLados(plano);
  const base = { lat: config.base_lat, lon: config.base_lon };

  // VAL-03 — distância até a Base.
  const medidos = pontos.map((p, i) => [i + 1, distanciaHaversine(p, base)]);
  const longeDemais = medidos.filter(([, metros]) => metros > config.base_max_radius_km * 1000);
  if (longeDemais.length) {
    const [indice, metros] = longeDemais[0];
    violacoes.push(violacao('VAL-03', ERRO,
      `O ponto ${indice} está a ${numero(metros / 1000)} km da Base. ` +
      'Confira se latitude e longitude não estão trocadas.',
      longeDemais.map(([i]) => i)));
  }

  // VAL-04 — pontos praticamente no mesmo lugar.
  const colados = [];
  lados.forEach((comprimento, i) => {
    if (comprimento < MIN_POINT_DISTANCE_M) {
      colados.push([i + 1, ((i + 1) % quantidade) + 1]);
    }
  });
  if (colados.length) {
    const [a, b] = colados[0];
    violacoes.push(violacao('VAL-04', ERRO,
      `Os pontos ${a} e ${b} estão praticamente no mesmo lugar. Apague um deles.`,
      [...new Set(colados.flat())].sort((x, y) => x - y), colados));
  }

  // VAL-05 antes de VAL-06: o sentido de uma cerca com lados cruzados é
  // indefinido (área com sinal zero), então não acusamos "linha reta".
  const cruzamentos = ladosQueSeCruzam(plano);
  if (cruzamentos.length) {
    violacoes.push(violacao('VAL-05', ERRO,
      'Dois lados da cerca se cruzam (destacados em vermelho). Apague ou mova um dos pontos.',
      [], cruzamentos));
  } else if (orientacao !== 'clockwise') {
    violacoes.push(violacao('VAL-06', ERRO, orientacao === null
      ? 'Os pontos estão todos em linha reta e não formam uma área. Mova um dos pontos para fora da linha.'
      : 'Os pontos foram marcados no sentido anti-horário. Marque seguindo o sentido dos ' +
        'ponteiros do relógio, ou use o botão Inverter ordem.'));
  }

  // VAL-07 — lado menor que o erro do GPS.
  lados.forEach((comprimento, i) => {
    if (comprimento >= config.gnss_expected_error_m || comprimento < MIN_POINT_DISTANCE_M) return;
    const a = i + 1;
    const b = ((i + 1) % quantidade) + 1;
    violacoes.push(violacao('VAL-07', AVISO,
      `O lado entre os pontos ${a} e ${b} tem ${numero(comprimento)} m, menos que o erro do GPS ` +
      `(~${numero(config.gnss_expected_error_m)} m). Nessa parte a coleira pode se confundir.`,
      [a, b], [[a, b]]));
  });

  // VAL-09 — cerca pequena para a margem.
  if (val09CercaPequena(plano, margemAtencao)) {
    violacoes.push(violacao('VAL-09', AVISO,
      'A cerca é pequena para a margem de atenção escolhida: o animal pode receber aviso ' +
      'em quase toda a área.'));
  }

  // VAL-10 — além do alcance do rádio.
  const foraDeAlcance = medidos.filter(([, metros]) => metros > config.lora_range_m);
  if (foraDeAlcance.length) {
    const [indice, metros] = foraDeAlcance.reduce((a, b) => (b[1] > a[1] ? b : a));
    violacoes.push(violacao('VAL-10', AVISO,
      `O ponto ${indice} está a ${numero(metros)} m da Base, além do alcance testado do rádio ` +
      `(~${numero(config.lora_range_m)} m). A cerca funciona, mas a atualização e os dados ` +
      'dessa região podem falhar.',
      foraDeAlcance.map(([i]) => i)));
  }

  // VAL-12 — margem crítica abaixo da folga recomendada pelo GPS.
  const recomendada = config.recommended_critical_margin_m;
  if (margemCritica < recomendada) {
    violacoes.push(violacao('VAL-12', AVISO,
      `A margem crítica (${numero(margemCritica)} m) é menor que a folga recomendada para o ` +
      `erro do GPS (${numero(recomendada)} m). Você pode enviar mesmo assim.`));
  }

  return resultado(violacoes, areaHa(plano), perimetroM(plano), orientacao);
}

function resultado(violacoes, area, perimetro, orientacao) {
  return {
    valid: !violacoes.some((v) => v.severity === ERRO),
    violations: violacoes,
    area_ha: area,
    perimeter_m: perimetro,
    orientation: orientacao,
  };
}

/* ---- Faixas de atenção e crítica no mapa (seção 11.3, item SHOULD) ---- */

/**
 * Contorno deslocado para DENTRO da cerca, a uma distância fixa.
 *
 * Mostra onde a coleira começa a avisar o animal. Método: deslocamento
 * pela bissetriz de cada canto. É aproximação e FALHA em cercas
 * estreitas; nesses casos devolve `null` e o editor explica, em vez de
 * desenhar uma faixa falsa.
 *
 * @returns {{lat:number,lon:number}[]|null}
 */
export function faixaParaDentro(pontos, distanciaM) {
  if (pontos.length < 3 || distanciaM <= 0) return null;
  const projecao = criarProjecao(pontos);
  const plano = pontos.map((p) => projecao.projetar(p));
  const n = plano.length;
  if (sentido(plano) !== 'clockwise') return null;

  const deslocado = [];
  for (let i = 0; i < n; i += 1) {
    const anterior = plano[(i - 1 + n) % n];
    const atual = plano[i];
    const seguinte = plano[(i + 1) % n];

    // Normal INTERNA de cada um dos dois lados que tocam este vértice.
    // Num polígono horário com x para leste e y para norte, o interior
    // fica à direita de quem caminha pela borda, e a normal interna de
    // (a→b) é (dy, −dx) normalizada. Conferência: no quadrado
    // (0,0)→(0,10)→(10,10)→(10,0), o lado p1→p2 aponta para o norte
    // (dx=0, dy=10) e o centro está a leste — e (dy,−dx)/10 = (1,0),
    // que é leste. O sinal trocado apontaria para fora.
    const normal = (a, b) => {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const comprimento = Math.hypot(dx, dy);
      if (comprimento <= EPSILON) return null;
      return { x: dy / comprimento, y: -dx / comprimento };
    };
    const n1 = normal(anterior, atual);
    const n2 = normal(atual, seguinte);
    if (!n1 || !n2) return null;

    // Bissetriz: soma das duas normais, normalizada.
    let bx = n1.x + n2.x;
    let by = n1.y + n2.y;
    const comprimento = Math.hypot(bx, by);
    if (comprimento <= EPSILON) return null;  // lados opostos: canto de 180°
    bx /= comprimento;
    by /= comprimento;

    // Quanto mais fechado o canto, mais longe o ponto deslocado fica.
    const cosMetade = Math.max(0.2, (1 + (n1.x * n2.x + n1.y * n2.y)) / 2) ** 0.5;
    const avanco = distanciaM / cosMetade;
    deslocado.push({ x: atual.x + bx * avanco, y: atual.y + by * avanco });
  }

  // O contorno deslocado é válido?
  //
  // Três conferências, e as três são necessárias. A terceira só foi
  // acrescentada depois de um teste falhar: num triângulo de 2 m com
  // margem de 8 m, o contorno deslocado passa do centro e reaparece do
  // outro lado — rodado 180°, o que PRESERVA o sentido horário e não
  // cria cruzamento nenhum. As duas primeiras conferências o aprovavam,
  // e o editor desenharia uma faixa que não existe.
  //
  // O que pega esse caso é exigir que todo vértice deslocado esteja
  // DENTRO da cerca original: uma faixa interna que sai da cerca não é
  // faixa interna.
  if (sentido(deslocado) !== 'clockwise') return null;
  if (ladosQueSeCruzam(deslocado).length) return null;
  if (!deslocado.every((vertice) => estaDentro(vertice, plano))) return null;
  if (areaM2(deslocado) >= areaM2(plano)) return null;

  const escalaLon = EARTH_RADIUS_M * Math.cos((projecao.lat0 * Math.PI) / 180);
  return deslocado.map((p) => ({
    lat: projecao.lat0 + (p.y / EARTH_RADIUS_M) * 180 / Math.PI,
    lon: projecao.lon0 + (p.x / escalaLon) * 180 / Math.PI,
  }));
}
