"""Forma canônica da cerca: de float para inteiro, uma única vez.

O problema que isto resolve
---------------------------
Número com casa decimal (`float`) não é exato em computador. A conta
`0.1 + 0.2` não dá exatamente `0.3` em nenhuma linguagem. Pior: o ESP32
tem `float` de 32 bits, em que a coordenada `-54.063935` só é
representável com erro de 0,2 a 0,4 m.

Se cada camada calculasse o CRC a partir do próprio `float`, o CRC do
Central, o da Base e o da coleira sairiam diferentes — e nenhuma cerca
jamais seria confirmada, sem ninguém entender o motivo.

A solução (seção 5.6 do planejamento)
-------------------------------------
A conversão de `float` para inteiro acontece **uma vez só, aqui**. Dali
em diante, Base, gateway e coleira trabalham com esses inteiros:

- coordenadas → **microgradus**: grau × 10⁶, guardado em `int32`;
- margens → **centímetros**: metro × 100, guardado em `uint16`.

A resolução de um microgradus é cerca de 0,11 m — 18 vezes mais fina que
a margem crítica padrão de 2 m, portanto precisão sobrando.

O detalhe do arredondamento
---------------------------
`round()` do Python arredonda "meio para o par": `round(0.5)` dá 0 e
`round(1.5)` dá 2. Isso é ótimo para estatística e **péssimo** para um
contrato entre linguagens, porque C e JavaScript não fazem isso. A
especificação manda "meio para longe do zero", que é o comportamento
intuitivo e o que `Math.round(Math.abs(x)) * sinal` faz no JavaScript.

Por isso usamos `Decimal` com `ROUND_HALF_UP`, que no módulo `decimal`
significa exatamente "empate vai para longe do zero". E convertemos o
`float` passando por `str()` primeiro: `Decimal(0.1)` captura o erro
binário do float, `Decimal(str(0.1))` captura o que a pessoa escreveu.

Ordem dos bytes
---------------
Little-endian (`<` no `struct`), que é a ordem nativa do ESP32 e do x86.
Escolher a ordem explicitamente evita que o código funcione no notebook
e falhe no microcontrolador.

    version   uint32   4 bytes
    dA_cm     uint16   2 bytes
    dC_cm     uint16   2 bytes
    n         uint8    1 byte
    por ponto: lat_e6 int32, lon_e6 int32   8 bytes × n

Uma cerca de 4 pontos dá 9 + 32 = 41 bytes.

Onde se conecta
---------------
`fence_service.py` chama `canonical_bytes()` ao criar a cerca e guarda o
CRC resultante. `routers/edge.py` devolve `points_e6` à Base. O editor
no navegador nunca faz essa conversão: ele manda graus e recebe de volta
os graus RECONSTRUÍDOS a partir dos microgradus, para o que aparece na
tela ser exatamente o que a coleira recebe.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

# Limites dos tipos usados nos bytes canônicos. Conferir contra eles dá
# uma mensagem clara em vez de um erro cru do struct.
MAX_UINT16 = 65535
MAX_UINT32 = 4294967295
MAX_POINTS = 32
E6_LIMIT = 2147483647  # int32: suporta ±2147,48 graus, muito além de ±180


class PointE6(NamedTuple):
    """Um ponto da cerca em microgradus inteiros."""

    lat_e6: int
    lon_e6: int


def to_e6(degrees: float) -> int:
    """Converte graus para microgradus (grau × 10⁶), arredondando longe do zero.

    >>> to_e6(-31.306000)
    -31306000
    >>> to_e6(0.0000005)   # empate: vai para longe do zero
    1
    >>> to_e6(-0.0000005)
    -1
    """
    value = int(
        Decimal(str(degrees)).scaleb(6).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    if not -E6_LIMIT <= value <= E6_LIMIT:
        raise ValueError(f"coordenada fora do alcance de int32: {degrees}")
    return value


def from_e6(value: int) -> float:
    """Volta de microgradus para graus.

    Usada para devolver ao navegador os pontos JÁ na forma canônica, de
    modo que a tela mostre exatamente o que a coleira vai receber.

    >>> from_e6(-31306000)
    -31.306
    """
    return value / 1_000_000


def to_cm(meters: float) -> int:
    """Converte metros para centímetros inteiros, arredondando longe do zero.

    >>> to_cm(5.0)
    500
    >>> to_cm(2.005)
    201
    """
    value = int(
        Decimal(str(meters)).scaleb(2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    if not 0 <= value <= MAX_UINT16:
        raise ValueError(f"margem fora do alcance de uint16: {meters} m")
    return value


def from_cm(value: int) -> float:
    """Volta de centímetros para metros.

    >>> from_cm(500)
    5.0
    """
    return value / 100


def points_to_e6(points: Sequence[tuple[float, float]]) -> list[PointE6]:
    """Converte uma lista de pares (lat, lon) em graus para microgradus."""
    return [PointE6(to_e6(lat), to_e6(lon)) for lat, lon in points]


def points_from_e6(points: Sequence[PointE6 | tuple[int, int]]) -> list[tuple[float, float]]:
    """Converte microgradus de volta para pares (lat, lon) em graus."""
    return [(from_e6(lat), from_e6(lon)) for lat, lon in points]


def canonical_bytes(
    version: int,
    margin_attention_cm: int,
    margin_critical_cm: int,
    points_e6: Sequence[PointE6 | tuple[int, int]],
) -> bytes:
    """Monta os bytes canônicos da cerca, na ordem do contrato (seção 5.5).

    É sobre ESTES bytes, e só eles, que o CRC-32 é calculado. Qualquer
    mudança de ordem ou de tipo aqui muda o CRC de todas as cercas e
    quebra a compatibilidade com o firmware.

    >>> pontos = [(-31306000, -54064200), (-31306000, -54063700),
    ...           (-31306400, -54063700), (-31306400, -54064200)]
    >>> canonical_bytes(1, 500, 200, pontos).hex()[:18]
    '01000000f401c80004'
    """
    quantity = len(points_e6)
    if not 3 <= quantity <= MAX_POINTS:
        raise ValueError(f"a cerca precisa ter de 3 a {MAX_POINTS} pontos (tem {quantity})")
    if not 0 <= version <= MAX_UINT32:
        raise ValueError(f"versão fora do alcance de uint32: {version}")
    for name, value in (
        ("margin_attention_cm", margin_attention_cm),
        ("margin_critical_cm", margin_critical_cm),
    ):
        if not 0 <= value <= MAX_UINT16:
            raise ValueError(f"{name} fora do alcance de uint16: {value}")

    blob = struct.pack(
        "<IHHB", version, margin_attention_cm, margin_critical_cm, quantity
    )
    for lat_e6, lon_e6 in points_e6:
        blob += struct.pack("<ii", lat_e6, lon_e6)
    return blob
