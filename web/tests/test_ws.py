"""Testes do WebSocket `/ws` (fase F6, item CT-07 do backlog).

Critério de aceite: "status de entrega atualiza sem recarregar".
"""

from __future__ import annotations


def test_ao_conectar_o_navegador_recebe_um_retrato_da_situacao(client):
    """Sem isso, a tela ficaria vazia até o próximo acontecimento — que
    pode levar minutos se o rebanho estiver parado."""
    with client.websocket_connect("/ws") as ws:
        mensagem = ws.receive_json()

    assert mensagem["type"] == "hello"
    assert mensagem["data"]["server_time"].endswith("Z")
    assert mensagem["data"]["active_fence_version"] is None
    assert mensagem["data"]["bases"][0]["base_id"] == "BASE01"


def test_o_retrato_inicial_traz_a_cerca_ativa(client, cerca_limpa):
    client.post("/api/fences", json=cerca_limpa)

    with client.websocket_connect("/ws") as ws:
        mensagem = ws.receive_json()

    assert mensagem["data"]["active_fence_version"] == 1


def test_heartbeat_da_base_chega_ao_navegador(client, base_headers):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # o "hello"

        client.post(
            "/api/edge/heartbeat",
            json={"base_id": "BASE01", "fence_version": None, "serial": "online", "queue_size": 3},
            headers=base_headers,
        )
        mensagem = ws.receive_json()

    assert mensagem["type"] == "base"
    assert mensagem["data"]["online"] is True
    assert mensagem["data"]["queue_size"] == 3


def test_mudanca_de_entrega_chega_ao_navegador(client, base_headers, cerca_limpa):
    """É o item CT-07: a linha do tempo se move sem recarregar a página."""
    cerca = client.post("/api/fences", json=cerca_limpa).json()

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello

        # A Base baixa a cerca: pending -> at_base, uma mensagem por coleira.
        client.get(f"/api/edge/fences/{cerca['version']}", headers=base_headers)
        primeira = ws.receive_json()

    assert primeira["type"] == "delivery"
    assert primeira["data"]["fence_version"] == cerca["version"]
    assert primeira["data"]["status"] == "at_base"
    assert primeira["data"]["status_label"] == "Na Base"


def test_confirmacao_de_coleira_chega_ao_navegador(client, base_headers, cerca_limpa):
    cerca = client.post("/api/fences", json=cerca_limpa).json()
    client.get(f"/api/edge/fences/{cerca['version']}", headers=base_headers)

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello

        client.post(
            "/api/edge/batch",
            json={"base_id": "BASE01", "items": [
                {"edge_seq": 1, "type": "delivery",
                 "data": {"collar_id": "COL01", "fence_version": cerca["version"],
                          "status": "confirmed", "crc32": cerca["crc32"]}},
            ]},
            headers=base_headers,
        )
        mensagem = ws.receive_json()

    assert mensagem["type"] == "delivery"
    assert mensagem["data"]["collar_id"] == "COL01"
    assert mensagem["data"]["status_label"] == "Confirmada"


def test_telemetria_e_evento_chegam_ao_navegador(client, base_headers):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello

        client.post(
            "/api/edge/batch",
            json={"base_id": "BASE01", "items": [
                {"edge_seq": 1, "type": "telemetry",
                 "data": {"collar_id": "COL01", "lat": -31.3062, "lon": -54.0639,
                          "zone": "ATENCAO", "battery_pct": 80}},
                {"edge_seq": 2, "type": "event",
                 "data": {"collar_id": "COL01", "kind": "zone_change", "zone": "ATENCAO"}},
            ]},
            headers=base_headers,
        )
        telemetria = ws.receive_json()
        evento = ws.receive_json()

    assert telemetria["type"] == "telemetry"
    assert telemetria["data"]["zone"] == "ATENCAO"
    assert evento["type"] == "event"
    assert evento["data"]["kind"] == "zone_change"


def test_os_quatro_tipos_da_secao_6_2_existem(client, base_headers, cerca_limpa):
    """A seção 6.2 define `delivery`, `telemetry`, `event` e `base`."""
    cerca = client.post("/api/fences", json=cerca_limpa).json()

    recebidos = set()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello

        client.post("/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers)
        recebidos.add(ws.receive_json()["type"])

        client.get(f"/api/edge/fences/{cerca['version']}", headers=base_headers)
        for _ in range(2):  # uma mensagem por coleira
            recebidos.add(ws.receive_json()["type"])

        client.post(
            "/api/edge/batch",
            json={"base_id": "BASE01", "items": [
                {"edge_seq": 1, "type": "telemetry",
                 "data": {"collar_id": "COL01", "zone": "SEGURO"}},
                {"edge_seq": 2, "type": "event",
                 "data": {"collar_id": "COL01", "kind": "zone_change", "zone": "SEGURO"}},
            ]},
            headers=base_headers,
        )
        recebidos.add(ws.receive_json()["type"])
        recebidos.add(ws.receive_json()["type"])

    assert recebidos == {"base", "delivery", "telemetry", "event"}


def test_dois_navegadores_recebem_a_mesma_mensagem(client, base_headers):
    """O produtor pode estar com o celular e o computador abertos."""
    with client.websocket_connect("/ws") as primeiro, client.websocket_connect("/ws") as segundo:
        primeiro.receive_json()
        segundo.receive_json()

        client.post("/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers)

        assert primeiro.receive_json()["type"] == "base"
        assert segundo.receive_json()["type"] == "base"


def test_o_sistema_funciona_sem_nenhum_navegador_conectado(client, base_headers, cerca_limpa):
    """O envio em difusão não pode derrubar a gravação do lote.

    Se a difusão falhasse com zero conexões, a Base perderia dados por
    causa de uma aba fechada.
    """
    cerca = client.post("/api/fences", json=cerca_limpa).json()

    resposta = client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [
            {"edge_seq": 1, "type": "delivery",
             "data": {"collar_id": "COL01", "fence_version": cerca["version"],
                      "status": "confirmed", "crc32": cerca["crc32"]}},
        ]},
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 1}
