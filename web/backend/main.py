"""Montagem da aplicação Central.

O que faz
---------
É o ponto de entrada. Junta as peças na ordem certa: lê a configuração,
liga o log, prepara o banco, encaixa as rotas da API e passa a servir o
frontend. É o que o comando abaixo executa:

    uvicorn backend.main:app

A escolha da fábrica `create_app()`
-----------------------------------
Poderíamos criar o `app` direto no corpo do módulo, como faz o Monitor
(`app/main.py:45`). Preferimos uma função que CRIA a aplicação, com
`app = create_app()` só no fim.

O motivo é testabilidade. Os testes precisam de um Central com banco em
pasta temporária. Com a aplicação montada no import, a configuração já
estaria lida quando o teste tentasse mudá-la — é por isso que os testes
do Monitor precisam escrever em `os.environ` antes da linha de `import`
(`tests/test_api.py:1-3`). Com a fábrica, cada teste chama
`create_app(env_file=None)` com o ambiente que quiser, e ganha uma
aplicação limpa, com banco próprio.

Como o frontend é servido
-------------------------
Tudo sai da MESMA aplicação FastAPI, na mesma origem — por isso não
precisamos configurar CORS (seção 3 da especificação).

    /                      -> frontend/index.html         (as páginas têm rota própria)
    /css/...  /js/...      -> frontend/css/  frontend/js/
    /vendor/leaflet/...    -> frontend/vendor/leaflet/     (Leaflet 1.9.4 local, sem CDN)
    /static/VFence.png     -> static/                     (logotipo e favicon)
    /api/...               -> rotas da API
    /docs                  -> documentação automática

Montamos três pastas separadas em vez de uma só na raiz de propósito: um
`StaticFiles` montado em "/" capturaria qualquer endereço não
reconhecido, inclusive erros de digitação em `/api/...`, e esconderia
problemas atrás de um 404 de arquivo.

Onde se conecta
---------------
Importa `config`, `logging_setup`, `db` e os routers. Nas fases
seguintes, a única mudança aqui é mais uma linha `include_router` por
assunto e uma rota por página nova.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.config import DEFAULT_ENV_FILE, PROJECT_DIR, Settings, load_settings
from backend.db import Database
from backend.errors import ApiError
from backend.logging_setup import setup_logging
from backend.security import SESSION_COOKIE, read_session_token
from backend.services.realtime import RealtimeHub
from backend.routers import (
    auth,
    edge,
    fences,
    health,
    monitoring,
    settings as settings_router,
    ws,
)

logger = logging.getLogger("central")

FRONTEND_DIR = PROJECT_DIR / "frontend"
STATIC_DIR = PROJECT_DIR / "static"
LOGO_FILE = STATIC_DIR / "VFence.png"


def create_app(
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    settings: Settings | None = None,
    log_to_file: bool = True,
) -> FastAPI:
    """Monta e devolve a aplicação do Central.

    Parâmetros (todos opcionais; o uso normal é `create_app()`):

    - `env_file`: caminho do `.env`. O padrão é o `web/.env`; passar
      `None` ignora o arquivo e usa só as variáveis de ambiente, que é o
      que os testes fazem.
    - `settings`: configuração já pronta, para quem quiser montar na mão.
    - `log_to_file`: `False` desliga o `data/logs/central.log`, usado
      nos testes para não gerar arquivo de log a cada execução.
    """
    if settings is None:
        settings = load_settings(env_file=env_file)

    database = Database(settings.db_path)
    hub = RealtimeHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Trabalho de subida e de desligamento do servidor.

        `lifespan` é a forma atual do FastAPI de fazer isso (substitui os
        antigos `@app.on_event`). O que está antes do `yield` roda uma
        vez, quando o servidor sobe; o que está depois, no desligamento.

        **Por que o log é configurado aqui e não em `create_app`.** Ligar
        o log cria a pasta `data/logs/`. Se isso acontecesse no corpo de
        `create_app`, o simples `import backend.main` já escreveria no
        disco — e os testes, que importam este módulo, criariam arquivos
        na pasta `data/` de verdade mesmo usando banco temporário. Dentro
        do `lifespan`, nada é criado até o servidor realmente subir.

        O efeito colateral aceito: as duas primeiras linhas do Uvicorn
        ("Started server process" e "Waiting for application startup")
        saem antes desta configuração e usam o formato padrão dele. Da
        terceira linha em diante tudo segue o formato da seção 10.

        As operações de banco vão em `asyncio.to_thread` porque o
        `sqlite3` é bloqueante: rodá-lo direto aqui travaria o laço de
        eventos do servidor durante a criação das tabelas.
        """
        setup_logging(settings.log_level, settings.log_file if log_to_file else None)
        await asyncio.to_thread(database.initialize)
        await asyncio.to_thread(database.seed_from_settings, settings)
        logger.info(
            "central started (version=%s, data_dir=%s)", __version__, settings.data_dir
        )
        yield
        logger.info("central stopping")

    app = FastAPI(
        title="VFence Central",
        description=(
            "Aplicação web do produtor rural: desenho, validação, versionamento "
            "e entrega da cerca virtual. O Central não decide a zona do animal."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # Guardamos configuração e banco em app.state. É o lugar que o
    # FastAPI oferece para dependências de vida longa: as rotas das
    # próximas fases leem de lá (via Request) em vez de usar variável
    # global, o que mantém dois apps de teste independentes um do outro.
    app.state.settings = settings
    app.state.db = database
    app.state.hub = hub
    # Calculado na primeira tentativa de login e reaproveitado (ver auth.py).
    app.state.admin_password_hash = None

    # ---- login: o que fica aberto e o que fica protegido ---------------
    #
    # Quatro grupos ficam SEMPRE abertos, e cada um por um motivo:
    #
    # /api/edge/*   a Base se autentica com o token dela (Bearer). Exigir
    #               cookie aqui impediria o Raspberry de sincronizar — ele
    #               não tem navegador nem como fazer login.
    # /api/health   é a verificação de "o serviço está no ar". Protegê-la
    #               faria qualquer monitoramento externo ver o sistema
    #               como quebrado.
    # /api/auth/*   é o próprio login. Protegê-lo seria um laço infinito.
    # /login e os
    # arquivos      a tela de login precisa carregar o CSS, o JavaScript,
    #               o logotipo e o Leaflet antes de alguém entrar.
    CAMINHOS_ABERTOS = ("/api/edge/", "/api/auth/", "/css/", "/js/", "/vendor/", "/static/")
    CAMINHOS_ABERTOS_EXATOS = {"/api/health", "/login", "/favicon.ico", "/docs", "/openapi.json", "/redoc"}

    def _caminho_aberto(path: str) -> bool:
        return path in CAMINHOS_ABERTOS_EXATOS or path.startswith(CAMINHOS_ABERTOS)

    def sessao_valida(request: Request) -> str | None:
        """Devolve o usuário logado, ou None. Usada aqui e no /ws."""
        if not settings.auth_enabled:
            return settings.admin_user
        return read_session_token(
            request.cookies.get(SESSION_COOKIE, ""), settings.session_secret
        )

    app.state.sessao_valida = sessao_valida

    @app.middleware("http")
    async def exigir_sessao(request: Request, call_next):
        """Barra quem não está logado nas rotas protegidas.

        Responde de forma diferente conforme quem pediu:

        - **página** (`/`, `/editor`, …): redireciona para `/login`, que é
          o que o produtor espera ao abrir o endereço no navegador;
        - **API** (`/api/...`): devolve 401 com `{"detail": "not_authenticated"}`,
          para o JavaScript saber que precisa mandar ao login em vez de
          tentar desenhar uma página de HTML como se fosse JSON.
        """
        if not settings.auth_enabled or _caminho_aberto(request.url.path):
            return await call_next(request)

        if sessao_valida(request) is not None:
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "not_authenticated"})
        return RedirectResponse(url="/login", status_code=303)

    # ---- erros no formato da especificação -----------------------------
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, error: ApiError) -> JSONResponse:
        """Devolve o corpo do erro como está, sem o embrulho do FastAPI.

        O `HTTPException` sempre aninha o que recebe dentro de `detail`.
        A seção 6 da especificação define quatro chaves no MESMO nível
        (`detail`, `rule`, `message`, `violations`), então as recusas de
        cerca passam por aqui. Ver `backend/errors.py`.
        """
        return JSONResponse(status_code=error.status_code, content=error.payload)

    # ---- API ----------------------------------------------------------
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(fences.router)
    app.include_router(edge.router)
    app.include_router(settings_router.router)
    app.include_router(monitoring.router)
    app.include_router(ws.router)

    # ---- arquivos do frontend -----------------------------------------
    app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")
    app.mount("/vendor", StaticFiles(directory=FRONTEND_DIR / "vendor"), name="vendor")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- páginas ------------------------------------------------------
    # Uma rota por tela, com endereço curto e sem ".html" à vista. As
    # outras telas entram nas fases F5 (editor) e F6 (demais).
    @app.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        """Início (painel) — seção 11.4 da especificação."""
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/editor", include_in_schema=False)
    async def editor() -> FileResponse:
        """Editor de cerca — a tela principal do projeto (seção 11.3)."""
        return FileResponse(FRONTEND_DIR / "editor.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        """Tela de login (seção 11.4)."""
        return FileResponse(FRONTEND_DIR / "login.html")

    @app.get("/historico", include_in_schema=False)
    async def historico() -> FileResponse:
        """Histórico de cercas, com reativação (seção 11.4)."""
        return FileResponse(FRONTEND_DIR / "historico.html")

    @app.get("/rebanho", include_in_schema=False)
    async def rebanho() -> FileResponse:
        """Mapa e lista das coleiras (seção 11.4)."""
        return FileResponse(FRONTEND_DIR / "rebanho.html")

    @app.get("/eventos", include_in_schema=False)
    async def eventos() -> FileResponse:
        """Linha do tempo dos acontecimentos (seção 11.4)."""
        return FileResponse(FRONTEND_DIR / "eventos.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        """Ícone da aba: o mesmo logotipo (seção 11.2)."""
        return FileResponse(LOGO_FILE, media_type="image/png")

    return app


# O Uvicorn procura este nome: `uvicorn backend.main:app`.
app = create_app()
