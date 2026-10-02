"""Testes da API da Base (fase F4, item CT-05 do backlog).

Cobre o contrato I2 da seção 9: autenticação por token, heartbeat,
download da cerca na forma canônica, e lote idempotente.
"""

from __future__ import annotations

import pytest


# ===========================================================================
# Autenticação
# ===========================================================================


@pytest.mark.parametrize(
    "rota,metodo",
    [
        ("/api/edge/heartbeat", "post"),
        ("/api/edge/fences/1", "get"),
        ("/api/edge/batch", "post"),
    ],
)
def test_todas_as_rotas_da_base_exigem_token(client, rota, metodo):
    # O GET não leva corpo; o `json` só vai nas rotas POST.
    extra = {} if metodo == "get" else {"json": {"base_id": "BASE01"}}
    resposta = getattr(client, metodo)(rota, **extra)

    assert resposta.status_code == 401
    assert resposta.json() == {"detail": "missing_token"}


@pytest.mark.parametrize(
    "cabecalho",
    [
        "token-errado",                 # sem o esquema Bearer
        "Bearer ",                      # Bearer sem token
        "Bearer token-que-nao-existe",  # token inválido
        "Basic dXNlcjpwYXNz",           # esquema errado
    ],
)
def test_token_invalido_responde_401(client, cabecalho):
    resposta = client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01"},
        headers={"Authorization": cabecalho},
    )

    assert resposta.status_code == 401
    assert resposta.json()["detail"] in {"invalid_token", "missing_token"}


def test_base_id_diferente_do_token_responde_403(client, base_headers):
    """O token é válido, mas a identidade declarada não bate.

    Decisão da fase F0: com uma Base só isso nunca acontece, mas fecha
    de graça a porta de uma Base usar o token de outra.
    """
    resposta = client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE99"},
        headers=base_headers,
    )

    assert resposta.status_code == 403
    assert resposta.json() == {"detail": "base_id_mismatch"}


# ===========================================================================
# 9.1 Heartbeat
# ===========================================================================


def test_heartbeat_sem_cerca_ativa_responde_versao_nula(client, base_headers):
    resposta = client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 0, "serial": "online", "queue_size": 0},
        headers=base_headers,
    )

    assert resposta.status_code == 200
    dados = resposta.json()
    assert set(dados) == {"desired_fence_version", "server_time"}
    assert dados["desired_fence_version"] is None
    assert dados["server_time"].endswith("Z")


def test_heartbeat_informa_a_versao_desejada(client, base_headers, cerca_limpa):
    client.post("/api/fences", json=cerca_limpa)

    resposta = client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 0},
        headers=base_headers,
    )

    assert resposta.json()["desired_fence_version"] == 1


def test_heartbeat_guarda_o_estado_reportado_pela_base(client, base_headers):
    client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 3, "serial": "offline", "queue_size": 42},
        headers=base_headers,
    )

    base = client.app.state.db.get_base("BASE01")

    assert base["fence_version_reported"] == 3
    assert base["serial_status"] == "offline"
    assert base["queue_size"] == 42
    assert base["last_heartbeat"].endswith("Z")


def test_heartbeat_e_o_encontro_do_estado_desejado_com_o_reportado(client, base_headers, cerca_limpa):
    """A diferença entre os dois é o que dispara o download.

    É o que torna o sistema autocorretivo: depois de uma queda de rede,
    o primeiro heartbeat já revela o atraso.
    """
    client.post("/api/fences", json=cerca_limpa)
    client.post("/api/fences", json=dict(cerca_limpa, name="Piquete B"))

    resposta = client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 1},
        headers=base_headers,
    )

    assert resposta.json()["desired_fence_version"] == 2  # a Base está atrasada


# ===========================================================================
# 9.2 Download da cerca
# ===========================================================================


def test_download_entrega_a_cerca_na_forma_canonica(client, base_headers, cerca_limpa):
    """Para a Base vão SÓ inteiros: microgradus e centímetros.

    Nenhum grau com casa decimal atravessa esta fronteira, para o CRC
    bater em todas as camadas (seção 5.6 do planejamento).
    """
    client.post("/api/fences", json=cerca_limpa)

    resposta = client.get("/api/edge/fences/1", headers=base_headers)

    assert resposta.status_code == 200
    cerca = resposta.json()
    assert set(cerca) == {
        "version", "margin_attention_cm", "margin_critical_cm", "points_e6", "crc32"
    }
    assert cerca["margin_attention_cm"] == 800
    assert cerca["margin_critical_cm"] == 500
    assert cerca["points_e6"][0] == [-31305800, -54064600]
    assert all(isinstance(valor, int) for par in cerca["points_e6"] for valor in par)


def test_download_marca_as_entregas_como_na_base(client, base_headers, cerca_limpa):
    """O efeito colateral da seção 9.2.

    O download É a prova de que a cerca chegou à propriedade: sem ele, o
    produtor não distinguiria "a Base nem sabe da cerca" de "a Base sabe
    mas o rádio não entregou".
    """
    client.post("/api/fences", json=cerca_limpa)
    antes = client.get("/api/fences/1/deliveries").json()
    assert all(e["status"] == "pending" for e in antes)

    client.get("/api/edge/fences/1", headers=base_headers)

    depois = client.get("/api/fences/1/deliveries").json()
    assert all(e["status"] == "at_base" for e in depois)
    assert all(e["status_label"] == "Na Base" for e in depois)


def test_download_de_versao_inexistente_responde_404(client, base_headers):
    resposta = client.get("/api/edge/fences/99", headers=base_headers)

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "fence_not_found"}


def test_download_repetido_nao_faz_entrega_voltar_atras(client, base_headers, cerca_limpa):
    """Só `pending` avança para `at_base`. Uma entrega que já estava
    transmitindo não volta por causa de um novo download."""
    client.post("/api/fences", json=cerca_limpa)
    client.get("/api/edge/fences/1", headers=base_headers)
    client.post(
        "/api/edge/batch",
        json={
            "base_id": "BASE01",
            "items": [
                {"edge_seq": 1, "type": "delivery",
                 "data": {"collar_id": "COL01", "fence_version": 1, "status": "transmitting"}}
            ],
        },
        headers=base_headers,
    )

    client.get("/api/edge/fences/1", headers=base_headers)

    entregas = {e["collar_id"]: e for e in client.get("/api/fences/1/deliveries").json()}
    assert entregas["COL01"]["status"] == "transmitting"


# ===========================================================================
# 9.3 Lote
# ===========================================================================


def lote(*itens) -> dict:
    return {"base_id": "BASE01", "items": list(itens)}


def telemetria(seq: int, **extra) -> dict:
    dados = {
        "collar_id": "COL01",
        "ts": "2026-10-06T13:01:58Z",
        "lat": -31.306119, "lon": -54.063935,
        "zone": "SEGURO", "satellites": 12, "hdop": 0.9,
        "battery_pct": 87, "fence_version": 1, "rssi": -92, "snr": 7.5,
    }
    dados.update(extra)
    return {"edge_seq": seq, "type": "telemetry", "data": dados}


def test_lote_vazio_e_aceito(client, base_headers):
    resposta = client.post("/api/edge/batch", json=lote(), headers=base_headers)

    assert resposta.status_code == 200
    assert resposta.json() == {"acked_up_to": 0}


def test_lote_grava_telemetria_e_atualiza_a_coleira(client, base_headers):
    resposta = client.post("/api/edge/batch", json=lote(telemetria(1)), headers=base_headers)

    assert resposta.json() == {"acked_up_to": 1}
    coleira = next(c for c in client.app.state.db.list_collars() if c["collar_id"] == "COL01")
    assert coleira["last_zone"] == "SEGURO"
    assert coleira["battery_pct"] == 87
    assert coleira["last_lat"] == pytest.approx(-31.306119)
    assert coleira["fence_version_reported"] == 1


def test_lote_e_idempotente(client, base_headers):
    """A Base reenvia até receber confirmação. Se a resposta se perder na
    volta, ela manda tudo de novo — e não pode gravar duas vezes.

    É a semântica "pelo menos uma vez" da seção 8.2 do planejamento:
    nada se perde e nada se duplica.
    """
    for _ in range(3):
        resposta = client.post(
            "/api/edge/batch",
            json=lote(telemetria(1), telemetria(2), telemetria(3)),
            headers=base_headers,
        )
        assert resposta.json() == {"acked_up_to": 3}

    with client.app.state.db.connect() as conn:
        quantidade = conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"]
    assert quantidade == 3  # e não 9


def test_acked_up_to_e_o_maior_seq_continuo(client, base_headers):
    """A palavra "contínuo" é o ponto.

    Se chegaram 1, 2, 3 e 7, o confirmado é 3 — não 7. O item 7 está
    gravado e não será gravado de novo, mas a Base precisa continuar
    guardando o 4, o 5 e o 6, que ainda não chegaram.
    """
    resposta = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1), telemetria(2), telemetria(3), telemetria(7)),
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 3}

    # Preenchendo o buraco, o confirmado salta para o fim.
    resposta = client.post(
        "/api/edge/batch",
        json=lote(telemetria(4), telemetria(5), telemetria(6)),
        headers=base_headers,
    )
    assert resposta.json() == {"acked_up_to": 7}


def test_lote_fora_de_ordem_e_processado_na_ordem_da_sequencia(client, base_headers):
    """O estado de uma entrega depende da sequência, então a ordem do
    JSON não pode decidir o resultado."""
    client.post("/api/fences", json={
        "name": "P", "margin_attention_m": 8.0, "margin_critical_m": 5.0,
        "points": [
            {"lat": -31.3058, "lon": -54.0646}, {"lat": -31.3058, "lon": -54.0633},
            {"lat": -31.3066, "lon": -54.0633}, {"lat": -31.3066, "lon": -54.0646},
        ],
    })
    crc = client.get("/api/fences/1").json()["crc32"]

    client.post(
        "/api/edge/batch",
        json=lote(
            {"edge_seq": 2, "type": "delivery",
             "data": {"collar_id": "COL01", "fence_version": 1, "status": "confirmed", "crc32": crc}},
            {"edge_seq": 1, "type": "delivery",
             "data": {"collar_id": "COL01", "fence_version": 1, "status": "transmitting"}},
        ),
        headers=base_headers,
    )

    entregas = {e["collar_id"]: e for e in client.get("/api/fences/1/deliveries").json()}
    assert entregas["COL01"]["status"] == "confirmed"


def test_coleira_desconhecida_e_cadastrada_automaticamente(client, base_headers):
    """Seção 7: coleiras também são cadastradas "quando aparecem na
    telemetria". Evita perder dados de uma coleira nova só porque
    ninguém lembrou de pôr no COLARES_CONHECIDOS."""
    client.post("/api/edge/batch", json=lote(telemetria(1, collar_id="COL99")), headers=base_headers)

    identificadores = [c["collar_id"] for c in client.app.state.db.list_collars()]
    assert "COL99" in identificadores


def test_zona_desconhecida_nao_derruba_o_lote(client, base_headers):
    """Decisão da fase F4: a telemetria é gravada com zona vazia e um
    evento `invalid_zone` é registrado.

    Recusar o lote criaria um item envenenado: a Base reenviaria para
    sempre o mesmo lote e pararia de entregar todo o resto.
    """
    resposta = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1, zone="VERDE")),
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 1}
    with client.app.state.db.connect() as conn:
        linha = conn.execute("SELECT zone FROM telemetry").fetchone()
        evento = conn.execute("SELECT * FROM events WHERE kind = 'invalid_zone'").fetchone()
    assert linha["zone"] is None
    assert evento is not None
    assert "VERDE" in evento["detail"]


def test_central_nao_recalcula_zona(client, base_headers):
    """A zona vem decidida pela coleira e é apenas gravada.

    Mandamos uma posição no CENTRO da cerca dizendo que a zona é FORA.
    O Central grava FORA, porque não é ele quem decide.
    """
    client.post("/api/fences", json={
        "name": "P", "margin_attention_m": 8.0, "margin_critical_m": 5.0,
        "points": [
            {"lat": -31.3058, "lon": -54.0646}, {"lat": -31.3058, "lon": -54.0633},
            {"lat": -31.3066, "lon": -54.0633}, {"lat": -31.3066, "lon": -54.0646},
        ],
    })

    client.post(
        "/api/edge/batch",
        json=lote(telemetria(1, lat=-31.3062, lon=-54.06395, zone="FORA")),
        headers=base_headers,
    )

    coleira = next(c for c in client.app.state.db.list_collars() if c["collar_id"] == "COL01")
    assert coleira["last_zone"] == "FORA"


def test_lote_grava_eventos(client, base_headers):
    client.post(
        "/api/edge/batch",
        json=lote({
            "edge_seq": 1, "type": "event",
            "data": {"collar_id": "COL01", "kind": "zone_change",
                     "zone": "ATENCAO", "ts": "2026-10-06T13:02:00Z"},
        }),
        headers=base_headers,
    )

    with client.app.state.db.connect() as conn:
        evento = dict(conn.execute("SELECT * FROM events").fetchone())
    assert evento["kind"] == "zone_change"
    assert evento["zone"] == "ATENCAO"
    assert evento["collar_id"] == "COL01"


def test_tipo_de_item_desconhecido_e_recusado_pelo_modelo(client, base_headers):
    resposta = client.post(
        "/api/edge/batch",
        json=lote({"edge_seq": 1, "type": "fofoca", "data": {}}),
        headers=base_headers,
    )

    assert resposta.status_code == 422


# ===========================================================================
# Estados de entrega relatados pela Base (seção 8)
# ===========================================================================


@pytest.fixture
def cerca_criada(client, cerca_limpa) -> dict:
    """Cria uma cerca e devolve o dicionário dela, já baixada pela Base."""
    client.post("/api/fences", json=cerca_limpa)
    return client.get("/api/fences/1").json()


def relatar(client, base_headers, seq, **dados):
    corpo = {"collar_id": "COL01", "fence_version": 1, **dados}
    return client.post(
        "/api/edge/batch",
        json=lote({"edge_seq": seq, "type": "delivery", "data": corpo}),
        headers=base_headers,
    )


def entrega_de(client, collar_id="COL01", versao=1) -> dict:
    return next(
        e for e in client.get(f"/api/fences/{versao}/deliveries").json()
        if e["collar_id"] == collar_id
    )


def test_confirmacao_com_crc_igual_vira_confirmada(client, base_headers, cerca_criada):
    """É a regra que dá sentido ao sistema: "Confirmada" significa que a
    coleira tem EXATAMENTE a cerca que o produtor desenhou."""
    relatar(client, base_headers, 1, status="transmitting")
    relatar(client, base_headers, 2, status="confirmed", crc32=cerca_criada["crc32"])

    entrega = entrega_de(client)
    assert entrega["status"] == "confirmed"
    assert entrega["status_label"] == "Confirmada"
    assert entrega["crc32_reported"] == cerca_criada["crc32"]
    assert entrega["detail"] is None


def test_confirmacao_com_crc_diferente_vira_falhou(client, base_headers, cerca_criada):
    relatar(client, base_headers, 1, status="confirmed", crc32="DEADBEEF")

    entrega = entrega_de(client)
    assert entrega["status"] == "failed"
    assert entrega["detail"] == "crc_mismatch"
    assert entrega["crc32_reported"] == "DEADBEEF"


def test_confirmacao_sem_crc_vira_falhou(client, base_headers, cerca_criada):
    """Caso que a especificação não previu, decidido na fase F0.

    Assumir sucesso sem prova seria pior: o produtor veria "Confirmada"
    sem ninguém ter conferido nada.
    """
    relatar(client, base_headers, 1, status="confirmed")

    entrega = entrega_de(client)
    assert entrega["status"] == "failed"
    assert entrega["detail"] == "crc_missing"


def test_crc_em_minusculas_tambem_confere(client, base_headers, cerca_criada):
    """Tolerância só na comparação: guardamos sempre em maiúsculas."""
    relatar(client, base_headers, 1, status="confirmed", crc32=cerca_criada["crc32"].lower())

    assert entrega_de(client)["status"] == "confirmed"


def test_tentativas_sao_contadas_a_cada_transmissao(client, base_headers, cerca_criada):
    relatar(client, base_headers, 1, status="transmitting")
    relatar(client, base_headers, 2, status="failed")
    relatar(client, base_headers, 3, status="transmitting")

    assert entrega_de(client)["attempts"] == 2


def test_volta_de_falhou_para_transmitindo_e_aceita(client, base_headers, cerca_criada):
    """O reenvio legítimo da seção 8.5 do planejamento: a coleira
    reaparece com versão antiga e o rádio tenta de novo."""
    relatar(client, base_headers, 1, status="failed")
    relatar(client, base_headers, 2, status="transmitting")

    assert entrega_de(client)["status"] == "transmitting"


def test_entrega_confirmada_nao_e_reaberta_por_pacote_atrasado(client, base_headers, cerca_criada):
    """Um pacote atrasado não pode fazer a tela piscar de "Confirmada"
    para "Transmitindo" sem nada ter acontecido."""
    relatar(client, base_headers, 1, status="confirmed", crc32=cerca_criada["crc32"])
    relatar(client, base_headers, 2, status="transmitting")

    assert entrega_de(client)["status"] == "confirmed"


def test_estado_que_a_base_nao_pode_relatar_e_ignorado(client, base_headers, cerca_criada):
    """`pending` e `at_base` são decisão do Central, não da Base."""
    relatar(client, base_headers, 1, status="pending")

    assert entrega_de(client)["status"] == "pending"  # não mudou nada


def test_relato_de_versao_antiga_vai_para_o_log_sem_mexer_na_cerca_ativa(
    client, base_headers, cerca_limpa
):
    """Seção 8: "Status de versões antigas que chegarem atrasados são
    registrados no log, mas não mexem na cerca ativa"."""
    client.post("/api/fences", json=cerca_limpa)
    antiga = client.get("/api/fences/1").json()
    client.post("/api/fences", json=dict(cerca_limpa, name="Piquete B"))

    relatar(client, base_headers, 1, status="confirmed", crc32=antiga["crc32"])

    assert entrega_de(client, versao=1)["status"] == "pending"  # intocada
    assert "late report confirmed" in client.get("/api/fences/1/log").text


def test_relato_sobre_cerca_inexistente_e_ignorado(client, base_headers):
    resposta = relatar(client, base_headers, 1, status="confirmed", crc32="DA29321E", fence_version=99)

    assert resposta.json() == {"acked_up_to": 1}  # o item foi recebido


def test_cada_mudanca_de_entrega_vai_para_o_log_da_cerca(client, base_headers, cerca_criada):
    relatar(client, base_headers, 1, status="transmitting")
    relatar(client, base_headers, 2, status="confirmed", crc32=cerca_criada["crc32"])

    texto = client.get("/api/fences/1/log").text

    assert "delivery collar=COL01 pending" in texto
    assert "delivery collar=COL01 transmitting" in texto
    assert "delivery collar=COL01 confirmed" in texto


def test_acked_up_to_funciona_com_a_sequencia_da_especificacao(client, base_headers):
    """O exemplo literal da seção 9.3: itens 1531, 1532 e 1533.

    Este teste nasceu de um defeito real. A primeira versão de
    `acked_up_to()` exigia que a sequência começasse em 1 e respondia 0
    aqui — e a Base reenviaria a fila inteira para sempre, sem nunca
    conseguir limpá-la.

    A fila local da Base é persistente e só cresce: ela apaga o que já foi
    confirmado, então depois de um tempo em operação o menor item que o
    Central tem é 1531, não 1.
    """
    resposta = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1531), telemetria(1532), telemetria(1533)),
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 1533}


def test_acked_up_to_com_buraco_no_meio_de_uma_sequencia_alta(client, base_headers):
    """A continuidade é contada do menor item que temos até o primeiro buraco."""
    resposta = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1531), telemetria(1532), telemetria(1540)),
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 1532}


def test_a_base_consegue_limpar_a_fila_e_continuar(client, base_headers):
    """O ciclo real: envia, recebe acked_up_to, apaga, envia os próximos."""
    primeiro = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1531), telemetria(1532)),
        headers=base_headers,
    ).json()
    assert primeiro == {"acked_up_to": 1532}

    segundo = client.post(
        "/api/edge/batch",
        json=lote(telemetria(1533), telemetria(1534)),
        headers=base_headers,
    ).json()
    assert segundo == {"acked_up_to": 1534}
