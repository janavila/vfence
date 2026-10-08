/* ==========================================================================
   paineis.js — Situação da propriedade, Histórico, Rebanho e Eventos.

   Um módulo para quatro telas porque as quatro fazem a mesma coisa:
   buscar dados, desenhar uma tabela, atualizar ao vivo. Em quatro
   arquivos, os formatadores e o laço de atualização ficariam duplicados
   — e a seção 11.1 dá 100 KB de orçamento para o JS e o CSS próprios.

   Cada tela chama `iniciarPainel('<nome>')` no fim do seu HTML.
   ========================================================================== */

import {
  enderecoDoGeojson, enderecoDoLog, explicarErro, lerCercaAtiva, lerColeiras,
  lerConfiguracao, lerEntregas, lerEventos, lerHistorico, reativarCerca,
} from './api.js';
import { criarMapa, enquadrar, marcarBase, mostrarLocalizacao, paraLeaflet } from './map.js';
import { conectarTempoReal } from './realtime.js';
import { escapeHtml, formatClock } from './ui.js';

/* Nomes de zona em português do produtor, e a variável CSS de cada cor.
   As cores em si vivem no :root de style.css, para a troca (quando o
   grupo confirmar o mapeamento da IP1) ser uma linha por cor. */
const ZONAS = {
  SEGURO: { texto: 'Tranquilo', cor: 'var(--zone-seguro)' },
  ATENCAO: { texto: 'Perto da cerca', cor: 'var(--zone-atencao)' },
  CRITICO: { texto: 'Muito perto', cor: 'var(--zone-critico)' },
  FORA: { texto: 'Fora da área', cor: 'var(--zone-fora)' },
  GNSS_INVALIDO: { texto: 'Sem sinal de GPS', cor: 'var(--zone-gnss-invalido)' },
};

const EVENTOS = {
  zone_change: 'mudou de situação',
  invalid_zone: 'enviou uma situação desconhecida',
};

const elemento = (id) => document.getElementById(id);

function etiquetaDeZona(zona) {
  const info = ZONAS[zona] ?? { texto: 'Sem informação', cor: 'var(--neutral)' };
  return `<span class="zone-badge" style="--zone: ${info.cor}">${escapeHtml(info.texto)}</span>`;
}

function vazio(colunas, texto) {
  return `<tr class="empty-row"><td colspan="${colunas}">${escapeHtml(texto)}</td></tr>`;
}

function numeroOu(valor, sufixo = '', casas = 0) {
  if (valor === null || valor === undefined) return '—';
  return `${Number(valor).toFixed(casas).replace('.', ',')}${sufixo}`;
}

/* ---- Situação da propriedade (modo Gestão) ---- */

async function desenharPainel() {
  // Cerca que está valendo e quantas coleiras confirmaram.
  try {
    const cerca = await lerCercaAtiva();
    elemento('active-fence-name').textContent = cerca.name;
    elemento('active-fence-name').classList.remove('empty-value');
    elemento('active-fence-note').textContent =
      `Cerca nº ${cerca.version} · ${numeroOu(cerca.area_ha, ' ha', 2)} · enviada ${formatClock(cerca.created_at, { withDate: true })}`;

    const entregas = await lerEntregas(cerca.version);
    const confirmadas = entregas.filter((e) => e.status === 'confirmed').length;
    elemento('confirmed-count').textContent = `${confirmadas} de ${entregas.length}`;
    elemento('confirmed-count').classList.remove('empty-value');
  } catch {
    elemento('active-fence-note').textContent = 'Nenhuma cerca enviada ainda. Use o Editor de cerca.';
  }

  // Contagem por zona.
  try {
    const coleiras = await lerColeiras();
    const emAtencao = coleiras.filter((c) => c.last_zone === 'ATENCAO' || c.last_zone === 'CRITICO').length;
    const foraDaArea = coleiras.filter((c) => c.last_zone === 'FORA').length;
    const definir = (id, valor) => {
      const node = elemento(id);
      node.textContent = String(valor);
      node.classList.toggle('empty-value', coleiras.length === 0);
      if (!coleiras.length) node.textContent = '—';
    };
    definir('attention-count', emAtencao);
    definir('outside-count', foraDaArea);
  } catch { /* o aviso de conexão já apareceu */ }
}

/* ---- Histórico ---- */

async function desenharHistorico() {
  const corpo = elemento('history-body');
  try {
    const cercas = await lerHistorico();
    corpo.innerHTML = cercas.length
      ? cercas.map((c) => `
          <tr${c.status === 'active' ? ' class="is-active"' : ''}>
            <td><strong>nº ${c.version}</strong>${c.status === 'active' ? ' <span class="pill ok">valendo</span>' : ''}</td>
            <td>${escapeHtml(c.name)}${c.reactivated_from ? `<br><small class="muted-text">cópia da nº ${c.reactivated_from}</small>` : ''}</td>
            <td class="small">${formatClock(c.created_at, { withDate: true })}</td>
            <td>${c.point_count}</td>
            <td>${numeroOu(c.area_ha, ' ha', 3)}</td>
            <td class="mono small">${escapeHtml(c.crc32)}</td>
            <td>
              <div class="row-actions">
                <a class="button" href="${enderecoDoLog(c.version)}" download>Relatório</a>
                <a class="button" href="${enderecoDoGeojson(c.version)}" download>Mapa</a>
                ${c.status === 'active' ? '' : `<button class="button" type="button" data-reactivate="${c.version}">Usar de novo</button>`}
              </div>
            </td>
          </tr>`).join('')
      : vazio(7, 'Nenhuma cerca foi criada ainda');
  } catch (erro) {
    corpo.innerHTML = vazio(7, explicarErro(erro));
  }
}

async function tratarReativacao(evento) {
  const botao = evento.target.closest('[data-reactivate]');
  if (!botao) return;
  const versao = Number(botao.dataset.reactivate);

  // Confirmação antes de substituir a cerca ativa (seção 6.3 do planejamento).
  if (!window.confirm(
    `Usar de novo os pontos da cerca nº ${versao}?\n\n`
    + 'Isto cria uma cerca nova com o mesmo desenho e a envia para as coleiras, '
    + 'substituindo a que está valendo agora.',
  )) return;

  botao.disabled = true;
  try {
    const nova = await reativarCerca(versao);
    window.alert(`Pronto. A cerca nº ${nova.version} foi criada e está sendo enviada.`);
    await desenharHistorico();
  } catch (erro) {
    window.alert(explicarErro(erro));
    botao.disabled = false;
  }
}

/* ---- Rebanho ---- */

const rebanho = { mapa: null, marcadores: new Map(), cerca: null };

async function desenharRebanho() {
  await desenharUltimosAcontecimentos();
  let coleiras = [];
  try {
    coleiras = await lerColeiras();
  } catch (erro) {
    elemento('herd-body').innerHTML = vazio(7, explicarErro(erro));
    return;
  }

  elemento('herd-body').innerHTML = coleiras.length
    ? coleiras.map((c) => `
        <tr>
          <td class="mono"><strong>${escapeHtml(c.collar_id)}</strong></td>
          <td>${escapeHtml(c.animal_label ?? '—')}</td>
          <td>${c.last_zone ? etiquetaDeZona(c.last_zone) : '—'}</td>
          <td>${numeroOu(c.battery_pct, '%')}</td>
          <td class="small">${numeroOu(c.rssi, ' dBm')} / ${numeroOu(c.snr, '', 1)}</td>
          <td>${c.fence_version_reported ?? '—'}
              ${c.outdated ? '<span class="tag-outdated">desatualizada</span>' : ''}</td>
          <td class="small">${formatClock(c.last_seen)}</td>
        </tr>`).join('')
    : vazio(7, 'Nenhuma coleira enviou dados ainda');

  desenharColeirasNoMapa(coleiras);
}

function desenharColeirasNoMapa(coleiras) {
  if (!rebanho.mapa || !window.L) return;
  const comPosicao = coleiras.filter((c) => c.last_lat !== null && c.last_lon !== null);

  comPosicao.forEach((coleira) => {
    const cor = (ZONAS[coleira.last_zone] ?? { cor: 'var(--neutral)' }).cor;
    const posicao = [coleira.last_lat, coleira.last_lon];
    let marcador = rebanho.marcadores.get(coleira.collar_id);
    if (marcador) {
      marcador.setLatLng(posicao);
      marcador.setStyle({ color: cor, fillColor: cor });
    } else {
      marcador = L.circleMarker(posicao, {
        radius: 9, color: cor, fillColor: cor, weight: 3, fillOpacity: 0.7,
      }).addTo(rebanho.mapa);
      rebanho.marcadores.set(coleira.collar_id, marcador);
    }
    marcador.bindTooltip(
      `${coleira.collar_id} — ${(ZONAS[coleira.last_zone] ?? { texto: 'sem informação' }).texto}`,
      { direction: 'top' },
    );
  });

  if (comPosicao.length && !rebanho.enquadrou) {
    enquadrar(rebanho.mapa, comPosicao.map((c) => ({ lat: c.last_lat, lon: c.last_lon })), 17);
    rebanho.enquadrou = true;
  }
}

/** Últimos acontecimentos, abaixo do mapa do rebanho. */
async function desenharUltimosAcontecimentos() {
  const corpo = elemento('events-body');
  try {
    const eventos = await lerEventos(12);
    corpo.innerHTML = eventos.length
      ? eventos.map((e) => `
          <tr>
            <td class="mono small">${formatClock(e.ts)}</td>
            <td class="mono">${escapeHtml(e.collar_id)}</td>
            <td>${escapeHtml(EVENTOS[e.kind] ?? e.kind)}${e.zone ? `: ${etiquetaDeZona(e.zone)}` : ''}</td>
          </tr>`).join('')
      : vazio(3, 'Nenhum acontecimento registrado');
  } catch (erro) {
    corpo.innerHTML = vazio(3, explicarErro(erro));
  }
}

/* ---- Eventos ---- */

async function desenharEventos() {
  const filtro = elemento('event-filter')?.value || '';
  const corpo = elemento('timeline-body');
  try {
    const eventos = await lerEventos(200);
    const filtrados = filtro ? eventos.filter((e) => e.collar_id === filtro) : eventos;
    corpo.innerHTML = filtrados.length
      ? filtrados.map((e) => `
          <tr>
            <td class="small">${formatClock(e.ts, { withDate: true })}</td>
            <td class="mono">${escapeHtml(e.collar_id ?? '—')}</td>
            <td>${escapeHtml(EVENTOS[e.kind] ?? e.kind)}</td>
            <td>${e.zone ? etiquetaDeZona(e.zone) : '—'}</td>
            <td class="small muted-text">${escapeHtml(e.detail ?? '')}</td>
          </tr>`).join('')
      : vazio(5, filtro ? `Nenhum acontecimento da coleira ${filtro}` : 'Nenhum acontecimento registrado');
  } catch (erro) {
    corpo.innerHTML = vazio(5, explicarErro(erro));
  }
}

async function preencherFiltroDeColeiras() {
  const seletor = elemento('event-filter');
  if (!seletor) return;
  try {
    const coleiras = await lerColeiras();
    seletor.innerHTML = '<option value="">Todas as coleiras</option>'
      + coleiras.map((c) => `<option value="${escapeHtml(c.collar_id)}">${escapeHtml(c.collar_id)}</option>`).join('');
  } catch { /* o filtro fica só com "todas" */ }
}

/* ---- Início de cada tela ---- */

const TELAS = {
  painel: { desenhar: desenharPainel },
  historico: {
    desenhar: desenharHistorico,
    ligar: () => elemento('history-body')?.addEventListener('click', tratarReativacao),
  },
  rebanho: {
    desenhar: desenharRebanho,
    async ligar() {
      const config = await lerConfiguracao();
      rebanho.mapa = criarMapa('herd-map', config, 16);
      if (rebanho.mapa) {
        marcarBase(rebanho.mapa, config);
        // Centraliza no produtor só se ainda não enquadrou as coleiras:
        // nesta tela, o que importa ver primeiro são os animais.
        mostrarLocalizacao(rebanho.mapa, () => !rebanho.enquadrou);
        // A cerca que está valendo, para o produtor ver onde o animal está
        // em relação a ela.
        try {
          const cerca = await lerCercaAtiva();
          L.polygon(paraLeaflet(cerca.points), {
            color: '#245c4f', weight: 2, fillColor: '#245c4f',
            fillOpacity: 0.08, interactive: false,
          }).addTo(rebanho.mapa);
        } catch { /* sem cerca ainda */ }
      }
    },
  },
  eventos: {
    desenhar: desenharEventos,
    async ligar() {
      await preencherFiltroDeColeiras();
      elemento('event-filter')?.addEventListener('change', desenharEventos);
    },
  },
};

/**
 * Liga uma das quatro telas de painel.
 *
 * @param {'painel'|'historico'|'rebanho'|'eventos'} nome
 */
export async function iniciarPainel(nome) {
  const tela = TELAS[nome];
  if (!tela) return;

  try {
    await tela.ligar?.();
  } catch (erro) {
    console.warn('falha ao preparar a tela', nome, erro);
  }
  await tela.desenhar();

  // Tempo real, com o plano B por consulta periódica cuidando do resto
  // (seção 6.2). Redesenhar a tela inteira é barato: são tabelas pequenas.
  conectarTempoReal({
    onTelemetry: () => tela.desenhar(),
    onEvent: () => tela.desenhar(),
    onDelivery: () => tela.desenhar(),
    onPollingTick: () => tela.desenhar(),
  });
}
