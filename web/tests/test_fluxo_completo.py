"""O fluxo completo: produtor desenha → Base baixa → Base confirma (fase F4).

Este é o teste que amarra o sistema inteiro. Ele reproduz, com o
TestClient, exatamente o ciclo que a Base de verdade executa (seção 7.2
do planejamento) e confere cada passo pelo lado do produtor.

É o critério "Pronto quando" da seção 15 da especificação:

    o produtor desenha → valida → envia → a Base baixa → confirma →
    a tela mostra "Confirmada"

O teste também usa o SIMULADOR como implementação independente da forma
canônica, para provar que o CRC do Central é reproduzível por quem não
compartilha o código dele — que é a situação do firmware.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# O simulador é um script de linha de comando, fora do pacote `backend`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import simulador_base  # noqa: E402

CERCA = {
    "name": "Piquete do açude",
    "margin_attention_m": 8.0,
    "margin_critical_m": 5.0,
    "points": [
        {"lat": -31.305800, "lon": -54.064600},
        {"lat": -31.305800, "lon": -54.063300},
        {"lat": -31.306600, "lon": -54.063300},
        {"lat": -31.306600, "lon": -54.064600},
    ],
}


def test_fluxo_completo_do_desenho_ate_confirmada(client, base_headers, settings):
    """O caminho inteiro, passo a passo, como no marco de 16/10."""

    # --- Passo 1: o produtor confere a cerca antes de enviar ----------
    validacao = client.post("/api/fences/validate", json=CERCA).json()
    assert validacao["valid"] is True
    assert validacao["violations"] == []
    assert validacao["orientation"] == "clockwise"

    # --- Passo 2: o produtor envia -------------------------------------
    criada = client.post("/api/fences", json=CERCA)
    assert criada.status_code == 201
    cerca = criada.json()
    versao, crc = cerca["version"], cerca["crc32"]

    # Todas as coleiras conhecidas aparecem como "Salva".
    entregas = client.get(f"/api/fences/{versao}/deliveries").json()
    assert {e["status_label"] for e in entregas} == {"Salva"}
    assert [e["collar_id"] for e in entregas] == list(settings.known_collars)

    # --- Passo 3: a Base dá sinal de vida e descobre a cerca nova -----
    batida = client.post(
        "/api/edge/heartbeat",
        json={"base_id": settings.base_id, "fence_version": None,
              "serial": "online", "queue_size": 0},
        headers=base_headers,
    ).json()
    assert batida["desired_fence_version"] == versao

    # --- Passo 4: a Base baixa e CONFERE o CRC por conta própria ------
    para_a_base = client.get(f"/api/edge/fences/{versao}", headers=base_headers).json()

    recalculado = simulador_base.crc32_hex(
        simulador_base.bytes_canonicos(
            para_a_base["version"],
            para_a_base["margin_attention_cm"],
            para_a_base["margin_critical_cm"],
            para_a_base["points_e6"],
        )
    )
    assert recalculado == crc, "o CRC do Central não é reproduzível por outra implementação"

    # O download marca as entregas como "Na Base".
    entregas = client.get(f"/api/fences/{versao}/deliveries").json()
    assert {e["status_label"] for e in entregas} == {"Na Base"}

    # --- Passo 5: a Base transmite pelo rádio --------------------------
    itens = [
        {"edge_seq": indice, "type": "delivery",
         "data": {"collar_id": collar_id, "fence_version": versao, "status": "transmitting"}}
        for indice, collar_id in enumerate(settings.known_collars, start=1)
    ]
    resposta = client.post(
        "/api/edge/batch",
        json={"base_id": settings.base_id, "items": itens},
        headers=base_headers,
    )
    assert resposta.json() == {"acked_up_to": len(itens)}

    entregas = client.get(f"/api/fences/{versao}/deliveries").json()
    assert {e["status_label"] for e in entregas} == {"Transmitindo"}

    # --- Passo 6: as coleiras confirmam, com o CRC que calcularam -----
    seguinte = len(itens) + 1
    itens = [
        {"edge_seq": seguinte + indice, "type": "delivery",
         "data": {"collar_id": collar_id, "fence_version": versao,
                  "status": "confirmed", "crc32": recalculado}}
        for indice, collar_id in enumerate(settings.known_collars)
    ]
    client.post(
        "/api/edge/batch",
        json={"base_id": settings.base_id, "items": itens},
        headers=base_headers,
    )

    # --- Passo 7: a tela do produtor mostra "Confirmada" --------------
    entregas = client.get(f"/api/fences/{versao}/deliveries").json()
    assert {e["status_label"] for e in entregas} == {"Confirmada"}
    assert all(e["crc32_reported"] == crc for e in entregas)

    # --- Passo 8: o log serve de evidência para o relatório -----------
    log = client.get(f"/api/fences/{versao}/log").text
    assert "Piquete do açude" in log
    assert crc in log
    for collar_id in settings.known_collars:
        assert f"delivery collar={collar_id} pending" in log
        assert f"delivery collar={collar_id} transmitting" in log
        assert f"delivery collar={collar_id} confirmed" in log
    assert "downloaded fence" in log


def test_fluxo_com_uma_coleira_que_falha(client, base_headers, settings):
    """O que o produtor vê quando uma coleira não responde.

    É o cenário de `--falhar COL02` no simulador, e o que vai acontecer
    de verdade quando uma coleira estiver fora de alcance.
    """
    cerca = client.post("/api/fences", json=CERCA).json()
    versao, crc = cerca["version"], cerca["crc32"]
    client.get(f"/api/edge/fences/{versao}", headers=base_headers)

    client.post(
        "/api/edge/batch",
        json={
            "base_id": settings.base_id,
            "items": [
                {"edge_seq": 1, "type": "delivery",
                 "data": {"collar_id": "COL01", "fence_version": versao,
                          "status": "confirmed", "crc32": crc}},
                {"edge_seq": 2, "type": "delivery",
                 "data": {"collar_id": "COL02", "fence_version": versao,
                          "status": "failed", "detail": "sem resposta da coleira"}},
            ],
        },
        headers=base_headers,
    )

    entregas = {e["collar_id"]: e for e in client.get(f"/api/fences/{versao}/deliveries").json()}

    assert entregas["COL01"]["status_label"] == "Confirmada"
    assert entregas["COL02"]["status_label"] == "Falhou"
    # O produtor consegue saber QUAL coleira ficou de fora e por quê.
    assert "COL02" in client.get(f"/api/fences/{versao}/log").text


def test_fluxo_com_coleira_que_devolve_crc_errado(client, base_headers):
    """O caso que o CRC existe para pegar: a coleira respondeu, mas o que
    ela guardou não é a cerca que o produtor desenhou.

    O relatório da IP1 registrou mensagens corrompidas em SNR baixo, então
    isto não é hipótese remota.
    """
    cerca = client.post("/api/fences", json=CERCA).json()
    client.get(f"/api/edge/fences/{cerca['version']}", headers=base_headers)

    client.post(
        "/api/edge/batch",
        json={
            "base_id": "BASE01",
            "items": [
                {"edge_seq": 1, "type": "delivery",
                 "data": {"collar_id": "COL01", "fence_version": cerca["version"],
                          "status": "confirmed", "crc32": "0BADC0DE"}},
            ],
        },
        headers=base_headers,
    )

    entrega = next(
        e for e in client.get(f"/api/fences/{cerca['version']}/deliveries").json()
        if e["collar_id"] == "COL01"
    )

    # "Confirmada" com CRC diferente NÃO é confirmada.
    assert entrega["status_label"] == "Falhou"
    assert entrega["detail"] == "crc_mismatch"
    assert "crc_mismatch" in client.get(f"/api/fences/{cerca['version']}/log").text


def test_cerca_nova_substitui_a_anterior_no_meio_da_entrega(client, base_headers, settings):
    """O produtor corrige a cerca antes da entrega terminar.

    A versão nova vira a desejada; a entrega da antiga fica como estava e
    não é reaberta por relatos atrasados.
    """
    primeira = client.post("/api/fences", json=CERCA).json()
    client.get(f"/api/edge/fences/{primeira['version']}", headers=base_headers)

    segunda = client.post("/api/fences", json=dict(CERCA, name="Piquete corrigido")).json()

    batida = client.post(
        "/api/edge/heartbeat",
        json={"base_id": settings.base_id, "fence_version": primeira["version"]},
        headers=base_headers,
    ).json()

    assert batida["desired_fence_version"] == segunda["version"]
    assert client.get("/api/fences/active").json()["version"] == segunda["version"]
    # A entrega da primeira parou onde estava.
    assert all(
        e["status"] == "at_base"
        for e in client.get(f"/api/fences/{primeira['version']}/deliveries").json()
    )


def test_a_base_sobrevive_a_uma_queda_de_rede(client, base_headers, settings):
    """Sem rede, a fila local cresce; quando a conexão volta, tudo sobe.

    Simulamos a queda acumulando itens e enviando todos de uma vez — é o
    que o `--intervalo` do simulador faz quando o Central está fora.
    """
    cerca = client.post("/api/fences", json=CERCA).json()
    client.get(f"/api/edge/fences/{cerca['version']}", headers=base_headers)

    # 10 minutos de telemetria acumulada na fila local.
    fila = [
        {"edge_seq": seq, "type": "telemetry",
         "data": {"collar_id": "COL01", "ts": "2026-10-06T13:00:00Z",
                  "lat": -31.3062, "lon": -54.0639, "zone": "SEGURO",
                  "battery_pct": 90, "fence_version": cerca["version"]}}
        for seq in range(1, 121)
    ]
    fila.append({
        "edge_seq": 121, "type": "delivery",
        "data": {"collar_id": "COL01", "fence_version": cerca["version"],
                 "status": "confirmed", "crc32": cerca["crc32"]},
    })

    resposta = client.post(
        "/api/edge/batch",
        json={"base_id": settings.base_id, "items": fila},
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 121}
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"] == 120
    assert next(
        e for e in client.get(f"/api/fences/{cerca['version']}/deliveries").json()
        if e["collar_id"] == "COL01"
    )["status"] == "confirmed"


def test_o_simulador_concorda_com_o_central_na_forma_canonica():
    """O simulador reimplementa a forma canônica DE PROPÓSITO.

    Se ele importasse `backend.services.canonical`, a conferência de CRC
    não provaria nada — seria o mesmo código conferindo a si mesmo. Com
    duas implementações independentes chegando ao mesmo valor, o contrato
    está de fato reproduzível, que é o que o firmware precisa.
    """
    from backend.services.canonical import canonical_bytes
    from backend.services.crc import crc32_hex

    pontos = [
        [-31306000, -54064200], [-31306000, -54063700],
        [-31306400, -54063700], [-31306400, -54064200],
    ]

    do_central = crc32_hex(canonical_bytes(1, 500, 200, pontos))
    do_simulador = simulador_base.crc32_hex(
        simulador_base.bytes_canonicos(1, 500, 200, pontos)
    )

    assert do_central == do_simulador == "DA29321E"


@pytest.mark.parametrize("graus,esperado", [(0.0000005, 1), (-0.0000005, -1), (-31.306, -31306000)])
def test_o_simulador_arredonda_igual_ao_central(graus, esperado):
    """O arredondamento "meio para longe do zero" também precisa bater."""
    from backend.services.canonical import to_e6

    assert simulador_base.graus_para_e6(graus) == to_e6(graus) == esperado
