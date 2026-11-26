"""Login do produtor: `/api/auth/*` (fase F8).

Usuário único (DEC-11)
----------------------
Não há cadastro: o usuário e a senha vêm de `ADMIN_USER` e
`ADMIN_PASSWORD` no `.env`. Para uma propriedade com um produtor, isso é
suficiente e evita construir e proteger um cadastro inteiro — telas de
criar conta, trocar senha, recuperar senha — que ninguém pediu.

A senha NÃO é guardada em lugar nenhum: a cada tentativa, comparamos o
que foi digitado com o `ADMIN_PASSWORD` do `.env` usando o scrypt de
`security.py`. É por isso que não existe tabela de usuários no banco.

A sessão
--------
Cookie `HttpOnly`, assinado com `SESSION_SECRET`. `HttpOnly` significa
que o JavaScript da página não consegue ler o cookie, o que limita o
estrago de uma eventual injeção de script.

`SameSite=Lax` impede que outro site faça o navegador enviar o cookie
numa requisição que o produtor não pediu.

O cookie NÃO é marcado como `Secure` porque o MVP roda em HTTP na rede
local (premissa P2) — com `Secure`, o navegador simplesmente não o
enviaria e o login nunca funcionaria. Quando o Central for para a
internet, HTTPS passa a ser obrigatório (seção 10 do planejamento) e
esta linha precisa mudar junto.

Onde se conecta
---------------
`main.py` registra este router e o middleware que confere a sessão nas
rotas protegidas. `frontend/login.html` é a tela.
"""

from __future__ import annotations

import asyncio
import logging
import os
from time import monotonic

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.permissions import session_user
from backend.security import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger("auth")

router = APIRouter(prefix="/api/auth", tags=["login"])

# Oito horas: um dia de trabalho. Curto o bastante para um computador
# esquecido aberto não ficar logado por semanas, longo o bastante para o
# produtor não ser desconectado no meio de uma tarefa.
SESSION_LIFETIME_S = 8 * 60 * 60


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    user: str | None
    authenticated: bool


@router.post("/login", response_model=SessionOut, summary="Entrar no sistema")
async def login_route(data: LoginIn, request: Request, response: Response) -> dict:
    """Confere usuário e senha e grava o cookie de sessão.

    A mesma mensagem de erro vale para usuário errado e senha errada, de
    propósito: dizer "esse usuário não existe" contaria a quem está
    tentando adivinhar qual metade ele já acertou.
    """
    settings = request.app.state.settings

    # O hash da senha do `.env` é calculado UMA vez e guardado em
    # `app.state`. Antes era refeito a cada tentativa, o que custava dois
    # scrypt por login (~200 ms) sem nenhum ganho: a senha do arquivo não
    # muda enquanto o servidor está no ar.
    #
    # Continua havendo um scrypt por tentativa — o da senha digitada — e
    # isso é proposital: são os ~100 ms que tornam a tentativa e erro
    # inviável.
    attempts = request.app.state.login_attempts
    client_key = request.client.host if request.client else ""
    now = monotonic()
    while attempts and next(iter(attempts.values()))[0] <= now:
        attempts.popitem(last=False)
    expires, hits = attempts.get(client_key, (now + 60, 0))
    if hits >= 12:
        raise HTTPException(429, "login_rate_limited", headers={"Retry-After": str(max(1, int(expires - now)))})
    attempts[client_key] = (expires, hits + 1)
    if len(attempts) > 2048:
        attempts.popitem(last=False)

    username = data.user.strip().casefold()
    is_producer = bool(settings.producer_user and username == settings.producer_user.casefold())
    user = settings.producer_user if is_producer else settings.admin_user
    password = settings.producer_password if is_producer else settings.admin_password
    hashes = request.app.state.password_hashes
    esperado = hashes.get(user)
    if esperado is None:
        esperado = await asyncio.to_thread(hash_password, password)
        hashes[user] = esperado

    usuario_confere = username == user.strip().casefold()
    senha_confere = await asyncio.to_thread(verify_password, data.password, esperado)

    if not (usuario_confere and senha_confere):
        logger.warning("login refused for user %r", data.user)
        raise HTTPException(status_code=401, detail="invalid_credentials")

    token = create_session_token(user, settings.session_secret, SESSION_LIFETIME_S)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_LIFETIME_S,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https" or os.getenv("VERCEL_ENV") == "production",
    )
    logger.info("login accepted for user %s", user)
    return {"user": user, "authenticated": True}


@router.post("/logout", response_model=SessionOut, summary="Sair do sistema")
async def logout_route(response: Response) -> dict:
    """Apaga o cookie. Como a sessão não tem estado no servidor, é só isso."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"user": None, "authenticated": False}


@router.get("/session", response_model=SessionOut, summary="Quem está logado")
async def session_route(request: Request) -> dict:
    """Usada pela tela para saber se precisa mandar o produtor ao login."""
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return {"user": settings.admin_user, "authenticated": True}
    usuario = session_user(request)
    return {"user": usuario, "authenticated": usuario is not None}
