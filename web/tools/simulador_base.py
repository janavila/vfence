#!/usr/bin/env python3
"""Base simulada do VFence (seção 9.4 da especificação).

Para que serve
--------------
Faz o papel do Raspberry Pi durante todo o desenvolvimento do Central.
Com ele é possível construir e demonstrar o sistema inteiro sem
hardware, sem rádio e sem depender do andamento da Base de verdade —
que é responsabilidade de outro integrante do grupo.

Também serve para a apresentação: dá para mostrar uma cerca saindo do
navegador e voltando confirmada, ao vivo, sem montar a bancada.

O que ele faz, em ciclo (seção 7.2 do planejamento)
---------------------------------------------------
    a cada --intervalo segundos:
        1. POST /api/edge/heartbeat   diz qual cerca tem
        2. se o Central quer outra versão:
             GET /api/edge/fences/{v} baixa
             recalcula o CRC pela forma canônica e CONFERE
             agenda a entrega de cada coleira
        3. envia 'transmitting' das coleiras agendadas
        4. depois de --atraso-confirmacao, envia 'confirmed'
           (ou 'failed', para as coleiras de --falhar)
        5. gera telemetria de uma coleira andando em linha reta
        6. POST /api/edge/batch       entrega a fila local

O que ele NÃO faz
-----------------
Não decide zona de verdade. A zona que ele envia é calculada de forma
simplificada (distância até a borda da cerca, com as margens recebidas)
só para a demonstração ter cores mudando na tela. No sistema real, quem
decide zona é a coleira — e o Central nunca recalcula.

Uso
---
    python tools/simulador_base.py --url http://localhost:8000 \\
           --base-id BASE01 --token troque-este-token \\
           --intervalo 5 --coleiras COL01,COL02 \\
           --atraso-confirmacao 3 --falhar COL02

Sem argumentos, ele lê `--base-id` e `--token` do `.env` do Central.
Encerre com Ctrl+C.
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Permite rodar de qualquer pasta: acha o .env do Central ao lado deste script.
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Raio da Terra: o MESMO valor do Central, para as contas baterem.
EARTH_RADIUS_M = 6371008.8


# ===========================================================================
# Forma canônica e CRC — reimplementados de propósito
# ===========================================================================
# Este script NÃO importa `backend.services.canonical`. A duplicação é
# intencional: o simulador faz o papel do firmware, e o valor do teste
# está justamente em uma implementação INDEPENDENTE chegar ao mesmo CRC.
# Se importasse a do Central, a conferência não provaria nada.


def graus_para_e6(graus: float) -> int:
    """Grau para microgradus, arredondando meio para longe do zero."""
    return int(math.floor(abs(graus) * 1_000_000 + 0.5)) * (1 if graus >= 0 else -1)


def bytes_canonicos(version: int, da_cm: int, dc_cm: int, pontos_e6: list[list[int]]) -> bytes:
    """Monta os bytes canônicos da cerca (seção 5.5), little-endian."""
    blob = struct.pack("<IHHB", version, da_cm, dc_cm, len(pontos_e6))
    for lat_e6, lon_e6 in pontos_e6:
        blob += struct.pack("<ii", lat_e6, lon_e6)
    return blob


def crc32_hex(dados: bytes) -> str:
    return f"{zlib.crc32(dados):08X}"


def agora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# Zona simplificada, só para a demonstração
# ===========================================================================


def projetar(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def distancia_ponto_segmento(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    comprimento = dx * dx + dy * dy
    if comprimento <= 1e-12:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / comprimento))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def esta_dentro(p, poligono) -> bool:
    """Teste de pertencimento por lançamento de raio (*ray casting*)."""
    dentro = False
    quantidade = len(poligono)
    for i in range(quantidade):
        ax, ay = poligono[i]
        bx, by = poligono[(i + 1) % quantidade]
        if (ay > p[1]) != (by > p[1]):
            corte = ax + (p[1] - ay) / (by - ay) * (bx - ax)
            if p[0] < corte:
                dentro = not dentro
    return dentro


def calcular_zona(lat: float, lon: float, cerca: dict) -> str:
    """Zona SIMPLIFICADA, só para a tela ter cor mudando na demonstração.

    No sistema real isto roda no firmware da coleira, com margem extra
    para o erro do GNSS (`distância − 1,5 × HDOP × UERE`). Aqui é a
    versão mínima que produz transições visíveis.
    """
    pontos = cerca["points_e6"]
    lat0 = sum(p[0] for p in pontos) / len(pontos) / 1e6
    lon0 = sum(p[1] for p in pontos) / len(pontos) / 1e6
    poligono = [projetar(p[0] / 1e6, p[1] / 1e6, lat0, lon0) for p in pontos]
    posicao = projetar(lat, lon, lat0, lon0)

    if not esta_dentro(posicao, poligono):
        return "FORA"

    borda = min(
        distancia_ponto_segmento(posicao, poligono[i], poligono[(i + 1) % len(poligono)])
        for i in range(len(poligono))
    )
    if borda <= cerca["margin_critical_cm"] / 100:
        return "CRITICO"
    if borda <= cerca["margin_attention_cm"] / 100:
        return "ATENCAO"
    return "SEGURO"


# ===========================================================================
# Estado do simulador
# ===========================================================================


@dataclass
class ColarSimulado:
    """Uma coleira simulada: posição, bateria e entrega pendente."""

    collar_id: str
    lat: float
    lon: float
    rumo_lat: float
    rumo_lon: float
    bateria: int = 100
    vai_falhar: bool = False
    # Versão de cerca a entregar e quando confirmar.
    entrega_versao: int | None = None
    entrega_em: float = 0.0
    entrega_transmitida: bool = False


@dataclass
class Simulador:
    url: str
    base_id: str
    token: str
    intervalo: float
    atraso_confirmacao: float
    coleiras: list[ColarSimulado]
    passo_m: float
    verbose: bool = True

    versao_local: int | None = None
    cerca_local: dict | None = None
    proximo_seq: int = 1
    fila: list[dict] = field(default_factory=list)

    # -- comunicação ----------------------------------------------------

    @property
    def cabecalhos(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def dizer(self, texto: str, marcador: str = "·") -> None:
        if self.verbose:
            print(f"{datetime.now().strftime('%H:%M:%S')} {marcador} {texto}", flush=True)

    # -- 1. heartbeat ---------------------------------------------------

    def heartbeat(self, cliente: httpx.Client) -> int | None:
        corpo = {
            "base_id": self.base_id,
            "fence_version": self.versao_local,
            "serial": "online (simulado)",
            "queue_size": len(self.fila),
        }
        resposta = cliente.post("/api/edge/heartbeat", json=corpo, headers=self.cabecalhos)
        resposta.raise_for_status()
        dados = resposta.json()
        desejada = dados["desired_fence_version"]
        self.dizer(
            f"heartbeat: tenho a cerca {self.versao_local}, "
            f"o Central quer a {desejada} (fila: {len(self.fila)})"
        )
        return desejada

    # -- 2. download e conferência do CRC -------------------------------

    def baixar_cerca(self, cliente: httpx.Client, versao: int) -> bool:
        resposta = cliente.get(f"/api/edge/fences/{versao}", headers=self.cabecalhos)
        if resposta.status_code == 404:
            self.dizer(f"cerca {versao} não existe mais no Central", "!")
            return False
        resposta.raise_for_status()
        cerca = resposta.json()

        # A conferência que dá sentido ao CRC: recalculamos aqui, com
        # código independente, e comparamos com o que o Central mandou.
        recalculado = crc32_hex(
            bytes_canonicos(
                cerca["version"],
                cerca["margin_attention_cm"],
                cerca["margin_critical_cm"],
                cerca["points_e6"],
            )
        )
        if recalculado != cerca["crc32"]:
            self.dizer(
                f"CRC NÃO BATE na cerca {versao}: "
                f"o Central diz {cerca['crc32']}, recalculei {recalculado}. "
                "A cerca foi DESCARTADA e a anterior continua valendo.",
                "✗",
            )
            return False

        self.dizer(
            f"cerca {versao} baixada: {len(cerca['points_e6'])} pontos, "
            f"dA={cerca['margin_attention_cm']}cm dC={cerca['margin_critical_cm']}cm, "
            f"CRC {cerca['crc32']} confere",
            "✓",
        )
        self.versao_local = versao
        self.cerca_local = cerca

        # Agenda a entrega em cada coleira.
        agora = time.monotonic()
        for coleira in self.coleiras:
            coleira.entrega_versao = versao
            coleira.entrega_transmitida = False
            coleira.entrega_em = agora + self.atraso_confirmacao
        return True

    # -- 3 e 4. entrega por coleira -------------------------------------

    def avancar_entregas(self) -> None:
        agora = time.monotonic()
        for coleira in self.coleiras:
            if coleira.entrega_versao is None:
                continue

            if not coleira.entrega_transmitida:
                self.enfileirar(
                    "delivery",
                    {
                        "collar_id": coleira.collar_id,
                        "fence_version": coleira.entrega_versao,
                        "status": "transmitting",
                    },
                )
                coleira.entrega_transmitida = True
                self.dizer(f"{coleira.collar_id}: transmitindo a cerca {coleira.entrega_versao}")
                continue

            if agora < coleira.entrega_em:
                continue

            if coleira.vai_falhar:
                self.enfileirar(
                    "delivery",
                    {
                        "collar_id": coleira.collar_id,
                        "fence_version": coleira.entrega_versao,
                        "status": "failed",
                        "detail": "sem resposta da coleira (simulado)",
                    },
                )
                self.dizer(
                    f"{coleira.collar_id}: FALHOU a cerca {coleira.entrega_versao} "
                    "(pedido por --falhar)",
                    "✗",
                )
            else:
                self.enfileirar(
                    "delivery",
                    {
                        "collar_id": coleira.collar_id,
                        "fence_version": coleira.entrega_versao,
                        "status": "confirmed",
                        "crc32": self.cerca_local["crc32"] if self.cerca_local else None,
                    },
                )
                self.dizer(
                    f"{coleira.collar_id}: CONFIRMOU a cerca {coleira.entrega_versao} "
                    f"(crc {self.cerca_local['crc32'] if self.cerca_local else '?'})",
                    "✓",
                )
            coleira.entrega_versao = None

    # -- 5. telemetria sintética ----------------------------------------

    def gerar_telemetria(self) -> None:
        """Move cada coleira em linha reta e gera uma posição.

        A primeira coleira anda o suficiente para atravessar a cerca e
        voltar, produzindo transições de zona visíveis no painel. É o
        que a seção 9.4 pede: "uma coleira andando em linha reta e
        cruzando a cerca".
        """
        for coleira in self.coleiras:
            metros_por_grau_lat = 111_320.0
            metros_por_grau_lon = 111_320.0 * math.cos(math.radians(coleira.lat))
            coleira.lat += coleira.rumo_lat * self.passo_m / metros_por_grau_lat
            coleira.lon += coleira.rumo_lon * self.passo_m / metros_por_grau_lon
            coleira.bateria = max(5, coleira.bateria - 1)

            zona = (
                calcular_zona(coleira.lat, coleira.lon, self.cerca_local)
                if self.cerca_local
                else "GNSS_INVALIDO"
            )

            self.enfileirar(
                "telemetry",
                {
                    "collar_id": coleira.collar_id,
                    "ts": agora_iso(),
                    "lat": round(coleira.lat, 6),
                    "lon": round(coleira.lon, 6),
                    "zone": zona,
                    "satellites": 11,
                    "hdop": 0.9,
                    "battery_pct": coleira.bateria,
                    "fence_version": self.versao_local,
                    "rssi": -92,
                    "snr": 7.5,
                },
            )

            if zona in {"FORA", "CRITICO"}:
                self.enfileirar(
                    "event",
                    {
                        "collar_id": coleira.collar_id,
                        "kind": "zone_change",
                        "zone": zona,
                        "ts": agora_iso(),
                    },
                )
                # Ao sair, inverte o rumo: a coleira volta para dentro e
                # a demonstração mostra o ciclo completo de zonas.
                coleira.rumo_lat *= -1
                coleira.rumo_lon *= -1

    # -- fila local ------------------------------------------------------

    def enfileirar(self, tipo: str, dados: dict) -> None:
        """Acrescenta um item à fila local, com `edge_seq` crescente.

        A sequência é por Base e nunca repete nem volta atrás: é o que
        permite ao Central deduplicar e responder `acked_up_to`.
        """
        self.fila.append({"edge_seq": self.proximo_seq, "type": tipo, "data": dados})
        self.proximo_seq += 1

    # -- 6. envio do lote ------------------------------------------------

    def enviar_lote(self, cliente: httpx.Client) -> None:
        if not self.fila:
            return
        quantidade = len(self.fila)
        resposta = cliente.post(
            "/api/edge/batch",
            json={"base_id": self.base_id, "items": self.fila},
            headers=self.cabecalhos,
        )
        resposta.raise_for_status()
        confirmado = resposta.json()["acked_up_to"]

        antes = len(self.fila)
        self.fila = [item for item in self.fila if item["edge_seq"] > confirmado]
        self.dizer(
            f"lote de {quantidade} item(ns) enviado, confirmado até {confirmado} "
            f"({antes - len(self.fila)} removido(s) da fila)"
        )

    # -- ciclo -----------------------------------------------------------

    def um_ciclo(self, cliente: httpx.Client) -> None:
        desejada = self.heartbeat(cliente)
        if desejada is not None and desejada != self.versao_local:
            self.baixar_cerca(cliente, desejada)
        self.avancar_entregas()
        self.gerar_telemetria()
        self.enviar_lote(cliente)

    def rodar(self) -> int:
        print("=" * 72)
        print("VFence — Base simulada")
        print("=" * 72)
        print(f"Central      : {self.url}")
        print(f"Base         : {self.base_id}")
        print(f"Coleiras     : {', '.join(c.collar_id for c in self.coleiras)}")
        falhas = [c.collar_id for c in self.coleiras if c.vai_falhar]
        print(f"Vão falhar   : {', '.join(falhas) if falhas else '(nenhuma)'}")
        print(f"Intervalo    : {self.intervalo}s")
        print(f"Confirmação  : {self.atraso_confirmacao}s após o download")
        print("=" * 72)
        print("Ctrl+C para encerrar.\n")

        with httpx.Client(base_url=self.url, timeout=10) as cliente:
            while True:
                try:
                    self.um_ciclo(cliente)
                except httpx.HTTPStatusError as erro:
                    self.dizer(
                        f"o Central respondeu {erro.response.status_code}: "
                        f"{erro.response.text[:200]}",
                        "!",
                    )
                    if erro.response.status_code in (401, 403):
                        print(
                            "\nToken recusado. Confira se --token é o mesmo BASE_TOKEN\n"
                            "do .env do Central, e se --base-id é o mesmo BASE_ID.",
                            file=sys.stderr,
                        )
                        return 1
                except httpx.RequestError as erro:
                    # Sem rede é situação NORMAL no campo: a fila cresce e
                    # sobe quando a conexão voltar.
                    self.dizer(
                        f"Central inalcançável ({erro.__class__.__name__}); "
                        f"a fila local guarda {len(self.fila)} item(ns)",
                        "!",
                    )
                time.sleep(self.intervalo)


# ===========================================================================
# Linha de comando
# ===========================================================================


def ler_env() -> dict:
    """Lê BASE_ID e BASE_TOKEN do .env do Central, se existir.

    Evita o passo mais chato de usar o simulador: copiar o token à mão
    toda vez. Leitura simples de `CHAVE=valor`, sem dependência nova.
    """
    caminho = PROJECT_DIR / ".env"
    valores: dict[str, str] = {}
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            valores[chave.strip()] = valor.strip()
    return valores


def main(argv: list[str] | None = None) -> int:
    env = ler_env()
    parser = argparse.ArgumentParser(
        description="Base simulada do VFence: faz o papel do Raspberry Pi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", default="http://localhost:8000", help="endereço do Central")
    parser.add_argument(
        "--base-id",
        default=os.getenv("BASE_ID", env.get("BASE_ID", "BASE01")),
        help="identificador da Base (padrão: do .env do Central)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("BASE_TOKEN", env.get("BASE_TOKEN", "")),
        help="BASE_TOKEN do Central (padrão: do .env do Central)",
    )
    parser.add_argument("--intervalo", type=float, default=5.0, help="segundos entre ciclos")
    parser.add_argument(
        "--coleiras",
        default=env.get("COLARES_CONHECIDOS", "COL01,COL02"),
        help="identificadores separados por vírgula",
    )
    parser.add_argument(
        "--atraso-confirmacao",
        type=float, default=3.0,
        help="segundos entre o download e a confirmação de cada coleira",
    )
    parser.add_argument(
        "--falhar", default="",
        help="coleiras que vão FALHAR a entrega, separadas por vírgula",
    )
    parser.add_argument(
        "--base-lat", type=float,
        default=float(env.get("BASE_LAT", "-31.306200")),
        help="de onde as coleiras partem",
    )
    parser.add_argument(
        "--base-lon", type=float,
        default=float(env.get("BASE_LON", "-54.063950")),
    )
    parser.add_argument(
        "--passo", type=float, default=4.0,
        help="metros que cada coleira anda por ciclo",
    )
    parser.add_argument("--silencioso", action="store_true", help="não imprime cada passo")
    args = parser.parse_args(argv)

    if not args.token:
        print(
            "Falta o token da Base. Use --token, ou crie o .env do Central\n"
            "com BASE_TOKEN (cp .env.example .env).",
            file=sys.stderr,
        )
        return 2

    falhar = {c.strip().upper() for c in args.falhar.split(",") if c.strip()}
    rumos = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)]
    coleiras = [
        ColarSimulado(
            collar_id=identificador.strip().upper(),
            lat=args.base_lat,
            lon=args.base_lon,
            rumo_lat=rumos[indice % len(rumos)][0],
            rumo_lon=rumos[indice % len(rumos)][1],
            bateria=100 - indice * 7,
            vai_falhar=identificador.strip().upper() in falhar,
        )
        for indice, identificador in enumerate(args.coleiras.split(","))
        if identificador.strip()
    ]
    if not coleiras:
        print("Nenhuma coleira informada em --coleiras.", file=sys.stderr)
        return 2

    simulador = Simulador(
        url=args.url.rstrip("/"),
        base_id=args.base_id.upper(),
        token=args.token,
        intervalo=args.intervalo,
        atraso_confirmacao=args.atraso_confirmacao,
        coleiras=coleiras,
        passo_m=args.passo,
        verbose=not args.silencioso,
    )
    try:
        return simulador.rodar() or 0
    except KeyboardInterrupt:
        print("\nSimulador encerrado.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
