/* ==========================================================================
   editor.js — a tela principal do projeto (seção 11.3).

   Os cinco passos, que são a estrutura deste arquivo
   --------------------------------------------------
   1. Marcar pontos    clique a clique, sempre em sentido horário
   2. Conferir problemas  validação ao vivo, e confirmação no servidor;
                       na tela, são os avisos acima do mapa
   3. Margens          dA e dC, com as faixas desenhadas no mapa
   4. Revisar          nome, resumo, aviso de impacto
   5. Enviar e acompanhar  linha do tempo por coleira
   Na tela, os passos 1, 3, 4 e 5 ficam abaixo do mapa, numerados de 1 a 4.

   Duas decisões que explicam o resto
   ----------------------------------
   **Captura própria, clique a clique** (DEC-04), sem Leaflet.draw: não
   é só peso, é controle da ORDEM dos pontos. O sentido horário é
   obrigatório e o CRC depende dessa ordem; um plugin entregaria um
   polígono pronto, sem garantia de ordem nem numeração estável.

   **Validação em dois tempos.** A cada clique, `geometry.js` recalcula
   no navegador (instantâneo, sem rede). Meio segundo depois,
   confirmamos com POST /api/fences/validate — quem decide é o servidor
   (seção 5.4).

   Onde se conecta
   ---------------
   `api.js` para falar com o servidor, `geometry.js` para as contas,
   `map.js` para o mapa, `realtime.js` para o acompanhamento ao vivo da
   entrega, `ui.js` para o aviso de conexão.
   ========================================================================== */

import {
  criarCerca, explicarErro, lerCercaAtiva, lerConfiguracao, lerEntregas,
  validarNoServidor, enderecoDoLog,
} from './api.js';
import {
  MAX_POINTS, MIN_POINTS, faixaParaDentro, numero, validarCerca,
} from './geometry.js';
import {
  criarMapa, desenharAlcanceDoRadio, enquadrar, marcarBase, mostrarLocalizacao, paraLeaflet,
} from './map.js';
import { conectarTempoReal } from './realtime.js';
import { escapeHtml, formatClock } from './ui.js';

/* Meio segundo depois da última mexida, confirmamos no servidor.
   Curto o bastante para parecer imediato, longo o bastante para não
   disparar uma requisição por clique durante o desenho. */
const ESPERA_ANTES_DE_CONFIRMAR_MS = 500;

/* ---- Estado da tela ---- */

const estado = {
  config: null,
  mapa: null,
  pontos: [],            // [{lat, lon}] na ordem de desenho
  cercaAtiva: null,      // a que está valendo, desenhada em cinza
  resultadoLocal: null,  // do geometry.js
  resultadoServidor: null,
  avisosAceitos: false,
  enviando: false,
  versaoEnviada: null,
  temporizador: null,
};

/* Camadas do Leaflet que recriamos a cada mudança. */
const camadas = {
  marcadores: [],
  contorno: null,
  linhaGuia: null,
  ladosComProblema: [],
  pontosComProblema: [],
  faixaAtencao: null,
  faixaCritica: null,
  cercaAtiva: null,
};

const elemento = (id) => document.getElementById(id);

/* ---- Passo 1 — marcar pontos ---- */

function acrescentarPonto(lat, lon) {
  if (estado.pontos.length >= MAX_POINTS) {
    anunciar(`A cerca já tem o máximo de ${MAX_POINTS} pontos.`);
    return;
  }
  estado.pontos.push({ lat, lon });
  mudou();
}

function desfazerUltimo() {
  estado.pontos.pop();
  mudou();
}

function limparTudo() {
  if (estado.pontos.length && !window.confirm('Apagar todos os pontos marcados?')) return;
  estado.pontos = [];
  mudou();
}

/**
 * Inverte a ordem mantendo p1 no lugar: `p1, pn, …, p2`.
 *
 * Não é um `reverse()` simples porque o produtor pensa no primeiro
 * ponto como o começo da divisa: inverter tudo mudaria qual ponto é o
 * número 1 e embaralharia a numeração que ele acabou de ver no mapa.
 */
function inverterOrdem() {
  if (estado.pontos.length < 3) return;
  const [primeiro, ...resto] = estado.pontos;
  estado.pontos = [primeiro, ...resto.reverse()];
  mudou();
}

function apagarPonto(indice) {
  estado.pontos.splice(indice, 1);
  mudou();
}

function moverPonto(indice, lat, lon) {
  estado.pontos[indice] = { lat, lon };
  mudou();
}

/* ---- Desenho no mapa ---- */

function limparCamadas() {
  camadas.marcadores.forEach((m) => m.remove());
  camadas.marcadores = [];
  camadas.ladosComProblema.forEach((l) => l.remove());
  camadas.ladosComProblema = [];
  camadas.pontosComProblema.forEach((p) => p.remove());
  camadas.pontosComProblema = [];
  ['contorno', 'linhaGuia', 'faixaAtencao', 'faixaCritica'].forEach((nome) => {
    if (camadas[nome]) {
      camadas[nome].remove();
      camadas[nome] = null;
    }
  });
}

function iconeNumerado(numeroDoPonto, comProblema) {
  return L.divIcon({
    className: `fence-marker${comProblema ? ' problem' : ''}`,
    html: String(numeroDoPonto),
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

function redesenhar() {
  if (!estado.mapa) return;
  limparCamadas();

  const resultado = estado.resultadoLocal;
  const comErro = resultado ? resultado.violations.some((v) => v.severity === 'error') : false;
  const pontosDestacados = new Set(
    (resultado?.violations ?? []).flatMap((v) => v.points),
  );
  const ladosDestacados = (resultado?.violations ?? []).flatMap((v) => v.edges);

  // Faixas de margem (item SHOULD da seção 11.3): desenhadas por baixo
  // do contorno, para não esconder os pontos.
  if (estado.pontos.length >= MIN_POINTS && !comErro) {
    desenharFaixas();
  }

  // Contorno, sempre fechado a partir do 3º ponto (seção 11.3).
  if (estado.pontos.length >= 3) {
    camadas.contorno = L.polygon(paraLeaflet(estado.pontos), {
      color: comErro ? '#a34f4f' : '#245c4f',
      weight: 3,
      opacity: 0.95,
      fillColor: comErro ? '#a34f4f' : '#245c4f',
      fillOpacity: 0.12,
      interactive: false,
    }).addTo(estado.mapa);
  } else if (estado.pontos.length === 2) {
    camadas.linhaGuia = L.polyline(paraLeaflet(estado.pontos), {
      color: '#245c4f', weight: 3, dashArray: '6 6', interactive: false,
    }).addTo(estado.mapa);
  }

  // Lados com problema, por cima do contorno e em vermelho.
  const n = estado.pontos.length;
  ladosDestacados.forEach(([a, b]) => {
    const inicio = estado.pontos[a - 1];
    const fim = estado.pontos[b - 1] ?? estado.pontos[a % n];
    if (!inicio || !fim) return;
    camadas.ladosComProblema.push(
      L.polyline([[inicio.lat, inicio.lon], [fim.lat, fim.lon]], {
        color: '#a34f4f', weight: 6, opacity: 0.85, interactive: false,
      }).addTo(estado.mapa),
    );
  });

  // Marcadores numerados, arrastáveis para corrigir (item SHOULD da 11.3).
  estado.pontos.forEach((ponto, indice) => {
    const marcador = L.marker([ponto.lat, ponto.lon], {
      icon: iconeNumerado(indice + 1, pontosDestacados.has(indice + 1)),
      draggable: !estado.versaoEnviada,
      keyboard: false,
      title: `Ponto ${indice + 1} — arraste para corrigir`,
    }).addTo(estado.mapa);
    marcador.on('dragend', (evento) => {
      const { lat, lng } = evento.target.getLatLng();
      moverPonto(indice, lat, lng);
    });
    camadas.marcadores.push(marcador);
  });
}

function desenharFaixas() {
  const atencao = lerMargem('margin-attention');
  const critica = lerMargem('margin-critical');
  const aviso = elemento('band-note');

  const faixaA = faixaParaDentro(estado.pontos, atencao);
  const faixaC = faixaParaDentro(estado.pontos, critica);

  if (faixaA) {
    camadas.faixaAtencao = L.polygon(paraLeaflet(faixaA), {
      color: '#8a6410', weight: 1, dashArray: '4 4',
      fillColor: '#8a6410', fillOpacity: 0.07, interactive: false,
    }).addTo(estado.mapa);
  }
  if (faixaC) {
    camadas.faixaCritica = L.polygon(paraLeaflet(faixaC), {
      color: '#a0643f', weight: 1, dashArray: '4 4',
      fillColor: '#a0643f', fillOpacity: 0.07, interactive: false,
    }).addTo(estado.mapa);
  }
  if (aviso) {
    aviso.textContent = faixaA && faixaC
      ? 'As linhas pontilhadas mostram onde a coleira começa a avisar o animal.'
      : 'A cerca é estreita demais para desenhar as faixas das margens.';
  }
}

function desenharCercaAtiva() {
  if (!estado.mapa || !estado.cercaAtiva) return;
  if (camadas.cercaAtiva) camadas.cercaAtiva.remove();
  camadas.cercaAtiva = L.polygon(paraLeaflet(estado.cercaAtiva.points), {
    color: '#828d88', weight: 2, dashArray: '8 6',
    fillColor: '#828d88', fillOpacity: 0.06, interactive: false,
  }).addTo(estado.mapa);
  camadas.cercaAtiva.bindTooltip(
    `Cerca nº ${estado.cercaAtiva.version} (a que está valendo)`,
    { sticky: true },
  );
}

/* ---- Passo 2 — conferir problemas ---- */

function lerMargem(id) {
  const campo = elemento(id);
  const valor = Number.parseFloat(campo?.value ?? '');
  return Number.isFinite(valor) ? valor : 0;
}

/** Recalcula tudo no navegador e agenda a confirmação no servidor. */
function mudou() {
  estado.resultadoLocal = estado.pontos.length
    ? validarCerca(estado.pontos, lerMargem('margin-attention'), lerMargem('margin-critical'), estado.config)
    : null;
  estado.resultadoServidor = null;

  redesenhar();
  renderizar();

  window.clearTimeout(estado.temporizador);
  if (estado.pontos.length >= MIN_POINTS) {
    estado.temporizador = window.setTimeout(confirmarNoServidor, ESPERA_ANTES_DE_CONFIRMAR_MS);
  }
}

/** Confirma a validação no servidor, que é quem decide. */
async function confirmarNoServidor() {
  const marcaDaChamada = estado.pontos.length;
  try {
    const resposta = await validarNoServidor(montarCorpo());
    if (estado.pontos.length !== marcaDaChamada) return;  // o produtor já mexeu
    estado.resultadoServidor = resposta;
    renderizar();
  } catch (erro) {
    // Sem rede, continuamos com a validação do navegador: o produtor
    // segue desenhando e o envio é que vai precisar de conexão.
    const campo = elemento('server-check');
    if (campo) campo.textContent = explicarErro(erro);
  }
}

/* ---- Montagem do corpo da requisição ---- */

function montarCorpo() {
  return {
    name: (elemento('fence-name')?.value || '').trim() || 'Cerca sem nome',
    margin_attention_m: lerMargem('margin-attention'),
    margin_critical_m: lerMargem('margin-critical'),
    points: estado.pontos.map((p) => ({ lat: p.lat, lon: p.lon })),
    accept_warnings: estado.avisosAceitos,
  };
}

/* ---- Desenho dos avisos e dos passos ---- */

function renderizar() {
  const resultado = estado.resultadoServidor ?? estado.resultadoLocal;
  const quantidade = estado.pontos.length;
  // Com menos de 3 pontos, a falta de pontos (VAL-01) não conta como erro:
  // o produtor ainda está desenhando, e vermelho ali só assustaria.
  const erros = (resultado?.violations ?? []).filter((v) => v.severity === 'error'
    && (v.rule !== 'VAL-01' || quantidade >= MIN_POINTS));
  const avisos = (resultado?.violations ?? []).filter((v) => v.severity === 'warning');

  // --- contador, área e perímetro ao vivo ---------------------------
  elemento('point-count').textContent = `${quantidade} de ${MAX_POINTS}`;
  elemento('area-value').textContent = resultado && quantidade >= MIN_POINTS
    ? `${numero(resultado.area_ha, 4)} ha` : '—';
  elemento('perimeter-value').textContent = resultado && quantidade >= MIN_POINTS
    ? `${numero(resultado.perimeter_m)} m` : '—';

  // --- botões ---------------------------------------------------------
  const travado = Boolean(estado.versaoEnviada);
  elemento('undo-point').disabled = travado || quantidade === 0;
  elemento('clear-points').disabled = travado || quantidade === 0;
  elemento('invert-order').disabled = travado || quantidade < 3;

  // --- indicador de sentido, a partir do 3º ponto (seção 11.3) -------
  const sentidoNode = elemento('orientation-state');
  if (quantidade < 3 || !resultado) {
    sentidoNode.className = 'notice';
    sentidoNode.innerHTML = `<div>Marque pelo menos ${MIN_POINTS} pontos para fechar a cerca.</div>`;
  } else if (resultado.orientation === 'clockwise') {
    sentidoNode.className = 'notice ok-notice';
    sentidoNode.innerHTML = '<div><strong>Sentido correto (horário)</strong>'
      + 'Os pontos seguem o sentido dos ponteiros do relógio.</div>';
  } else {
    sentidoNode.className = 'notice error';
    sentidoNode.innerHTML = '<div><strong>Sentido anti-horário</strong>'
      + 'Use o botão Inverter ordem, ou refaça marcando no sentido do relógio.</div>';
  }

  // --- lista dos pontos -----------------------------------------------
  const lista = elemento('points-body');
  lista.innerHTML = quantidade
    ? estado.pontos.map((ponto, indice) => `
        <tr>
          <td>${indice + 1}</td>
          <td class="mono">${ponto.lat.toFixed(6)}</td>
          <td class="mono">${ponto.lon.toFixed(6)}</td>
          <td><button class="icon-button" type="button" data-remove="${indice}"
                 aria-label="Apagar o ponto ${indice + 1}"${travado ? ' disabled' : ''}>Apagar</button></td>
        </tr>`).join('')
    : '<tr class="empty-row"><td colspan="4">Clique no mapa para marcar o primeiro ponto</td></tr>';

  // --- problemas: avisos acima do mapa, só quando existem ------------
  const problemas = elemento('problems');
  problemas.hidden = !erros.length && !avisos.length;
  problemas.innerHTML = [...erros, ...avisos].map((v) => `
    <div class="notice ${v.severity === 'error' ? 'error' : 'warn'}">
      <div><strong>${v.severity === 'error' ? 'Precisa corrigir' : 'Atenção'}</strong>
      ${escapeHtml(v.message)}</div>
    </div>`).join('');

  elemento('server-check').textContent = estado.resultadoServidor
    ? 'Conferido pelo servidor.'
    : (quantidade >= MIN_POINTS ? 'Conferindo com o servidor…' : '');

  // --- revisão --------------------------------------------------------
  const caixaDeAvisos = elemento('accept-warnings-box');
  caixaDeAvisos.hidden = avisos.length === 0;
  elemento('accept-warnings').checked = estado.avisosAceitos;

  elemento('review-summary').innerHTML = quantidade >= MIN_POINTS && resultado
    ? `
      <dl class="summary">
        <div><dt>Pontos</dt><dd>${quantidade}</dd></div>
        <div><dt>Área</dt><dd>${numero(resultado.area_ha, 4)} ha</dd></div>
        <div><dt>Perímetro</dt><dd>${numero(resultado.perimeter_m)} m</dd></div>
        <div><dt>Aviso ao animal a</dt><dd>${numero(lerMargem('margin-attention'))} m da cerca</dd></div>
        <div><dt>Aviso forte a</dt><dd>${numero(lerMargem('margin-critical'))} m da cerca</dd></div>
      </dl>`
    : '<p class="muted-text">O resumo aparece quando a cerca estiver fechada.</p>';

  elemento('impact-note').textContent = montarAvisoDeImpacto();

  // --- botão enviar ----------------------------------------------------
  const temNome = (elemento('fence-name')?.value || '').trim().length > 0;
  const podeEnviar = Boolean(
    resultado && resultado.valid && quantidade >= MIN_POINTS && !estado.enviando
    && !travado && (avisos.length === 0 || estado.avisosAceitos) && temNome,
  );
  elemento('send-fence').disabled = !podeEnviar;

  orientar(quantidade, erros, avisos, temNome);
}

/* Cor de cada situação da faixa acima do mapa; as outras ficam `info`. */
const COR_DA_FAIXA = { erro: 'error', aviso: 'warn', pronta: 'ok-notice', enviada: 'ok-notice' };

/**
 * Faixa acima do mapa: o que o produtor deve fazer agora. Os textos
 * ficam no editor.html; aqui só se escolhe qual mostrar e a cor. Com
 * erro, ela fica vermelha e diz que o envio está bloqueado; o que está
 * errado aparece nos avisos logo abaixo dela.
 */
function orientar(quantidade, erros, avisos, temNome) {
  let situacao = temNome ? 'pronta' : 'nome';
  if (estado.versaoEnviada) situacao = 'enviada';
  else if (erros.length) situacao = 'erro';
  else if (quantidade < MIN_POINTS) situacao = quantidade ? 'continuar' : 'comecar';
  else if (avisos.length && !estado.avisosAceitos) situacao = 'aviso';

  const faixa = elemento('map-guide');
  faixa.className = `notice map-guide ${COR_DA_FAIXA[situacao] ?? 'info'}`;
  faixa.querySelectorAll('[data-when]').forEach((frase) => {
    frase.hidden = frase.dataset.when !== situacao;
  });
}

/**
 * "Vai substituir a cerca nº N em K coleiras" (seção 11.3, passo 4).
 * O produtor precisa saber o tamanho do que faz ANTES de clicar.
 */
function montarAvisoDeImpacto() {
  const coleiras = estado.config?.known_collars?.length ?? 0;
  const plural = coleiras === 1 ? 'coleira' : 'coleiras';
  if (!estado.cercaAtiva) {
    return coleiras
      ? `Esta será a primeira cerca, enviada para ${coleiras} ${plural}.`
      : 'Esta será a primeira cerca. Nenhuma coleira está cadastrada ainda.';
  }
  return `Vai substituir a cerca nº ${estado.cercaAtiva.version} em ${coleiras} ${plural}.`;
}

/* ---- Passo 5 — enviar e acompanhar ---- */

async function enviar() {
  if (estado.enviando) return;
  estado.enviando = true;
  renderizar();
  anunciar('Salvando a cerca…');

  try {
    const cerca = await criarCerca(montarCorpo());
    estado.versaoEnviada = cerca.version;
    anunciar(`Cerca nº ${cerca.version} salva. Aguardando a Base.`);
    mostrarAcompanhamento(cerca);
    await atualizarEntregas();
  } catch (erro) {
    anunciar(explicarErro(erro), 'error');
    // Se o servidor recusou por regra, mostramos as regras dele.
    if (erro.violations?.length) {
      estado.resultadoServidor = {
        valid: false,
        violations: erro.violations,
        area_ha: estado.resultadoLocal?.area_ha ?? 0,
        perimeter_m: estado.resultadoLocal?.perimeter_m ?? 0,
        orientation: estado.resultadoLocal?.orientation ?? null,
      };
    }
  } finally {
    estado.enviando = false;
    renderizar();
  }
}

function mostrarAcompanhamento(cerca) {
  elemento('sent-panel').hidden = false;
  elemento('sent-version').textContent = `nº ${cerca.version}`;
  elemento('sent-crc').textContent = cerca.crc32;
  elemento('sent-created').textContent = formatClock(cerca.created_at, { withDate: true });
  elemento('log-link').href = enderecoDoLog(cerca.version);
  elemento('editor-steps').classList.add('sent');
  // Os marcadores deixam de ser arrastáveis: a cerca já foi enviada.
  redesenhar();
}

async function atualizarEntregas() {
  if (!estado.versaoEnviada) return;
  try {
    desenharEntregas(await lerEntregas(estado.versaoEnviada));
  } catch {
    /* O WebSocket ou a próxima tentativa resolvem. */
  }
}

/* A linha do tempo: Salva → Na Base → Transmitindo → Confirmada/Falhou. */
const ETAPAS = ['pending', 'at_base', 'transmitting', 'confirmed'];

function desenharEntregas(entregas) {
  const corpo = elemento('deliveries-body');
  if (!entregas.length) {
    corpo.innerHTML = '<tr class="empty-row"><td colspan="3">Nenhuma coleira cadastrada</td></tr>';
    return;
  }
  corpo.innerHTML = entregas.map((entrega) => {
    const falhou = entrega.status === 'failed';
    const alcancada = falhou ? ETAPAS.length : ETAPAS.indexOf(entrega.status);
    const trilha = ETAPAS.map((etapa, indice) => {
      const feito = indice <= alcancada && !falhou;
      return `<span class="step-dot${feito ? ' done' : ''}" title="${etapa}"></span>`;
    }).join('');
    const detalhe = entrega.detail === 'crc_mismatch'
      ? ' (o que a coleira guardou não é a cerca enviada)'
      : entrega.detail === 'crc_missing'
        ? ' (a coleira não devolveu a conferência)'
        : entrega.detail ? ` (${escapeHtml(entrega.detail)})` : '';
    return `
      <tr>
        <td class="mono">${escapeHtml(entrega.collar_id)}</td>
        <td><span class="delivery-track">${trilha}</span></td>
        <td class="${falhou ? 'text-bad' : entrega.status === 'confirmed' ? 'text-ok' : ''}">
          ${escapeHtml(entrega.status_label)}${detalhe}
        </td>
      </tr>`;
  }).join('');
}

/* ---- Avisos ao produtor ---- */

function anunciar(texto, tipo = 'info') {
  const faixa = elemento('editor-announce');
  if (!faixa) return;
  faixa.className = `notice ${tipo === 'error' ? 'error' : 'info'}`;
  faixa.innerHTML = `<div>${escapeHtml(texto)}</div>`;
  faixa.hidden = false;
}

/* ---- Ligação dos controles ---- */

function ligarControles() {
  elemento('undo-point').addEventListener('click', desfazerUltimo);
  elemento('clear-points').addEventListener('click', limparTudo);
  elemento('invert-order').addEventListener('click', inverterOrdem);
  elemento('send-fence').addEventListener('click', enviar);

  elemento('points-body').addEventListener('click', (evento) => {
    const botao = evento.target.closest('[data-remove]');
    if (botao) apagarPonto(Number(botao.dataset.remove));
  });

  ['margin-attention', 'margin-critical'].forEach((id) => {
    elemento(id).addEventListener('input', mudou);
  });
  elemento('fence-name').addEventListener('input', renderizar);

  elemento('accept-warnings').addEventListener('change', (evento) => {
    estado.avisosAceitos = evento.target.checked;
    renderizar();
  });

  // Atalhos de teclado: desfazer é a ação mais repetida ao desenhar.
  document.addEventListener('keydown', (evento) => {
    if (evento.target.matches('input, textarea')) return;
    if ((evento.ctrlKey || evento.metaKey) && evento.key === 'z') {
      evento.preventDefault();
      desfazerUltimo();
    }
  });
}

/* ---- Início ---- */

async function iniciar() {
  try {
    estado.config = await lerConfiguracao();
  } catch (erro) {
    anunciar(explicarErro(erro), 'error');
    return;
  }

  // Margens padrão do .env (seção 11.3, passo 3).
  elemento('margin-attention').value = estado.config.margin_attention_default_m;
  elemento('margin-critical').value = estado.config.margin_critical_default_m;
  elemento('attention-explain').textContent =
    `A coleira começa a avisar o animal a ${numero(estado.config.margin_attention_default_m)} m da cerca.`;
  elemento('base-label').textContent = estado.config.base_id;
  elemento('max-radius').textContent = numero(estado.config.base_max_radius_km);

  estado.mapa = criarMapa('editor-map', estado.config, 17);
  if (estado.mapa) {
    marcarBase(estado.mapa, estado.config);
    desenharAlcanceDoRadio(estado.mapa, estado.config);
    estado.mapa.on('click', (evento) => {
      if (estado.versaoEnviada) return;  // cerca já enviada: não aceita mais ponto
      acrescentarPonto(evento.latlng.lat, evento.latlng.lng);
    });
    estado.mapa.on('mousemove', (evento) => {
      elemento('cursor-position').textContent =
        `${evento.latlng.lat.toFixed(6)}, ${evento.latlng.lng.toFixed(6)}`;
    });
  }

  // A cerca que está valendo, desenhada em cinza como referência.
  try {
    estado.cercaAtiva = await lerCercaAtiva();
    desenharCercaAtiva();
    enquadrar(estado.mapa, estado.cercaAtiva.points, 17);
  } catch {
    estado.cercaAtiva = null;  // 404: ainda não há cerca. Normal.
  }

  // Depois da cerca ativa, de propósito: quando a posição do produtor
  // chega, ela vence o enquadramento — desde que ele não tenha começado
  // a marcar pontos.
  mostrarLocalizacao(estado.mapa, () => estado.pontos.length === 0);

  ligarControles();
  renderizar();

  // Tempo real: a linha do tempo da entrega se move sozinha.
  conectarTempoReal({
    onDelivery(dados) {
      if (dados.fence_version === estado.versaoEnviada) atualizarEntregas();
    },
    onBase(dados) {
      elemento('base-state').textContent = dados.online ? 'conectada' : 'sem contato';
    },
    // Plano B quando o WebSocket não está disponível (seção 6.2).
    onPollingTick: atualizarEntregas,
  });
}

iniciar();
