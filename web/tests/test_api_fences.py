"""Testes da API de cercas (fase F3, itens CT-04 e CT-06).

Cobre: validar sem salvar, criar, versões sempre crescentes, avisos que
exigem aceite, cerca ativa única, histórico, reativação, entregas, o
arquivo de log e a exportação GeoJSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CASOS = json.loads(
    (Path(__file__).parent / "casos_geometricos.json").read_text(encoding="utf-8")
)


def caso(nome: str) -> dict:
    return next(c for c in CASOS["casos"] if c["nome"] == nome)


def corpo(nome: str, **extra) -> dict:
    """Monta um `FenceIn` a partir de um caso compartilhado."""
    c = caso(nome)
    return {
        "name": extra.pop("name", "Piquete A"),
        "margin_attention_m": extra.pop("margin_attention_m", c["margin_attention_m"]),
        "margin_critical_m": extra.pop("margin_critical_m", c["margin_critical_m"]),
        "points": c["points"],
        **extra,
    }


# Cerca folgada: nenhum erro e nenhum aviso, então pode ser salva direto.
LIMPA = corpo("cerca_boa_sem_nenhum_aviso")


# ===========================================================================
# POST /api/fences/validate
# ===========================================================================


def test_validate_responde_o_formato_da_secao_5_4(client):
    resposta = client.post("/api/fences/validate", json=LIMPA)

    assert resposta.status_code == 200
    dados = resposta.json()
    assert set(dados) == {"valid", "violations", "area_ha", "perimeter_m", "orientation"}
    assert dados["valid"] is True
    assert dados["violations"] == []
    assert dados["orientation"] == "clockwise"
    assert dados["area_ha"] > 0


def test_validate_nao_responde_erro_http_para_cerca_invalida(client):
    """O editor chama esta rota a cada mudança, inclusive com a cerca pela
    metade. Nessa hora, cerca inválida é a situação NORMAL — devolver 422
    faria o navegador tratar como falha de rede."""
    resposta = client.post("/api/fences/validate", json=corpo("cerca_de_exemplo_invertida"))

    assert resposta.status_code == 200
    assert resposta.json()["valid"] is False
    assert [v["rule"] for v in resposta.json()["violations"] if v["severity"] == "error"] == ["VAL-06"]


def test_validate_nao_salva_nada(client):
    client.post("/api/fences/validate", json=LIMPA)

    assert client.get("/api/fences").json() == []
    assert client.get("/api/fences/active").status_code == 404


def test_validate_devolve_indices_para_destacar_no_mapa(client):
    resposta = client.post("/api/fences/validate", json=corpo("gravata_borboleta"))

    violacao = next(v for v in resposta.json()["violations"] if v["rule"] == "VAL-05")
    assert violacao["edges"] == [[1, 3]]


# ===========================================================================
# POST /api/fences
# ===========================================================================


def test_cria_cerca_e_devolve_201_com_o_contrato_da_secao_6(client):
    resposta = client.post("/api/fences", json=LIMPA)

    assert resposta.status_code == 201
    cerca = resposta.json()
    assert set(cerca) == {
        "version", "name", "status", "margin_attention_m", "margin_critical_m",
        "points", "area_ha", "perimeter_m", "crc32", "warnings", "created_at",
        "reactivated_from",
    }
    assert cerca["version"] == 1
    assert cerca["status"] == "active"
    assert cerca["name"] == "Piquete A"
    assert len(cerca["crc32"]) == 8
    assert cerca["created_at"].endswith("Z")


def test_a_cerca_de_exemplo_da_especificacao_produz_o_crc_do_contrato(client):
    """O vetor de teste da seção 5.5, atravessando a API inteira.

    Este é o teste que amarra tudo: se o CRC sair DA29321E aqui, a
    conversão para microgradus, a montagem dos bytes e o cálculo estão
    todos corretos no caminho real, não só nos testes de unidade.
    """
    resposta = client.post(
        "/api/fences", json=corpo("cerca_de_exemplo", accept_warnings=True)
    )

    cerca = resposta.json()
    assert cerca["version"] == 1
    assert cerca["crc32"] == "DA29321E"
    assert cerca["area_ha"] == pytest.approx(0.2113, rel=0.01)
    assert cerca["perimeter_m"] == pytest.approx(183.96, rel=0.01)


def test_pontos_devolvidos_sao_os_da_forma_canonica(client):
    """A tela mostra exatamente o que a coleira recebe (seção 6).

    Enviamos coordenadas com 9 casas decimais; a resposta traz os
    valores já arredondados para microgradus.
    """
    dados = dict(LIMPA)
    dados["points"] = [
        {"lat": -31.3058004999, "lon": -54.0646004999},
        {"lat": -31.3058000000, "lon": -54.0633000000},
        {"lat": -31.3066000000, "lon": -54.0633000000},
        {"lat": -31.3066000000, "lon": -54.0646000000},
    ]

    cerca = client.post("/api/fences", json=dados).json()

    assert cerca["points"][0] == {"lat": -31.3058, "lon": -54.0646}


def test_cerca_com_erro_e_recusada_com_422_no_formato_da_secao_6(client):
    resposta = client.post("/api/fences", json=corpo("cerca_de_exemplo_invertida"))

    assert resposta.status_code == 422
    corpo_erro = resposta.json()
    # As quatro chaves no MESMO nível, sem o embrulho do HTTPException.
    assert set(corpo_erro) == {"detail", "rule", "message", "violations"}
    assert corpo_erro["detail"] == "fence_invalid"
    assert corpo_erro["rule"] == "VAL-06"
    assert "anti-horário" in corpo_erro["message"]
    assert len(corpo_erro["violations"]) >= 1


def test_cerca_com_aviso_exige_aceite_e_responde_409(client):
    resposta = client.post("/api/fences", json=corpo("cerca_de_exemplo"))

    assert resposta.status_code == 409
    corpo_erro = resposta.json()
    assert corpo_erro["detail"] == "warnings_not_accepted"
    assert [v["rule"] for v in corpo_erro["violations"]] == ["VAL-12"]
    assert all(v["severity"] == "warning" for v in corpo_erro["violations"])
    # Nada foi salvo.
    assert client.get("/api/fences").json() == []


def test_cerca_com_aviso_aceito_e_salva_com_os_avisos_registrados(client):
    resposta = client.post("/api/fences", json=corpo("cerca_de_exemplo", accept_warnings=True))

    assert resposta.status_code == 201
    assert [v["rule"] for v in resposta.json()["warnings"]] == ["VAL-12"]


def test_cerca_limpa_nao_precisa_de_aceite(client):
    """Sem aviso, `accept_warnings` não faz falta: o produtor não é
    obrigado a marcar uma caixa sobre coisa nenhuma."""
    assert client.post("/api/fences", json=LIMPA).status_code == 201


def test_nome_vazio_e_recusado(client):
    dados = dict(LIMPA, name="")

    assert client.post("/api/fences", json=dados).status_code == 422


def test_campo_desconhecido_no_corpo_e_recusado(client):
    """`extra="forbid"` pega erro de digitação em nome de campo — como
    mandar `long` em vez de `lon`, que é justamente o risco da DEC-14."""
    dados = dict(LIMPA)
    dados["points"] = [{"lat": -31.3058, "long": -54.0646}, *LIMPA["points"][1:]]

    assert client.post("/api/fences", json=dados).status_code == 422


# ===========================================================================
# Versões e cerca ativa
# ===========================================================================


def test_versoes_sempre_crescem(client):
    primeira = client.post("/api/fences", json=LIMPA).json()
    segunda = client.post("/api/fences", json=dict(LIMPA, name="Piquete B")).json()
    terceira = client.post("/api/fences", json=dict(LIMPA, name="Piquete C")).json()

    assert [primeira["version"], segunda["version"], terceira["version"]] == [1, 2, 3]


def test_criar_uma_cerca_desativa_a_anterior(client):
    """Premissa P3: uma cerca ativa por vez."""
    client.post("/api/fences", json=LIMPA)
    client.post("/api/fences", json=dict(LIMPA, name="Piquete B"))

    historico = client.get("/api/fences").json()
    ativas = [f for f in historico if f["status"] == "active"]

    assert len(ativas) == 1
    assert ativas[0]["version"] == 2
    assert client.get("/api/fences/active").json()["version"] == 2


def test_sem_cerca_ativa_responde_404(client):
    resposta = client.get("/api/fences/active")

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "no_active_fence"}


def test_a_rota_active_nao_e_confundida_com_um_numero_de_versao(client):
    """`/api/fences/active` precisa vir declarada antes de
    `/api/fences/{version}`, senão "active" seria lido como número."""
    client.post("/api/fences", json=LIMPA)

    assert client.get("/api/fences/active").status_code == 200


def test_versao_inexistente_responde_404(client):
    resposta = client.get("/api/fences/99")

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "fence_not_found"}


# ===========================================================================
# Histórico
# ===========================================================================


def test_historico_vem_do_mais_recente_para_o_mais_antigo(client):
    client.post("/api/fences", json=dict(LIMPA, name="Primeira"))
    client.post("/api/fences", json=dict(LIMPA, name="Segunda"))

    historico = client.get("/api/fences").json()

    assert [f["version"] for f in historico] == [2, 1]
    assert [f["name"] for f in historico] == ["Segunda", "Primeira"]


def test_historico_traz_resumo_sem_os_pontos(client):
    """A lista é leve de propósito: quem quiser os pontos pede a versão."""
    client.post("/api/fences", json=LIMPA)

    linha = client.get("/api/fences").json()[0]

    assert "points" not in linha
    assert linha["point_count"] == 4


# ===========================================================================
# Reativação
# ===========================================================================


def test_reativar_cria_uma_versao_nova_e_registra_a_origem(client):
    primeira = client.post("/api/fences", json=dict(LIMPA, name="Piquete do açude")).json()
    client.post("/api/fences", json=dict(LIMPA, name="Outra"))

    resposta = client.post(f"/api/fences/{primeira['version']}/reactivate")

    assert resposta.status_code == 201
    nova = resposta.json()
    assert nova["version"] == 3
    assert nova["status"] == "active"
    assert nova["name"] == "Piquete do açude"
    assert nova["reactivated_from"] == 1
    assert nova["points"] == primeira["points"]


def test_reativar_nao_reabre_a_versao_antiga(client):
    """"Maior versão = mais recente" precisa valer sempre: a versão
    antiga continua inativa e o log dela segue verdadeiro."""
    primeira = client.post("/api/fences", json=LIMPA).json()
    client.post("/api/fences", json=dict(LIMPA, name="Outra"))

    client.post(f"/api/fences/{primeira['version']}/reactivate")

    assert client.get("/api/fences/1").json()["status"] == "inactive"


def test_reativar_a_mesma_cerca_produz_o_mesmo_crc_so_se_a_versao_for_igual(client):
    """A versão entra no cálculo do CRC, então uma cópia dos mesmos
    pontos em versão nova tem CRC novo — e a coleira sabe que recebeu
    coisa diferente."""
    primeira = client.post("/api/fences", json=LIMPA).json()

    nova = client.post(f"/api/fences/{primeira['version']}/reactivate").json()

    assert nova["points"] == primeira["points"]
    assert nova["crc32"] != primeira["crc32"]


def test_reativar_versao_inexistente_responde_404(client):
    assert client.post("/api/fences/99/reactivate").status_code == 404


# ===========================================================================
# Entregas
# ===========================================================================


def test_criar_cerca_gera_uma_entrega_pendente_por_coleira(client, settings):
    versao = client.post("/api/fences", json=LIMPA).json()["version"]

    entregas = client.get(f"/api/fences/{versao}/deliveries").json()

    assert [e["collar_id"] for e in entregas] == list(settings.known_collars)
    assert all(e["status"] == "pending" for e in entregas)
    assert all(e["status_label"] == "Salva" for e in entregas)
    assert all(e["attempts"] == 0 for e in entregas)


def test_entregas_de_versao_inexistente_respondem_404(client):
    assert client.get("/api/fences/99/deliveries").status_code == 404


# ===========================================================================
# Arquivo de log
# ===========================================================================


def test_log_da_cerca_e_baixavel_como_texto(client):
    versao = client.post("/api/fences", json=LIMPA).json()["version"]

    resposta = client.get(f"/api/fences/{versao}/log")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/plain")
    assert "attachment" in resposta.headers["content-disposition"]
    assert f"vfence_cerca_{versao}.txt" in resposta.headers["content-disposition"]


def test_log_traz_cabecalho_regras_e_entrega(client):
    """É a evidência do relatório: precisa bastar sozinho."""
    versao = client.post("/api/fences", json=dict(LIMPA, name="Piquete do açude")).json()["version"]

    texto = client.get(f"/api/fences/{versao}/log").text

    assert "Piquete do açude" in texto
    assert "CRC-32" in texto
    assert "lat_e6" in texto and "lon_e6" in texto  # as duas unidades
    assert "validation passed" in texto
    assert "VAL-11 not applicable" in texto
    assert "COL01" in texto
    assert "Salva" in texto


def test_log_registra_os_avisos_aceitos_pelo_produtor(client):
    versao = client.post(
        "/api/fences", json=corpo("cerca_de_exemplo", accept_warnings=True)
    ).json()["version"]

    texto = client.get(f"/api/fences/{versao}/log").text

    assert "warning accepted by user: VAL-12" in texto


# ===========================================================================
# GeoJSON
# ===========================================================================


def test_geojson_segue_a_rfc_7946(client):
    versao = client.post("/api/fences", json=LIMPA).json()["version"]

    resposta = client.get(f"/api/fences/{versao}/geojson")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("application/geo+json")
    dados = resposta.json()
    assert dados["type"] == "Feature"
    assert dados["geometry"]["type"] == "Polygon"


def test_geojson_usa_lon_lat_nessa_ordem(client):
    """A inversão em relação ao Leaflet acontece em um lugar só do
    backend, coberto por este teste. Trocar lat por lon é um dos riscos
    listados no planejamento."""
    cerca = client.post("/api/fences", json=LIMPA).json()
    versao = cerca["version"]

    anel = client.get(f"/api/fences/{versao}/geojson").json()["geometry"]["coordinates"][0]

    primeiro = anel[0]
    assert primeiro[0] == cerca["points"][0]["lon"]  # longitude primeiro
    assert primeiro[1] == cerca["points"][0]["lat"]
    # Longitude em -54, latitude em -31: a ordem não está trocada.
    assert -55 < primeiro[0] < -53
    assert -32 < primeiro[1] < -31


def test_geojson_fecha_o_anel_e_inverte_o_sentido(client):
    """A RFC 7946 exige anel fechado e anel externo anti-horário. Dentro
    do sistema a cerca é horária e p1 não é repetido."""
    cerca = client.post("/api/fences", json=LIMPA).json()

    anel = client.get(f"/api/fences/{cerca['version']}/geojson").json()["geometry"]["coordinates"][0]

    assert len(anel) == len(cerca["points"]) + 1
    assert anel[0] == anel[-1]  # fechado

    # Fórmula do laço em lon/lat: positivo = anti-horário.
    area = sum(
        anel[i][0] * anel[i + 1][1] - anel[i + 1][0] * anel[i][1]
        for i in range(len(anel) - 1)
    )
    assert area > 0


def test_geojson_traz_as_propriedades_da_cerca(client):
    versao = client.post("/api/fences", json=LIMPA).json()["version"]

    propriedades = client.get(f"/api/fences/{versao}/geojson").json()["properties"]

    assert propriedades["version"] == versao
    assert propriedades["name"] == "Piquete A"
    assert len(propriedades["crc32"]) == 8
