"""Erros da API no formato exato da especificação.

O problema
----------
A seção 6 define este corpo para uma cerca recusada:

    HTTP 422
    {"detail": "fence_invalid", "rule": "VAL-05", "message": "...",
     "violations": [ ... ]}

Quatro chaves no mesmo nível. O `HTTPException` do FastAPI não produz
isso: ele sempre embrulha o que recebe dentro de `detail`. Passar um
dicionário geraria `{"detail": {"detail": "fence_invalid", ...}}`, com o
conteúdo um nível abaixo do contrato.

A solução
---------
Uma exceção própria que carrega o corpo pronto, e um tratador registrado
no `main.py` que o devolve como está. Assim o contrato fica escrito em um
lugar só, e qualquer rota pode levantá-lo.

Para erros simples de uma chave (`{"detail": "fence_not_found"}`) o
`HTTPException` comum já serve e é o que o Monitor usa
(`app/main.py:82`) — não vale criar coisa nova para isso.

Onde se conecta
---------------
`routers/fences.py` levanta `FenceInvalid` e `WarningsNotAccepted`;
`main.py` registra o tratador; os testes conferem o corpo chave por chave.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Erro que define o corpo completo da resposta HTTP.

    `payload` vai para o JSON exatamente como está, sem embrulho.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("detail", "api_error"))
        self.status_code = status_code
        self.payload = payload


class FenceInvalid(ApiError):
    """HTTP 422 — a cerca tem pelo menos um erro de validação.

    `rule` e `message` repetem a PRIMEIRA violação de severidade `error`.
    Isso é redundante com a lista `violations`, e de propósito: mantém a
    compatibilidade com o formato de erro mais simples do plano, para
    quem só quer saber "qual é o problema" sem percorrer a lista.
    """

    def __init__(self, violations: list[dict]) -> None:
        errors = [v for v in violations if v.get("severity") == "error"]
        first = errors[0] if errors else {}
        super().__init__(
            status_code=422,
            payload={
                "detail": "fence_invalid",
                "rule": first.get("rule"),
                "message": first.get("message"),
                "violations": violations,
            },
        )


class WarningsNotAccepted(ApiError):
    """HTTP 409 — a cerca é salvável, mas tem avisos que ninguém assumiu.

    Por que 409 (conflito) e não 422: a cerca não está errada. O estado
    da requisição é que está em conflito com a regra de negócio — falta
    o produtor marcar "Estou ciente dos avisos" na revisão. Repetir a
    mesma requisição com `accept_warnings: true` resolve.
    """

    def __init__(self, violations: list[dict]) -> None:
        super().__init__(
            status_code=409,
            payload={
                "detail": "warnings_not_accepted",
                "violations": [v for v in violations if v.get("severity") == "warning"],
            },
        )
