/* ==========================================================================
   api.js — todas as chamadas à API em um lugar só.

   Três coisas precisam acontecer igual em TODA chamada, e repeti-las em
   cada tela é como elas saem de sincronia: decodificar o corpo de erro
   da seção 6 (`fence_invalid`, `warnings_not_accepted`); acender e
   apagar a faixa "Sem conexão com o servidor"; e cortar a requisição no
   tempo limite, para a tela não ficar em "salvando…" para sempre.

   `editor.js`, `paineis.js` e `realtime.js` chamam só as funções daqui;
   nunca usam `fetch` direto.
   ========================================================================== */

import { setOfflineBanner } from './ui.js';

const TEMPO_LIMITE_MS = 10000;

/**
 * Erro vindo da API, com o corpo já decodificado.
 *
 * `code` é o `detail` do servidor (`fence_invalid`, `fence_not_found`…),
 * `violations` traz a lista de regras quando o erro é de cerca.
 */
export class ErroDaApi extends Error {
  constructor(status, corpo) {
    const codigo = corpo?.detail ?? 'erro_desconhecido';
    super(corpo?.message || codigo);
    this.name = 'ErroDaApi';
    this.status = status;
    this.code = typeof codigo === 'string' ? codigo : 'erro_de_validacao';
    this.rule = corpo?.rule ?? null;
    this.violations = corpo?.violations ?? [];
    this.corpo = corpo;
  }
}

/** Erro de rede: o servidor não respondeu. É situação normal no campo. */
export class ErroDeConexao extends Error {
  constructor(causa) {
    super('não foi possível falar com o servidor');
    this.name = 'ErroDeConexao';
    this.causa = causa;
  }
}

/**
 * Faz uma chamada à API e devolve o corpo já decodificado.
 *
 * @param {string} caminho  ex.: '/api/fences'
 * @param {{metodo?: string, corpo?: object, texto?: boolean}} opcoes
 */
export const produtor = location.pathname === '/produtor' || location.pathname.startsWith('/produtor/');
const fencePath = produtor ? '/api/producer/fences' : '/api/fences';

async function chamar(caminho, { metodo = 'GET', corpo = null, texto = false, key = null, signal } = {}) {
  let resposta;
  try {
    resposta = await fetch(caminho, {
      method: metodo,
      headers: { ...(corpo ? { 'Content-Type': 'application/json' } : {}),
        ...(key ? { 'Idempotency-Key': key } : {}) },
      body: corpo ? JSON.stringify(corpo) : undefined,
      cache: 'no-store',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(TEMPO_LIMITE_MS)]) : AbortSignal.timeout(TEMPO_LIMITE_MS),
    });
  } catch (causa) {
    if (!signal?.aborted) setOfflineBanner(true);
    throw new ErroDeConexao(causa);
  }

  setOfflineBanner(false);

  if (resposta.status === 204) return null;
  if (resposta.status === 401 && produtor) location.replace('/login?next=%2Fprodutor');

  if (!resposta.ok) {
    let decodificado = null;
    try {
      decodificado = await resposta.json();
    } catch {
      decodificado = { detail: `http_${resposta.status}` };
    }
    throw new ErroDaApi(resposta.status, decodificado);
  }

  return texto ? resposta.text() : resposta.json();
}

/* ---- Configuração e situação ---- */

export const consultar = chamar;
export const lerConfiguracao = () => chamar(produtor ? '/api/producer/settings' : '/api/settings');

/* ---- Cercas ---- */

export const validarNoServidor = (cerca) =>
  chamar(fencePath + '/validate', { metodo: 'POST', corpo: cerca });

export const criarCerca = (cerca, key) =>
  chamar(fencePath, { metodo: 'POST', corpo: cerca, key });

export const lerCercaAtiva = () => chamar(fencePath + '/active');
export const lerHistorico = () => chamar(fencePath);
export const lerCerca = versao => chamar(fencePath + '/' + versao);
export const reativarCerca = (versao, key) =>
  chamar(fencePath + '/' + versao + '/reactivate', { metodo: 'POST', key });
export const lerEntregas = versao => chamar(fencePath + '/' + versao + '/deliveries');
export const lerRascunhos = () => chamar('/api/producer/drafts');
export const lerRascunho = id => chamar('/api/producer/drafts/' + id);
export const salvarRascunho = (id, corpo) => chamar('/api/producer/drafts/' + id, { metodo: 'PUT', corpo });

/** Endereços de download. Não passam por `fetch`: o navegador baixa direto. */
export const enderecoDoLog = (versao) => `/api/fences/${versao}/log`;
export const enderecoDoGeojson = (versao) => `/api/fences/${versao}/geojson`;

/* ---- Rebanho e eventos ---- */

export const lerColeiras = () => chamar('/api/collars');
export const lerEventos = (limite = 100, filtros = {}) =>
  chamar(`/api/events?${new URLSearchParams({ limit: limite, ...filtros })}`);
export const lerTrajeto = (coleira, filtros) =>
  chamar(`/api/collars/${encodeURIComponent(coleira)}/telemetry?${new URLSearchParams({ limit: 200, ...filtros })}`);
export const salvarNomeAnimal = (coleira, nome) =>
  chamar(`/api/collars/${encodeURIComponent(coleira)}`,
    { metodo: 'PATCH', corpo: { animal_label: nome } });

/* ---- Mensagem de erro para o produtor ---- */

/**
 * Traduz um erro em frase que diz O QUE FAZER (seção 11.1), em vez de
 * mostrar o código técnico do servidor.
 */
export function explicarErro(erro) {
  if (erro instanceof ErroDeConexao) {
    return 'Sem conexão com o servidor. Confira se o VFence está ligado e tente de novo.';
  }
  if (!(erro instanceof ErroDaApi)) {
    return 'Aconteceu um problema inesperado. Recarregue a página e tente de novo.';
  }
  switch (erro.code) {
    case 'fence_invalid':
      return erro.message || 'A cerca tem problemas que precisam ser corrigidos antes de enviar.';
    case 'warnings_not_accepted':
      return 'Marque "Estou ciente dos avisos" para poder enviar esta cerca.';
    case 'fence_not_found':
      return 'Essa cerca não existe mais.';
    case 'no_active_fence':
      return 'Nenhuma cerca foi enviada ainda.';
    case 'collar_not_found':
      return 'Essa coleira não está cadastrada.';
    default:
      if (erro.status === 422) return 'Algum campo do formulário está incorreto.';
      if (erro.status >= 500) return 'O servidor teve um problema. Tente de novo em instantes.';
      return erro.message || 'Não foi possível concluir a operação.';
  }
}
