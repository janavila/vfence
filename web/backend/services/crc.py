"""CRC-32 da cerca.

O que é e para que serve
------------------------
Um CRC é um número calculado a partir de um conjunto de bytes. Se um
único bit mudar no caminho, o número calculado de novo sai diferente.
No VFence ele resolve um problema concreto: como ter certeza de que a
coleira guardou EXATAMENTE a cerca que o produtor desenhou?

O caminho é longo — Central, Base, serial, rádio LoRa fragmentado,
memória da coleira — e o relatório da IP1 já registrou mensagens
corrompidas em SNR baixo. Então:

1. o Central calcula o CRC-32 sobre a forma canônica da cerca;
2. o CRC viaja junto;
3. a coleira remonta os fragmentos e recalcula;
4. a coleira devolve o valor calculado no FENCE_ACK;
5. o Central só marca "Confirmada" se o valor devolvido for igual.

Se der diferente, a entrega vira `failed` com `detail = "crc_mismatch"`.

Qual CRC-32
-----------
O CRC-32 padrão IEEE 802.3, o mesmo do `zlib.crc32` do Python, do ZIP e
do Ethernet. Foi escolhido porque existe em qualquer linguagem: o
firmware do ESP32 consegue reproduzi-lo sem biblioteca exótica.

Guardamos e transmitimos como 8 dígitos hexadecimais MAIÚSCULOS, para
não haver dúvida de formatação entre as camadas (`da29321e` e
`DA29321E` são o mesmo número, mas comparação de texto diria que não).

Vetor de teste clássico
-----------------------
`CRC-32("123456789") = CBF43926`. Esse valor está em toda tabela de
referência de CRC-32 e serve para o firmware conferir a implementação
dele antes de tentar a cerca inteira.

Onde se conecta
---------------
`canonical.py` monta os bytes; este módulo os transforma no texto que vai
para a coluna `fences.crc32` e para a Base em `GET /api/edge/fences/{v}`.
"""

from __future__ import annotations

import zlib


def crc32_hex(data: bytes) -> str:
    """Calcula o CRC-32 e devolve 8 dígitos hexadecimais maiúsculos.

    >>> crc32_hex(b"123456789")
    'CBF43926'
    """
    return f"{zlib.crc32(data):08X}"
