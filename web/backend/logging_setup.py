"""Formato de log do Central.

O que faz
---------
Configura o log no formato combinado na seção 10 da especificação, que é
o mesmo já usado pelo VFence Monitor:

    2026-10-06 13:02:05 [INFO] fences: fence=1 created (4 points, ...)
    └── data e hora ──┘ nível   módulo  mensagem

Esse formato não é enfeite: é o que permite buscar `fence=1` nos logs do
Central e da Base e reconstruir o caminho inteiro de uma cerca (seção
3.7 do planejamento). O arquivo gerado aqui é uma das evidências do
relatório da IP2.

Uma diferença proposital em relação ao Monitor
----------------------------------------------
O Monitor usa `logging.basicConfig` sem tocar no fuso. O padrão do
módulo `logging` do Python é `time.localtime`, então os logs dele saem
em hora de Brasília. A especificação manda UTC. Por isso trocamos o
conversor do formatador por `time.gmtime`.

Isso foi registrado na fase F0 como divergência intencional: quando o
grupo comparar o log do Central com o log da Base na bancada, os dois
podem estar com 3 horas de diferença até que a Base faça o mesmo ajuste.

Onde se conecta
---------------
`main.py` chama `setup_logging()` antes de qualquer outra coisa, usando
o `LOG_LEVEL` e o caminho de `data/logs/central.log` vindos do Settings.
Depois disso, qualquer módulo só precisa de
`logger = logging.getLogger("fences")` para escrever no formato certo.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Loggers que o Uvicorn cria com formato próprio. Aplicamos o nosso
# formatador neles também, para o arquivo de log ficar homogêneo em vez
# de misturar dois estilos de linha.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def build_formatter() -> logging.Formatter:
    """Cria o formatador padrão do projeto, com data e hora em UTC."""
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    formatter.converter = time.gmtime
    return formatter


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Liga o log no console e, se houver caminho, também em arquivo.

    A função é idempotente: limpa os handlers do logger raiz antes de
    instalar os seus. Sem isso, chamá-la duas vezes (o que acontece nos
    testes, que criam vários apps) duplicaria cada linha de log.

    `log_file` com valor `None` desliga o arquivo — é o que os testes
    usam, para não encher o disco de log de teste.
    """
    formatter = build_formatter()

    handlers: list[logging.Handler] = []

    # Console: stdout, para o systemd e o journalctl capturarem.
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers.append(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(_as_level(level))

    for name in UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)


def _as_level(level: str) -> int:
    """Converte "INFO", "debug" etc. para a constante do módulo logging.

    Valor desconhecido cai em INFO em vez de derrubar o servidor: um erro
    de digitação no LOG_LEVEL não é motivo para o produtor ficar sem o
    sistema.
    """
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO
