"""Hora oficial do Central.

Por que este módulo existe
--------------------------
A especificação (seção 7) manda gravar toda data e hora em UTC, no
formato ISO 8601 terminando em "Z":

    2026-10-06T13:02:05Z

Isso parece detalhe, mas é a origem de um erro silencioso muito comum:
`datetime.now(timezone.utc).isoformat()` produz `...+00:00`, e não
`...Z`. Os dois textos representam o MESMO instante, mas são textos
diferentes — e o Central compara e ordena esses campos como texto no
SQLite. Misturar os dois formatos quebraria ordenação e comparação.

Concentrar a conversão em uma função só garante que o Central inteiro
(banco, API, logs e arquivo de log da cerca) escreva sempre igual.

Onde se conecta
---------------
Usado pelo `db.py` (todo `created_at`, `updated_at`, `last_seen`), pelas
rotas (`/api/health`) e, nas fases seguintes, pelos serviços de entrega
e de log da cerca.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Formato de gravação: sem microssegundos (não precisamos dessa precisão)
# e com o "Z" literal indicando UTC.
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    """Devolve o instante atual já com o fuso UTC explícito.

    Nunca usamos `datetime.now()` sem fuso no projeto: um datetime "ingênuo"
    (sem tzinfo) é ambíguo e o Raspberry Pi, que não tem relógio de bateria,
    é justamente onde essa ambiguidade causa estrago.
    """
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Converte um datetime para o texto de gravação do Central.

    Se o datetime vier sem fuso, assumimos que já está em UTC em vez de
    deixar o Python supor o fuso local da máquina — suposição que mudaria
    o resultado conforme quem roda o servidor.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def utc_now_iso() -> str:
    """Atalho para o caso mais comum: a hora de agora, pronta para gravar."""
    return to_iso(utc_now())
