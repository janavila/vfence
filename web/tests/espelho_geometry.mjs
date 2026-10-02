/* Roda os casos de tests/casos_geometricos.json contra frontend/js/geometry.js
   e imprime o resultado em JSON, para o pytest comparar com o do Python.

   Executado por tests/test_geometry_js.py. Node serve só de ferramenta de
   teste: o navegador roda este MESMO arquivo .js sem nenhuma ferramenta. */

import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const aqui = dirname(fileURLToPath(import.meta.url));

/* O navegador carrega geometry.js como módulo ES, por causa do
   <script type="module">. O Node, sem um package.json dizendo
   "type": "module", trata .js como CommonJS e recusa o `export`.

   Copiamos o arquivo para um .mjs temporário em vez de acrescentar um
   package.json ao frontend: o projeto não deve ganhar artefato de Node
   nenhum por causa de uma ferramenta de teste. O conteúdo é o mesmo
   byte a byte, então é exatamente o arquivo do navegador que roda aqui. */
const origem = join(aqui, '..', 'frontend', 'js', 'geometry.js');
const pastaTemporaria = mkdtempSync(join(tmpdir(), 'vfence-espelho-'));
const copia = join(pastaTemporaria, 'geometry.mjs');
writeFileSync(copia, readFileSync(origem));
const geo = await import(pathToFileURL(copia).href);

const dados = JSON.parse(readFileSync(join(aqui, 'casos_geometricos.json'), 'utf8'));
const ref = dados._base_de_referencia;

const config = {
  base_lat: ref.base_lat,
  base_lon: ref.base_lon,
  base_max_radius_km: ref.base_raio_max_km,
  lora_range_m: ref.lora_alcance_m,
  gnss_expected_error_m: ref.gnss_erro_esperado_m,
  gnss_safety_factor: ref.gnss_fator_seguranca,
  recommended_critical_margin_m: ref.gnss_erro_esperado_m * ref.gnss_fator_seguranca,
};

const saida = { casos: {}, conferencias: {} };

// A conferência obrigatória da seção 5.3, sem passar por projeção.
const quadrado = [
  { x: 0, y: 0 }, { x: 0, y: 1 }, { x: 1, y: 1 }, { x: 1, y: 0 },
];
saida.conferencias.quadrado_area_com_sinal = geo.areaComSinal(quadrado);
saida.conferencias.quadrado_sentido = geo.sentido(quadrado);
saida.conferencias.quadrado_perimetro = geo.perimetroM(quadrado);

// O arredondamento da forma canônica.
saida.conferencias.micrograus = [
  geo.paraMicrograus(-31.306),
  geo.paraMicrograus(0.0000005),
  geo.paraMicrograus(-0.0000005),
];

for (const caso of dados.casos) {
  if (!caso.points) continue;
  const resultado = geo.validarCerca(
    caso.points, caso.margin_attention_m, caso.margin_critical_m, config,
  );
  saida.casos[caso.nome] = {
    valid: resultado.valid,
    errors: [...new Set(resultado.violations.filter((v) => v.severity === 'error').map((v) => v.rule))].sort(),
    warnings: [...new Set(resultado.violations.filter((v) => v.severity === 'warning').map((v) => v.rule))].sort(),
    orientation: resultado.orientation,
    area_ha: resultado.area_ha,
    perimeter_m: resultado.perimeter_m,
    edges: resultado.violations.flatMap((v) => v.edges),
    points: resultado.violations.flatMap((v) => v.points),
    messages: resultado.violations.map((v) => v.message),
  };
}

// As faixas de margem (item SHOULD da seção 11.3).
const cercaBoa = dados.casos.find((c) => c.nome === 'cerca_boa_sem_nenhum_aviso').points;
const faixa = geo.faixaParaDentro(cercaBoa, 8.0);
saida.conferencias.faixa_cerca_boa = faixa ? faixa.length : null;
saida.conferencias.faixa_area_menor = faixa
  ? geo.areaHa(geo.projetarTodos(faixa)) < geo.areaHa(geo.projetarTodos(cercaBoa))
  : null;
const triangulo = dados.casos.find((c) => c.nome === 'triangulo_muito_pequeno').points;
saida.conferencias.faixa_triangulo_pequeno = geo.faixaParaDentro(triangulo, 8.0);

process.stdout.write(JSON.stringify(saida));
