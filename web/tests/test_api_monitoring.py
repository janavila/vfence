"""Testes das rotas de rebanho, eventos, Base e configuração (fase F6).

Também cobre a rota `/api/settings`, acrescentada na fase F5 porque o
editor não funciona sem conhecer a posição da Base e os limites do
`.env`.
"""

from __future__ import annotations

import pytest


def lote_de_telemetria(seq, **extra):
    dados = {
        "collar_id": "COL01", "ts": "2026-10-06T13:01:58Z",
        "lat": -31.306119, "lon": -54.063935, "zone": "SEGURO",
        "satellites": 12, "hdop": 0.9, "battery_pct": 87,
        "fence_version": 1, "rssi": -92, "snr": 7.5,
    }
    dados.update(extra)
    return {"edge_seq": seq, "type": "telemetry", "data": dados}


# ===========================================================================
# GET /api/settings
# ===========================================================================


def test_settings_entrega_o_que_a_tela_precisa(client, settings):
    resposta = client.get("/api/settings")

    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["base_lat"] == pytest.approx(settings.base_lat)
    assert dados["base_lon"] == pytest.approx(settings.base_lon)
    assert dados["margin_attention_default_m"] == settings.margin_attention_default_m
    assert dados["known_collars"] == list(settings.known_collars)
    assert (dados["min_points"], dados["max_points"]) == (3, 32)


def test_settings_calcula_a_folga_recomendada_do_gps(client):
    """1,5 × 3,0 m = 4,5 m. É o número que aparece no aviso VAL-12."""
    dados = client.get("/api/settings").json()

    assert dados["recommended_critical_margin_m"] == pytest.approx(4.5)


def test_settings_nunca_vaza_segredo(client):
    """Teste de guarda: se alguém acrescentar o token ou a senha ao modelo
    por descuido, este teste falha antes de ir para o repositório."""
    corpo = client.get("/api/settings").text.lower()

    for proibido in ("token", "password", "senha", "secret"):
        assert proibido not in corpo


# ===========================================================================
# GET /api/collars
# ===========================================================================


def test_collars_lista_as_coleiras_cadastradas(client, settings):
    coleiras = client.get("/api/collars").json()

    assert [c["collar_id"] for c in coleiras] == list(settings.known_collars)
    # Nada inventado: sem telemetria, não há zona nem posição.
    assert all(c["last_zone"] is None for c in coleiras)
    assert all(c["outdated"] is False for c in coleiras)


def test_collars_mostra_o_que_a_coleira_relatou(client, base_headers):
    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [lote_de_telemetria(1)]},
        headers=base_headers,
    )

    coleira = next(c for c in client.get("/api/collars").json() if c["collar_id"] == "COL01")

    assert coleira["last_zone"] == "SEGURO"
    assert coleira["battery_pct"] == 87
    assert coleira["rssi"] == -92
    assert coleira["snr"] == pytest.approx(7.5)
    assert coleira["last_seen"] == "2026-10-06T13:01:58Z"


def test_coleira_com_cerca_antiga_aparece_como_desatualizada(client, base_headers, cerca_limpa):
    """É o "estado desejado × estado reportado" da seção 3.2 do
    planejamento, aparecendo na tela como "desatualizada"."""
    client.post("/api/fences", json=cerca_limpa)                      # versão 1
    client.post("/api/fences", json=dict(cerca_limpa, name="Outra"))  # versão 2

    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [lote_de_telemetria(1, fence_version=1)]},
        headers=base_headers,
    )

    coleira = next(c for c in client.get("/api/collars").json() if c["collar_id"] == "COL01")

    assert coleira["fence_version_reported"] == 1
    assert coleira["outdated"] is True


def test_coleira_com_a_cerca_certa_nao_aparece_como_desatualizada(client, base_headers, cerca_limpa):
    client.post("/api/fences", json=cerca_limpa)

    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [lote_de_telemetria(1, fence_version=1)]},
        headers=base_headers,
    )

    coleira = next(c for c in client.get("/api/collars").json() if c["collar_id"] == "COL01")
    assert coleira["outdated"] is False


# ===========================================================================
# GET /api/collars/{id}/telemetry
# ===========================================================================


def test_telemetria_vem_do_mais_recente_para_o_mais_antigo(client, base_headers):
    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [
            lote_de_telemetria(1, battery_pct=90),
            lote_de_telemetria(2, battery_pct=89),
            lote_de_telemetria(3, battery_pct=88),
        ]},
        headers=base_headers,
    )

    historico = client.get("/api/collars/COL01/telemetry").json()

    assert [t["battery_pct"] for t in historico] == [88, 89, 90]


def test_telemetria_respeita_o_limite(client, base_headers):
    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [lote_de_telemetria(s) for s in range(1, 11)]},
        headers=base_headers,
    )

    assert len(client.get("/api/collars/COL01/telemetry?limit=4").json()) == 4


def test_telemetria_de_coleira_inexistente_responde_404(client):
    resposta = client.get("/api/collars/COL99/telemetry")

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "collar_not_found"}


@pytest.mark.parametrize("limite", [0, 1001, -1])
def test_limite_fora_da_faixa_e_recusado(client, limite):
    """Mesma faixa do Monitor: mínimo 1, máximo 1000 (app/main.py:87)."""
    assert client.get(f"/api/collars/COL01/telemetry?limit={limite}").status_code == 422


# ===========================================================================
# GET /api/events
# ===========================================================================


def test_eventos_vem_do_mais_recente_para_o_mais_antigo(client, base_headers):
    itens = [
        {"edge_seq": i, "type": "event",
         "data": {"collar_id": "COL01", "kind": "zone_change", "zone": zona,
                  "ts": f"2026-10-06T13:0{i}:00Z"}}
        for i, zona in enumerate(["ATENCAO", "CRITICO", "FORA"], start=1)
    ]
    client.post("/api/edge/batch", json={"base_id": "BASE01", "items": itens}, headers=base_headers)

    eventos = client.get("/api/events").json()

    assert [e["zone"] for e in eventos] == ["FORA", "CRITICO", "ATENCAO"]


def test_eventos_podem_ser_filtrados_por_coleira(client, base_headers):
    """Atende a seção 11.4: "linha do tempo com filtro por coleira"."""
    client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [
            {"edge_seq": 1, "type": "event",
             "data": {"collar_id": "COL01", "kind": "zone_change", "zone": "FORA"}},
            {"edge_seq": 2, "type": "event",
             "data": {"collar_id": "COL02", "kind": "zone_change", "zone": "ATENCAO"}},
        ]},
        headers=base_headers,
    )

    somente_col01 = client.get("/api/events?collar_id=COL01").json()

    assert len(somente_col01) == 1
    assert somente_col01[0]["collar_id"] == "COL01"
    assert len(client.get("/api/events").json()) == 2


# ===========================================================================
# GET /api/bases
# ===========================================================================


def test_base_recem_cadastrada_aparece_offline(client, settings):
    """Nunca houve heartbeat: a Base não pode aparecer como ligada."""
    bases = client.get("/api/bases").json()

    assert len(bases) == 1
    assert bases[0]["base_id"] == settings.base_id
    assert bases[0]["online"] is False
    assert bases[0]["last_heartbeat"] is None


def test_base_fica_online_depois_do_heartbeat(client, base_headers):
    client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": None, "serial": "online", "queue_size": 7},
        headers=base_headers,
    )

    base = client.get("/api/bases").json()[0]

    assert base["online"] is True
    assert base["serial_status"] == "online"
    assert base["queue_size"] == 7
    assert base["last_heartbeat"].endswith("Z")


def test_base_com_cerca_antiga_aparece_como_desatualizada(client, base_headers, cerca_limpa):
    client.post("/api/fences", json=cerca_limpa)
    client.post("/api/fences", json=dict(cerca_limpa, name="Outra"))
    client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 1},
        headers=base_headers,
    )

    base = client.get("/api/bases").json()[0]

    assert base["fence_version_reported"] == 1
    assert base["fence_version_active"] == 2
    assert base["outdated"] is True


def test_bases_nunca_devolve_o_hash_do_token(client, base_headers):
    """O hash não é segredo como o token, mas também não tem por que sair:
    quanto menos superfície, melhor."""
    client.post("/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers)

    corpo = client.get("/api/bases").text.lower()

    assert "token" not in corpo
    assert "hash" not in corpo


def test_base_fica_offline_depois_do_prazo(client, base_headers):
    """`BASE_OFFLINE_APOS_S` segundos sem heartbeat = offline (seção 9.1).

    Em vez de esperar 30 segundos, passamos para a função um instante no
    futuro. É por isso que `base_is_online()` recebe `now` como
    parâmetro: deixa o tempo testável sem substituir o módulo `datetime`.
    """
    from datetime import datetime, timedelta, timezone

    from backend.services import edge_service

    client.post("/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers)
    assert client.get("/api/bases").json()[0]["online"] is True

    base = client.app.state.db.get_base("BASE01")
    settings = client.app.state.settings
    limite = settings.base_offline_after_s

    agora = datetime.now(timezone.utc)
    # Dentro do prazo: continua online.
    assert edge_service.base_is_online(base, settings, agora + timedelta(seconds=limite - 1)) is True
    # Passado o prazo: offline.
    assert edge_service.base_is_online(base, settings, agora + timedelta(seconds=limite + 5)) is False


def test_hora_de_heartbeat_em_formato_estranho_conta_como_offline(client, settings):
    """Uma linha estranha no banco não pode derrubar a tela."""
    from backend.services import edge_service

    assert edge_service.base_is_online({"base_id": "X", "last_heartbeat": "ontem"}, settings) is False
    assert edge_service.base_is_online({"base_id": "X", "last_heartbeat": None}, settings) is False
