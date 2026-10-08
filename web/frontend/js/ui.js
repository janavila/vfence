/* ==========================================================================
   ui.js — o que é comum a todas as telas (seções 11.1 e 11.2):
   marca o item do menu, consulta GET /api/health e GET /api/bases de
   tempo em tempo (indicador da Base no cabeçalho) e acende/apaga a
   faixa "Sem conexão com o servidor".

   Começa pela conexão de propósito: a seção 11.1 trata conexão ruim como
   situação NORMAL, não erro — o Central roda na rede de uma propriedade
   rural. O aviso e a nova tentativa não são enfeite para o fim.

   Decisões: módulo ES nativo (sem empacotador, e o módulo só executa
   depois do HTML montado, dispensando `DOMContentLoaded`);
   `AbortSignal.timeout` para um servidor que aceita a conexão mas não
   responde não deixar a tela em "Verificando…" para sempre;
   `escapeHtml`, `formatClock` e `setOfflineBanner` exportadas porque
   `api.js`, `editor.js` e `paineis.js` as reaproveitam.
   ========================================================================== */

/* Intervalo entre verificações do servidor, em milissegundos.
   5 s é o mesmo valor que a seção 6.2 define como plano B do WebSocket:
   um número só, para o sistema ter um ritmo previsível. */
const HEALTH_INTERVAL_MS = 5000;

/* Tempo máximo de espera por uma resposta. */
const REQUEST_TIMEOUT_MS = 4000;

/* ---- Utilidades de texto, reaproveitáveis pelas outras telas ---- */

/**
 * Neutraliza caracteres de HTML em texto vindo do servidor.
 *
 * Usada em toda linha de tabela montada com `innerHTML`. O nome de uma
 * cerca é digitado pelo produtor: se alguém escrever `<script>` como
 * nome do piquete, isso não pode virar código na tela.
 */
export function escapeHtml(value) {
  if (value === null || value === undefined) return '—';
  return String(value).replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[character]));
}

/**
 * Data ISO em UTC para a hora local do produtor.
 *
 * O servidor grava tudo em UTC (seção 7); a conversão acontece só aqui,
 * na tela — o produtor precisa ler "14:32", não "17:32Z".
 */
export function formatClock(isoText, options = {}) {
  if (!isoText) return '—';
  const moment = new Date(isoText);
  if (Number.isNaN(moment.getTime())) return '—';
  const timePart = moment.toLocaleTimeString('pt-BR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  if (!options.withDate) return timePart;
  const datePart = moment.toLocaleDateString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  });
  return `${datePart} às ${timePart}`;
}

/* ---- Faixa de "sem conexão" ---- */

/** Mostra ou esconde a faixa de conexão perdida (tolera ela não existir). */
export function setOfflineBanner(offline) {
  const banner = document.getElementById('offline-banner');
  if (banner) banner.dataset.visible = offline ? 'true' : 'false';
}

/* ---- Menu: marca a página atual ---- */

/**
 * Marca o item do menu do endereço aberto.
 *
 * O HTML já traz `aria-current="page"`, mas repetir aqui evita menu
 * errado se alguém copiar uma página e esquecer de trocar o atributo.
 */
function markCurrentPage() {
  const here = window.location.pathname.replace(/\/+$/, '') || '/';
  document.querySelectorAll('.primary-nav a').forEach((link) => {
    const target = new URL(link.getAttribute('href'), window.location.origin)
      .pathname.replace(/\/+$/, '') || '/';
    if (target === here) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

/* ---- Verificação do servidor ---- */

/**
 * GET em `/api/health` ou `/api/bases`, ou `null` se falhar. Não deixa
 * exceção escapar: para a tela, "não respondeu" é resultado esperado.
 * Não usa o `api.js` porque ele importa este arquivo.
 */
async function fetchJson(url) {
  try {
    const response = await fetch(url, {
      cache: 'no-store',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

/** Escreve texto em um elemento, se ele existir nesta página. */
function write(elementId, text) {
  const element = document.getElementById(elementId);
  if (element) element.textContent = text;
}

/**
 * Atualiza o painel "Servidor VFence" (só existe no painel da
 * Gestão) e, se o servidor não responde, o indicador da Base.
 */
function renderHealth(health) {
  const pill = document.getElementById('service-pill');
  const baseIndicator = document.getElementById('base-indicator');

  if (health === null) {
    if (pill) {
      pill.className = 'pill bad';
      pill.textContent = 'Fora do ar';
    }
    write('service-status', 'O servidor não respondeu');
    if (baseIndicator) {
      baseIndicator.dataset.state = 'offline';
      write('base-indicator-text', 'Sem conexão');
    }
    setOfflineBanner(true);
    return;
  }

  if (pill) {
    pill.className = 'pill ok';
    pill.textContent = 'Funcionando';
  }
  write('service-status', 'Funcionando normalmente');
  write('service-version', health.version ?? '—');
  write('service-time', formatClock(health.time, { withDate: true }));
  write('service-checked', formatClock(new Date().toISOString()));
  setOfflineBanner(false);
}

/**
 * Indicador da Base no cabeçalho: ligada ou não, e o último contato.
 * Fica aqui, e não em cada tela, porque o cabeçalho é o mesmo em todas
 * — antes o editor ficava preso em "ainda não configurada".
 */
function renderBase(bases) {
  const indicator = document.getElementById('base-indicator');
  if (!indicator || !bases) return;
  const base = bases[0];
  indicator.dataset.state = base ? (base.online ? 'online' : 'offline') : 'unknown';
  let text = 'Nenhuma Base cadastrada';
  if (base?.online) text = `Base ligada · ${formatClock(base.last_heartbeat)}`;
  else if (base?.last_heartbeat) text = `Base sem contato desde ${formatClock(base.last_heartbeat)}`;
  else if (base) text = 'Base nunca se conectou';
  write('base-indicator-text', text);
}

/** Verifica agora e agenda a próxima verificação. */
async function monitorService() {
  const health = await fetchJson('/api/health');
  renderHealth(health);
  if (health) renderBase(await fetchJson('/api/bases'));
  window.setTimeout(monitorService, HEALTH_INTERVAL_MS);
}

/* ---- Início ---- */

markCurrentPage();
monitorService();

/* ---- Sair do sistema (fase F8) ---- */

/**
 * Liga o botão "Sair" do cabeçalho, se a tela tiver um.
 *
 * `replace` em vez de `href`: o botão Voltar do navegador não deve
 * trazer o produtor de volta para uma tela que ele já não pode ver.
 */
function ligarBotaoSair() {
  const botao = document.getElementById('logout-button');
  if (!botao) return;
  botao.addEventListener('click', async () => {
    botao.disabled = true;
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch { /* sem rede: mandamos ao login de todo jeito */ }
    window.location.replace('/login');
  });
}

ligarBotaoSair();
