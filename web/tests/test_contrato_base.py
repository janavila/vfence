"""O `docs/CONTRATO_BASE.md` continua verdadeiro? (fase F7)

Por que este arquivo existe
---------------------------
Um documento de contrato que envelhece é pior que nenhum: o responsável
pela Base implementaria contra uma descrição que não corresponde mais ao
código, e a divergência só apareceria na bancada, no dia do ensaio.

Estes testes amarram o documento à implementação. Eles não conferem a
redação — conferem os VALORES que a Base vai usar: os vetores de teste do
CRC, os nomes dos estados, os códigos de erro, o comportamento prometido.
Se alguém mudar o código sem atualizar o documento, a suíte falha aqui.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.services import delivery
from backend.services.canonical import canonical_bytes, to_cm, to_e6
from backend.services.crc import crc32_hex
from backend.services.edge_service import VALID_ZONES

RAIZ = Path(__file__).resolve().parent.parent
CONTRATO = (RAIZ / "docs" / "CONTRATO_BASE.md").read_text(encoding="utf-8")


def test_o_documento_existe_e_nao_esta_vazio():
    assert len(CONTRATO) > 5000


# ---------------------------------------------------------------------------
# Os vetores de teste do CRC (seção 6.3 do documento)
# ---------------------------------------------------------------------------


def test_o_vetor_do_crc_no_documento_e_o_mesmo_do_codigo():
    assert 'CRC-32("123456789") = CBF43926' in CONTRATO
    assert crc32_hex(b"123456789") == "CBF43926"


def test_os_bytes_da_cerca_de_exemplo_no_documento_sao_os_que_o_codigo_gera():
    """Extrai o hex do documento e recalcula com o código.

    É a conferência mais importante daqui: se o firmware seguir o
    documento e o Central gerar outros bytes, nenhuma cerca é confirmada.
    """
    encontrado = re.search(r"^(01000000f401c80004[0-9a-f]+)$", CONTRATO, re.M)
    assert encontrado, "os bytes canônicos não foram encontrados no documento"

    pontos = [
        (-31.306000, -54.064200), (-31.306000, -54.063700),
        (-31.306400, -54.063700), (-31.306400, -54.064200),
    ]
    do_codigo = canonical_bytes(
        1, to_cm(5.0), to_cm(2.0), [(to_e6(lat), to_e6(lon)) for lat, lon in pontos]
    )

    assert encontrado.group(1) == do_codigo.hex()
    assert len(do_codigo) == 41
    assert "**41 bytes**" in CONTRATO


def test_o_crc_da_cerca_de_exemplo_no_documento_confere():
    pontos = [
        (-31306000, -54064200), (-31306000, -54063700),
        (-31306400, -54063700), (-31306400, -54064200),
    ]

    assert crc32_hex(canonical_bytes(1, 500, 200, pontos)) == "DA29321E"
    assert "CRC-32 = DA29321E" in CONTRATO


def test_o_arredondamento_descrito_no_documento_e_o_do_codigo():
    assert "0,0000005  →  1" in CONTRATO
    assert "-0,0000005 → -1" in CONTRATO
    assert to_e6(0.0000005) == 1
    assert to_e6(-0.0000005) == -1


# ---------------------------------------------------------------------------
# Rotas, estados e códigos de erro
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rota",
    ["/api/edge/heartbeat", "/api/edge/fences/{version}", "/api/edge/batch", "/api/health"],
)
def test_as_rotas_citadas_no_documento_existem_de_verdade(rota, client):
    """Confere que a rota está no documento E no esquema OpenAPI."""
    assert rota in CONTRATO
    assert rota in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("status", ["pending", "at_base", "transmitting", "confirmed", "failed"])
def test_os_cinco_estados_de_entrega_estao_no_documento(status):
    assert f"`{status}`" in CONTRATO
    assert status in delivery.STATUS_ORDER


@pytest.mark.parametrize("texto", sorted(delivery.STATUS_LABELS.values()))
def test_os_textos_de_tela_do_documento_sao_os_do_codigo(texto):
    """A tabela da seção 8 mostra o texto que o produtor vê. Mudar o
    texto no código sem mudar o documento confundiria quem lê os dois."""
    assert texto in CONTRATO


def test_o_documento_diz_quais_estados_a_base_pode_relatar():
    assert delivery.BASE_REPORTABLE == {"transmitting", "confirmed", "failed"}
    assert "A Base relata **apenas** `transmitting`, `confirmed` e `failed`" in CONTRATO


@pytest.mark.parametrize("zona", sorted(VALID_ZONES))
def test_as_cinco_zonas_estao_no_documento(zona):
    assert zona in CONTRATO


@pytest.mark.parametrize(
    "codigo",
    ["missing_token", "invalid_token", "base_id_mismatch", "fence_not_found",
     "crc_mismatch", "crc_missing", "invalid_zone"],
)
def test_os_codigos_de_erro_do_documento_aparecem_no_codigo(codigo):
    """Cada código citado no documento tem de existir no backend.

    Protege contra alguém renomear um código e o responsável pela Base
    continuar tratando o nome antigo.
    """
    assert codigo in CONTRATO

    fontes = "\n".join(
        arquivo.read_text(encoding="utf-8")
        for arquivo in sorted((RAIZ / "backend").rglob("*.py"))
    )
    assert codigo in fontes


# ---------------------------------------------------------------------------
# Comportamentos que o documento promete
# ---------------------------------------------------------------------------


def test_o_exemplo_de_acked_up_to_do_documento_funciona(client, base_headers):
    """O documento diz: itens 1531, 1532, 1533 e 1540 → resposta 1533."""
    assert "**1533**, não 1540" in CONTRATO

    itens = [
        {"edge_seq": seq, "type": "telemetry", "data": {"collar_id": "COL01", "zone": "SEGURO"}}
        for seq in (1531, 1532, 1533, 1540)
    ]
    resposta = client.post(
        "/api/edge/batch", json={"base_id": "BASE01", "items": itens}, headers=base_headers
    )

    assert resposta.json() == {"acked_up_to": 1533}


def test_o_documento_promete_401_e_403_e_o_codigo_cumpre(client_anonimo, base_headers):
    assert '`401` · `{"detail": "missing_token"}`' in CONTRATO
    assert '`403` · `{"detail": "base_id_mismatch"}`' in CONTRATO

    sem_token = client_anonimo.post("/api/edge/heartbeat", json={"base_id": "BASE01"})
    assert sem_token.status_code == 401
    assert sem_token.json() == {"detail": "missing_token"}

    outra_base = client_anonimo.post(
        "/api/edge/heartbeat", json={"base_id": "BASE99"}, headers=base_headers
    )
    assert outra_base.status_code == 403
    assert outra_base.json() == {"detail": "base_id_mismatch"}


def test_o_documento_promete_que_a_base_nao_precisa_de_login(client_anonimo, base_headers):
    assert "a Base não tem navegador e não faz" in CONTRATO

    assert client_anonimo.post(
        "/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers
    ).status_code == 200


def test_o_documento_promete_que_zona_desconhecida_nao_derruba_o_lote(client, base_headers):
    assert "Zona desconhecida** não derruba o lote" in CONTRATO

    resposta = client.post(
        "/api/edge/batch",
        json={"base_id": "BASE01", "items": [
            {"edge_seq": 1, "type": "telemetry", "data": {"collar_id": "COL01", "zone": "ROXO"}},
        ]},
        headers=base_headers,
    )

    assert resposta.json() == {"acked_up_to": 1}


def test_o_documento_promete_idempotencia_e_o_codigo_cumpre(client, base_headers):
    assert "Item já visto é **ignorado sem erro**" in CONTRATO

    lote = {"base_id": "BASE01", "items": [
        {"edge_seq": 1, "type": "telemetry", "data": {"collar_id": "COL01", "zone": "SEGURO"}},
    ]}
    for _ in range(3):
        assert client.post("/api/edge/batch", json=lote, headers=base_headers).json() == {
            "acked_up_to": 1
        }

    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"] == 1


def test_o_documento_promete_a_entrega_canonica_e_o_codigo_cumpre(client, base_headers, cerca_limpa):
    """O documento afirma que não há grau com casa decimal na rota da Base."""
    assert "**não há grau com casa decimal aqui**" in CONTRATO
    client.post("/api/fences", json=cerca_limpa)

    cerca = client.get("/api/edge/fences/1", headers=base_headers).json()

    assert set(cerca) == {
        "version", "margin_attention_cm", "margin_critical_cm", "points_e6", "crc32"
    }
    assert all(isinstance(v, int) for par in cerca["points_e6"] for v in par)
    assert isinstance(cerca["margin_attention_cm"], int)


def test_o_documento_registra_o_conflito_e6_x_e7_em_aberto():
    """A decisão mais importante em aberto não pode ficar só na cabeça de
    quem escreveu o código."""
    assert "Microgradus (E6) × E7" in CONTRATO
    assert "o CRC nunca bate" in CONTRATO
