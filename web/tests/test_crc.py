"""Testes do CRC-32 (fase F2).

O vetor `CRC-32("123456789") = CBF43926` é o mais importante do projeto
inteiro: é com ele que o firmware da coleira confere a implementação
dele antes de tentar a cerca completa. Se este teste falhar, nada no
VFence funciona.
"""

from __future__ import annotations

import pytest

from backend.services.crc import crc32_hex


def test_vetor_de_teste_classico():
    """CRC-32 de "123456789" é CBF43926 em qualquer implementação correta."""
    assert crc32_hex(b"123456789") == "CBF43926"


def test_resultado_sempre_em_oito_digitos_maiusculos():
    """Formato fixo: o firmware compara TEXTO, não número.

    "da29321e" e "DA29321E" são o mesmo número, mas comparação de texto
    diria que não. E um CRC que começa com zero precisa dos 8 dígitos.
    """
    for dados in (b"", b"a", b"123456789", bytes(range(256))):
        resultado = crc32_hex(dados)
        assert len(resultado) == 8
        assert resultado == resultado.upper()
        assert all(digito in "0123456789ABCDEF" for digito in resultado)


def test_crc_de_entrada_vazia():
    """Por definição, o CRC-32 de nenhum byte é zero."""
    assert crc32_hex(b"") == "00000000"


def test_um_bit_diferente_muda_o_crc():
    """É para isto que o CRC serve: detectar alteração nos dados.

    Simula o que a IP1 registrou em SNR baixo — um bit trocado no rádio.
    """
    original = bytes([0b00000000, 0b11110000])
    alterado = bytes([0b00000001, 0b11110000])

    assert crc32_hex(original) != crc32_hex(alterado)


@pytest.mark.parametrize(
    "dados,esperado",
    [
        (b"", "00000000"),
        (b"123456789", "CBF43926"),
        (b"The quick brown fox jumps over the lazy dog", "414FA339"),
    ],
)
def test_vetores_conhecidos(dados, esperado):
    """Valores publicados em tabelas de referência de CRC-32 (IEEE 802.3)."""
    assert crc32_hex(dados) == esperado
