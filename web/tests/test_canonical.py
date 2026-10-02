"""Testes da forma canônica (fase F2).

O que estes testes protegem: a forma canônica é o CONTRATO entre o
Central, a Base e o firmware. Qualquer mudança aqui muda o CRC de todas
as cercas e quebra a compatibilidade. Estes testes são a trava.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.canonical import (
    MAX_POINTS,
    canonical_bytes,
    from_cm,
    from_e6,
    points_from_e6,
    points_to_e6,
    to_cm,
    to_e6,
)
from backend.services.crc import crc32_hex

CASOS = json.loads(
    (Path(__file__).parent / "casos_geometricos.json").read_text(encoding="utf-8")
)
CASO_EXEMPLO = next(c for c in CASOS["casos"] if c["nome"] == "cerca_de_exemplo")


# ---------------------------------------------------------------------------
# Arredondamento
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "graus,esperado",
    [
        (-31.306000, -31306000),
        (-54.064200, -54064200),
        (0.0, 0),
        (90.0, 90000000),
        (-180.0, -180000000),
    ],
)
def test_converte_graus_para_microgradus(graus, esperado):
    assert to_e6(graus) == esperado


def test_empate_arredonda_para_longe_do_zero():
    """A regra da especificação, e por que `round()` do Python não serve.

    `round()` arredonda "meio para o par": `round(0.5)` dá 0. Isso é bom
    para estatística e péssimo para um contrato entre linguagens, porque
    C e JavaScript não fazem isso. A especificação manda "meio para longe
    do zero", que é o comportamento intuitivo.
    """
    assert to_e6(0.0000005) == 1
    assert to_e6(-0.0000005) == -1
    assert to_e6(0.0000015) == 2
    assert to_e6(-0.0000015) == -2

    # Prova de que o round() embutido daria outro resultado:
    assert round(0.0000005 * 10**6) == 0


def test_converte_metros_para_centimetros():
    assert to_cm(5.0) == 500
    assert to_cm(2.0) == 200
    assert to_cm(0.005) == 1  # empate: longe do zero
    assert to_cm(3.456) == 346


def test_volta_de_inteiro_para_grau_e_metro():
    assert from_e6(-31306000) == pytest.approx(-31.306)
    assert from_cm(500) == 5.0


def test_ida_e_volta_nao_perde_o_que_importa():
    """Ida e volta reproduz a coordenada até a 6ª casa decimal.

    É isso que permite ao `FenceOut` devolver os pontos JÁ na forma
    canônica: o produtor vê na tela exatamente o que a coleira recebe.
    """
    for lat, lon in [(-31.306119, -54.063935), (-31.3061, -54.0639), (0.000001, -0.000001)]:
        assert from_e6(to_e6(lat)) == pytest.approx(lat, abs=5e-7)
        assert from_e6(to_e6(lon)) == pytest.approx(lon, abs=5e-7)


def test_converte_lista_de_pontos_nos_dois_sentidos():
    graus = [(-31.306000, -54.064200), (-31.306400, -54.063700)]
    inteiros = points_to_e6(graus)

    assert inteiros == [(-31306000, -54064200), (-31306400, -54063700)]
    assert points_from_e6(inteiros) == pytest.approx(graus)


def test_recusa_coordenada_fora_do_alcance_de_int32():
    """O contrato guarda a coordenada em int32; acima disso não cabe."""
    with pytest.raises(ValueError, match="int32"):
        to_e6(3000.0)


def test_recusa_margem_fora_do_alcance_de_uint16():
    """Margem vai em uint16 centímetros: no máximo 655,35 m."""
    with pytest.raises(ValueError, match="uint16"):
        to_cm(700.0)
    with pytest.raises(ValueError, match="uint16"):
        to_cm(-1.0)


# ---------------------------------------------------------------------------
# Os bytes canônicos e o CRC
# ---------------------------------------------------------------------------


def test_bytes_canonicos_da_cerca_de_exemplo():
    """O vetor de teste da seção 5.5, byte por byte.

    41 bytes = 4 (version) + 2 (dA_cm) + 2 (dC_cm) + 1 (n) + 8 × 4 pontos.
    """
    esperado = CASO_EXEMPLO["expected"]
    blob = canonical_bytes(
        esperado["version"],
        to_cm(CASO_EXEMPLO["margin_attention_m"]),
        to_cm(CASO_EXEMPLO["margin_critical_m"]),
        [tuple(par) for par in esperado["points_e6"]],
    )

    assert len(blob) == 41
    assert blob.hex() == esperado["canonical_hex"]


def test_crc_da_cerca_de_exemplo():
    """O outro vetor obrigatório: CRC DA29321E.

    Este par (bytes + CRC) é o que o firmware precisa reproduzir. Se o
    firmware chegar a um valor diferente, a diferença está nos bytes, e
    o teste acima aponta onde.
    """
    esperado = CASO_EXEMPLO["expected"]
    blob = canonical_bytes(
        esperado["version"],
        to_cm(CASO_EXEMPLO["margin_attention_m"]),
        to_cm(CASO_EXEMPLO["margin_critical_m"]),
        [tuple(par) for par in esperado["points_e6"]],
    )

    assert crc32_hex(blob) == esperado["crc32"] == "DA29321E"


def test_ordem_dos_bytes_e_little_endian():
    """Confere a ordem dos bytes campo por campo.

    Little-endian é a ordem nativa do ESP32 e do x86. Escolhê-la de
    forma explícita evita código que funciona no notebook e falha no
    microcontrolador.
    """
    pontos = [(1, 2), (3, 4), (5, 6)]
    blob = canonical_bytes(1, 500, 200, pontos)

    assert blob[0:4] == b"\x01\x00\x00\x00"  # version = 1, uint32
    assert blob[4:6] == b"\xf4\x01"          # 500 cm = 0x01F4, uint16
    assert blob[6:8] == b"\xc8\x00"          # 200 cm = 0x00C8, uint16
    assert blob[8:9] == b"\x03"              # n = 3, uint8
    assert blob[9:13] == b"\x01\x00\x00\x00"  # lat_e6 do ponto 1, int32
    assert len(blob) == 9 + 8 * 3


def test_tamanho_cresce_oito_bytes_por_ponto():
    """Serve para o firmware dimensionar os fragmentos do rádio.

    Com 32 pontos dá 265 bytes — acima do limite de 255 bytes por pacote
    do SX1276. É daí que vem a necessidade de fragmentar (seção 8.4).
    """
    base = [(0, 0), (1, 1), (2, 2)]
    assert len(canonical_bytes(1, 500, 200, base)) == 9 + 24

    cheia = [(i, i) for i in range(MAX_POINTS)]
    assert len(canonical_bytes(1, 500, 200, cheia)) == 9 + 8 * 32 == 265


def test_versoes_diferentes_dao_crc_diferente():
    """A versão entra no CRC, então reenviar a mesma cerca como versão
    nova produz um CRC novo — e a coleira sabe que recebeu coisa nova."""
    pontos = [(-31306000, -54064200), (-31306000, -54063700), (-31306400, -54063700)]

    primeira = crc32_hex(canonical_bytes(1, 500, 200, pontos))
    segunda = crc32_hex(canonical_bytes(2, 500, 200, pontos))

    assert primeira != segunda


def test_ordem_dos_pontos_muda_o_crc():
    """Por que o sentido horário é obrigatório (DEC-15).

    A mesma cerca desenhada em ordem diferente dá CRC diferente. Com um
    sentido único e obrigatório, cada cerca tem um CRC só — sem
    ambiguidade entre as camadas.
    """
    pontos = [(-31306000, -54064200), (-31306000, -54063700), (-31306400, -54063700)]
    invertidos = list(reversed(pontos))

    assert crc32_hex(canonical_bytes(1, 500, 200, pontos)) != crc32_hex(
        canonical_bytes(1, 500, 200, invertidos)
    )


def test_recusa_quantidade_de_pontos_invalida():
    with pytest.raises(ValueError, match="3 a 32"):
        canonical_bytes(1, 500, 200, [(0, 0), (1, 1)])
    with pytest.raises(ValueError, match="3 a 32"):
        canonical_bytes(1, 500, 200, [(i, i) for i in range(33)])
