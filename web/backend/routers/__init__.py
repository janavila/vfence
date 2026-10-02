"""Rotas da API do Central, uma por assunto.

Cada arquivo aqui é um `APIRouter` do FastAPI — um grupo de rotas que o
`main.py` encaixa na aplicação. Separar por assunto evita um `main.py`
de mil linhas e deixa claro, pelo nome do arquivo, onde mexer.

Nesta fase existe só `health.py`. As demais entram nas fases previstas
na seção 12 da especificação: `fences` (F3), `edge` (F4), `collars`,
`events`, `bases` e `ws` (F6) e `auth` (F8).
"""
