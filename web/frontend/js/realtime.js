/* ==========================================================================
   realtime.js — WebSocket com reconexão e plano B por consulta periódica.

   A linha do tempo da entrega precisa se mover sozinha: o produtor
   clicou em Enviar e espera ver "Confirmada". Mandar recarregar a página
   seria inaceitável.

   WebSocket resolve, mas é a parte mais frágil da pilha: cai em rede
   instável e alguns intermediários o bloqueiam. Por isso a seção 6.2
   manda ter plano B — consultar a API a cada 5 s enquanto ele estiver
   fora, e desligar o plano B quando ele voltar.

   A reconexão usa espera crescente (1 s, 2 s, 4 s… até 30 s). Com
   reconexão fixa de 1 s, vinte abas abertas viram vinte requisições por
   segundo contra um servidor que já está com problema.

   Tipos de mensagem (seção 6.2): `delivery`, `telemetry`, `event`, `base`.
   ========================================================================== */

import { setOfflineBanner } from './ui.js';

const ESPERA_INICIAL_MS = 1000;
const ESPERA_MAXIMA_MS = 30000;
const INTERVALO_DO_PLANO_B_MS = 5000;

/**
 * Liga o tempo real. Os tratadores são todos opcionais: cada tela passa
 * só os que lhe interessam.
 *
 * @param {{onDelivery?, onTelemetry?, onEvent?, onBase?, onPollingTick?,
 *          onStatus?}} tratadores
 * @returns {{fechar: () => void, ligado: () => boolean}}
 */
export function conectarTempoReal(tratadores = {}) {
  let socket = null;
  let espera = ESPERA_INICIAL_MS;
  let temporizadorDeReconexao = null;
  let temporizadorDoPlanoB = null;
  let encerrado = false;

  function avisarSituacao(ligado) {
    const indicador = document.getElementById('realtime-state');
    if (indicador) {
      indicador.textContent = ligado ? 'ao vivo' : 'atualizando a cada 5 s';
      indicador.dataset.live = ligado ? 'true' : 'false';
    }
    tratadores.onStatus?.(ligado);
  }

  function ligarPlanoB() {
    if (temporizadorDoPlanoB || !tratadores.onPollingTick) return;
    temporizadorDoPlanoB = window.setInterval(() => {
      tratadores.onPollingTick?.();
    }, INTERVALO_DO_PLANO_B_MS);
    // Uma consulta já agora, para a tela não ficar 5 s parada.
    tratadores.onPollingTick?.();
  }

  function desligarPlanoB() {
    if (!temporizadorDoPlanoB) return;
    window.clearInterval(temporizadorDoPlanoB);
    temporizadorDoPlanoB = null;
  }

  function tratarMensagem(texto) {
    let mensagem;
    try {
      mensagem = JSON.parse(texto);
    } catch {
      return;  // mensagem malformada: ignorar é melhor que quebrar a tela
    }
    const { type: tipo, data: dados } = mensagem;
    if (tipo === 'delivery') tratadores.onDelivery?.(dados);
    else if (tipo === 'telemetry') tratadores.onTelemetry?.(dados);
    else if (tipo === 'event') tratadores.onEvent?.(dados);
    else if (tipo === 'base') tratadores.onBase?.(dados);
  }

  function conectar() {
    if (encerrado) return;
    const protocolo = window.location.protocol === 'https:' ? 'wss' : 'ws';
    try {
      socket = new WebSocket(`${protocolo}://${window.location.host}/ws`);
    } catch {
      agendarReconexao();
      return;
    }

    socket.onopen = () => {
      espera = ESPERA_INICIAL_MS;  // deu certo: zera a espera
      desligarPlanoB();
      setOfflineBanner(false);
      avisarSituacao(true);
    };

    socket.onmessage = ({ data }) => tratarMensagem(data);

    socket.onclose = () => {
      socket = null;
      avisarSituacao(false);
      ligarPlanoB();
      agendarReconexao();
    };

    socket.onerror = () => {
      // O `onclose` vem logo depois e cuida da reconexão. Aqui só
      // garantimos que o plano B assuma de imediato.
      ligarPlanoB();
    };
  }

  function agendarReconexao() {
    if (encerrado || temporizadorDeReconexao) return;
    temporizadorDeReconexao = window.setTimeout(() => {
      temporizadorDeReconexao = null;
      conectar();
    }, espera);
    espera = Math.min(espera * 2, ESPERA_MAXIMA_MS);
  }

  conectar();

  // Ao voltar para a aba, tenta reconectar na hora em vez de esperar:
  // o navegador costuma derrubar WebSocket de aba em segundo plano.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !socket && !encerrado) {
      espera = ESPERA_INICIAL_MS;
      window.clearTimeout(temporizadorDeReconexao);
      temporizadorDeReconexao = null;
      conectar();
    }
  });

  return {
    fechar() {
      encerrado = true;
      desligarPlanoB();
      window.clearTimeout(temporizadorDeReconexao);
      socket?.close();
    },
    ligado: () => socket?.readyState === WebSocket.OPEN,
  };
}
