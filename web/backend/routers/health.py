"""GET /api/health — o serviço está no ar?

O que faz
---------
Responde o contrato da seção 6 da especificação:

    {"status": "ok", "version": "0.1.0", "time": "2026-10-06T13:02:05Z"}

Para que serve
--------------
Três usos concretos:

1. O frontend consulta esta rota para acender ou apagar o aviso
   "Sem conexão com o servidor" (seção 11.1).
2. O responsável pela Base usa ela para confirmar que achou o Central na
   rede antes de configurar o `CENTRAL_URL`.
3. É o critério de aceite da fase F1 (item CT-01 do backlog).

Por que a rota é tão simples
----------------------------
De propósito: ela responde "o processo está vivo e atendendo". Se
consultássemos o banco aqui, uma lentidão do disco faria o navegador
mostrar "sem conexão" sem o servidor ter caído — e o produtor perderia
confiança no aviso. O estado da Base e das coleiras tem rotas próprias
(`/api/bases`, `/api/collars`).
"""

from __future__ import annotations

from fastapi import APIRouter

from backend import __version__
from backend.clock import utc_now_iso

# prefix="/api" faz a rota final ser /api/health.
# tags aparece como título do grupo na documentação automática em /docs.
router = APIRouter(prefix="/api", tags=["saude"])


@router.get("/health", summary="Situação do serviço")
async def health() -> dict:
    """Devolve situação, versão do Central e a hora do servidor em UTC.

    A hora serve também para a Base: a seção 7.3 do planejamento lembra
    que o Raspberry Pi 3 não tem relógio de bateria e precisa de uma
    referência externa.
    """
    return {"status": "ok", "version": __version__, "time": utc_now_iso()}
