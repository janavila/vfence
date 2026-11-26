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

    /                      -> redireciona para /editor    (as páginas têm rota própria)
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
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from collections import OrderedDict
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.config import ConfigError, DEFAULT_ENV_FILE, PROJECT_DIR, Settings, load_settings
from backend.db import Database, DatabaseConfigurationError, describe_connection_error
from backend.errors import ApiError
from backend.logging_setup import setup_logging
from backend.navigation import header
from backend.permissions import browser_origin_matches, producer_path, role_for, session_user
from backend.services.realtime import RealtimeHub
from backend.routers import (
    auth,
    areas,
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

# Intervalo mínimo entre duas tentativas de preparar um banco indisponível.
DB_RETRY_SECONDS = 5.0


def create_app(
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    settings: Settings | None = None,
    log_to_file: bool = True,
    property_id: str | None = None,
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
    if os.getenv("VERCEL_ENV") == "production" and (
        not settings.auth_enabled or settings.admin_password == "troque-esta-senha"
        or settings.session_secret == "troque-esta-chave" or settings.base_token == "troque-este-token"
    ):
        raise ConfigError("Produção exige autenticação e credenciais provisionadas")

    database = Database(settings.database_target)
    hub = RealtimeHub()

    def preparar_banco() -> None:
        """Cria o schema, amarra a propriedade e cadastra Base e coleiras.

        Tudo idempotente: pode rodar de novo a cada tentativa de reconexão.
        """
        database.initialize()
        if property_id is not None:
            database.bind_property(property_id, settings.base_id)
        database.seed_from_settings(settings)

    async def tentar_preparar_banco(*, config_fatal: bool) -> bool:
        """Uma tentativa de deixar o banco pronto. Devolve se conseguiu.

        **Por que uma falha aqui não derruba mais o servidor.** Antes, se o
        PostgreSQL não respondesse na subida, o `lifespan` levantava a
        exceção e o Uvicorn encerrava o processo. No Vercel isso virava
        `500 FUNCTION_INVOCATION_FAILED` em TODAS as rotas — até a tela de
        login, que nem usa o banco — e o produtor não via mensagem nenhuma.
        Agora as telas continuam abrindo, a API responde 503
        (`database_unavailable`, que o frontend já traduz para "O servidor
        teve um problema. Tente de novo em instantes.") e a próxima
        requisição tenta de novo, no máximo a cada `DB_RETRY_SECONDS`.

        A exceção é o erro de CONFIGURAÇÃO na subida (driver ausente, banco
        de uma propriedade já amarrado a outra): esse não se resolve
        sozinho, e servir dados com ele seria pior do que não subir.
        """
        app.state.db_last_attempt = time.monotonic()
        try:
            await asyncio.to_thread(preparar_banco)
        except DatabaseConfigurationError:
            app.state.db_ready = False
            if config_fatal:
                raise
            logger.exception("database configuration error; API answers 503")
            return False
        except Exception as error:  # noqa: BLE001 - qualquer falha vira 503, não queda
            app.state.db_ready = False
            dica = ""
            if not settings.uses_postgresql and os.getenv("VERCEL"):
                dica = " | DATABASE_URL ausente: no Vercel o SQLite não tem onde gravar"
            logger.error(
                "database unavailable (%s: %s); pages stay up, API answers 503 "
                "and retries in %.0fs%s",
                type(error).__name__,
                describe_connection_error(error, settings.database_url or ""),
                app.state.db_retry_s, dica,
            )
            return False
        if not app.state.db_ready:
            app.state.db_ready = True
            logger.info("database available (backend=%s)", database.backend)
        return True

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

        As operações de banco vão em `asyncio.to_thread` porque os drivers
        síncronos SQLite e PostgreSQL são bloqueantes: executá-los direto aqui
        travaria o laço de eventos durante a criação das tabelas.
        """
        # No PostgreSQL/Vercel, nenhum arquivo de runtime é criado dentro do
        # deployment. Os logs ficam no stdout e são coletados pela plataforma.
        file_log = settings.log_file if log_to_file and not settings.uses_postgresql else None
        setup_logging(settings.log_level, file_log)
        pronto = await tentar_preparar_banco(config_fatal=True)
        logger.info(
            "central started (version=%s, database=%s, %s)",
            __version__, database.backend, "ready" if pronto else "UNAVAILABLE",
        )
        try:
            yield
        finally:
            await hub.close()
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
    app.state.property_id = property_id
    app.state.db = database
    app.state.hub = hub
    # Calculado na primeira tentativa de login e reaproveitado (ver auth.py).
    app.state.password_hashes = {}
    app.state.login_attempts = OrderedDict()
    # Situação do banco (ver `tentar_preparar_banco`). Começa "não pronto":
    # se a plataforma não rodar o `lifespan`, a primeira chamada à API
    # prepara o banco do mesmo jeito.
    app.state.db_ready = False
    app.state.db_last_attempt = float("-inf")
    app.state.db_retry_s = DB_RETRY_SECONDS
    app.state.db_lock = asyncio.Lock()

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
    CAMINHOS_ABERTOS_EXATOS = {"/", "/visitante", "/api/public/rebanho", "/api/health",
                              "/login", "/favicon.ico", "/docs", "/openapi.json", "/redoc"}

    def _caminho_aberto(path: str) -> bool:
        return path in CAMINHOS_ABERTOS_EXATOS or path.startswith(CAMINHOS_ABERTOS)

    def sessao_valida(request: Request) -> str | None:
        """Devolve o usuário logado, ou None. Usada aqui e no /ws."""
        return session_user(request)

    app.state.sessao_valida = sessao_valida

    # Declarado ANTES do `exigir_sessao` de propósito: no FastAPI o último
    # middleware declarado é o mais externo, então o login é conferido
    # primeiro e quem não entrou recebe 401 sem tocar no banco.
    @app.middleware("http")
    async def exigir_banco(request: Request, call_next):
        """Responde 503 na API enquanto o banco estiver indisponível.

        Ficam de fora as páginas e arquivos (não usam o banco), o
        `/api/health` (diz se o PROCESSO está no ar — ver health.py) e o
        login (o usuário vem do `.env`, não do banco). Para o resto, cada
        requisição pode disparar uma nova tentativa, uma de cada vez e no
        máximo a cada `db_retry_s` segundos, para um banco fora do ar não
        receber uma enxurrada de conexões.
        """
        path = request.url.path
        if (
            app.state.db_ready
            or not path.startswith("/api/")
            or path == "/api/health"
            or path.startswith("/api/auth/")
        ):
            return await call_next(request)
        async with app.state.db_lock:
            espera = time.monotonic() - app.state.db_last_attempt
            if not app.state.db_ready and espera >= app.state.db_retry_s:
                await tentar_preparar_banco(config_fatal=False)
        if app.state.db_ready:
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={"detail": "database_unavailable"},
            headers={"Retry-After": str(max(1, round(app.state.db_retry_s)))},
        )

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
        path = request.url.path
        if _caminho_aberto(path):
            return await call_next(request)

        request.state.user = sessao_valida(request)
        role = role_for(settings, request.state.user)
        if role == "management" or (role == "producer" and producer_path(path)):
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not browser_origin_matches(request):
                return JSONResponse(status_code=403, content={"detail": "origin_not_allowed"})
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        if role == "producer":
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"detail": "forbidden"})
            return RedirectResponse(url="/produtor", status_code=303)

        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "not_authenticated"})
        next_area = path if producer_path(path) or path == "/gestao" or path.startswith("/gestao/") else None
        if next_area and request.url.query:
            next_area += "?" + request.url.query
        login_url = "/login?" + urlencode({"next": next_area}) if next_area else "/login"
        return RedirectResponse(url=login_url, status_code=303)

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
    app.include_router(areas.router)
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
    # Uma rota por tela, com endereço curto e sem ".html" à vista.
    #
    # As telas se dividem em dois modos, escolhidos no cabeçalho:
    # Produtor (editor e rebanho) e Gestão (painel, histórico e
    # eventos). O modo é só de navegação, não de permissão: o login
    # continua sendo um usuário único.
    @app.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "inicio.html")

    @app.get("/visitante", include_in_schema=False)
    async def visitante() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "visitante.html")

    @app.get("/produtor", include_in_schema=False)
    async def produtor() -> HTMLResponse:
        return await area_page("visitante.html", "producer", "herd", "/")

    @app.get("/produtor/avisos", include_in_schema=False)
    async def produtor_avisos():
        return RedirectResponse("/produtor#visitor-alerts-title", status_code=303)

    @app.get("/produtor/cercas", include_in_schema=False)
    @app.get("/produtor/editor", include_in_schema=False)
    async def editor_produtor():
        return await area_page("editor.html", "producer", "fences", "/produtor/cercas")

    @app.get("/gestao/cercas", include_in_schema=False)
    async def minhas_cercas():
        return await area_page("editor.html", "management", "fences", "/gestao")

    @app.get("/gestao/visitante", include_in_schema=False)
    async def gestao_visitante():
        return RedirectResponse("/gestao/painel#visibilidade-publica", status_code=303)

    @app.get("/gestao", include_in_schema=False)
    async def gestao() -> HTMLResponse:
        return await area_page("visitante.html", "management", "herd", "/")

    @app.get("/painel", include_in_schema=False)
    @app.get("/gestao/painel", include_in_schema=False)
    async def painel() -> HTMLResponse:
        """Situação da propriedade, do modo Gestão (seção 11.4)."""
        return await area_page("gestao-painel.html", "management", "overview", "/gestao")

    @app.get("/editor", include_in_schema=False)
    @app.get("/gestao/editor", include_in_schema=False)
    async def editor() -> HTMLResponse:
        """Editor de cerca — a tela principal do projeto (seção 11.3)."""
        return await area_page("editor.html", "management", "fences", "/gestao/cercas")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        """Tela de login (seção 11.4)."""
        return FileResponse(FRONTEND_DIR / "login.html")

    @app.get("/historico", include_in_schema=False)
    @app.get("/gestao/historico", include_in_schema=False)
    async def historico() -> HTMLResponse:
        """Histórico de cercas, com reativação (seção 11.4)."""
        return await area_page("gestao-historico.html", "management", "history", "/gestao")

    @app.get("/rebanho", include_in_schema=False)
    @app.get("/gestao/rebanho", include_in_schema=False)
    async def rebanho() -> HTMLResponse:
        """Mapa e lista das coleiras (seção 11.4)."""
        return RedirectResponse("/gestao#dados-coleiras", status_code=303)

    @app.get("/eventos", include_in_schema=False)
    @app.get("/gestao/eventos", include_in_schema=False)
    async def eventos() -> HTMLResponse:
        """Linha do tempo dos acontecimentos (seção 11.4)."""
        return await area_page("gestao-eventos.html", "management", "events", "/gestao")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        """Ícone da aba: o mesmo logotipo (seção 11.2)."""
        return FileResponse(LOGO_FILE, media_type="image/png")

    return app


async def area_page(file: str, area: str, page: str, back: str, *, indicator: bool = False) -> HTMLResponse:
    content = await asyncio.to_thread((FRONTEND_DIR / file).read_text, encoding="utf-8")
    if area == "producer" or (area == "management" and page in {"herd", "fences"}):
        return await operational_page(content, page, area)
    content = content.replace("<!-- area-header -->", header(area, page, back, indicator=indicator), 1)
    if page == "overview":
        publication = await asyncio.to_thread((FRONTEND_DIR / "publicacao.html").read_text, encoding="utf-8")
        form = publication[publication.index("<form"):publication.index("</form>") + len("</form>")]
        content = content.replace("<!-- public-policy-form -->", form, 1)
    return HTMLResponse(content)


async def operational_page(content: str, page: str, area: str) -> HTMLResponse:
    content = content.replace("</head>", '  <link rel="stylesheet" href="/css/produtor.css">\n</head>', 1)
    area_header = header(area, page, "/produtor")
    if page == "herd":
        start = content.index("<header")
        end = content.index("</header>", start) + len("</header>")
        content = content[:start] + area_header + content[end:]
        content = content.replace('class="visitor-page"', 'class="visitor-page producer-area"', 1)
        content = content.replace("Rebanho público", "Meu Rebanho")
        content = content.replace("Consultando informações públicas…", "Consultando os dados da propriedade…", 1)
        content = content.replace("Acompanhe a localização dos animais e sua situação em relação à cerca virtual.",
                                  "Acompanhe a localização e a situação dos seus animais.")
        content = content.replace('<p id="visitor-update"', '<div class="producer-intro-actions"><a class="button primary" '
                                  'href="/produtor/cercas">Gerenciar cercas</a><p id="visitor-update"', 1)
        content = content.replace("Consultando o rebanho…</p>", "Consultando o rebanho…</p></div>", 1)
        sections = await asyncio.to_thread((FRONTEND_DIR / "producer-herd-panels.html").read_text, encoding="utf-8")
        current, history = sections.split("<!-- producer-history -->", 1)
        anchor = '    <section class="visitor-panel" aria-labelledby="visitor-animals-title">'
        content = content.replace(anchor, current + anchor, 1).replace("</main>", history + "</main>", 1)
    else:
        extra = await asyncio.to_thread((FRONTEND_DIR / "producer-editor-panels.html").read_text, encoding="utf-8")
        toolbar, history = extra.split("<!-- producer-fence-history -->", 1)
        content = content.replace("<!-- area-header -->", area_header, 1)
        content = content.replace("<body>", '<body class="visitor-page producer-area producer-editor">', 1)
        content = content.replace('<link rel="stylesheet" href="/css/style.css">',
                                  '<link rel="stylesheet" href="/css/style.css">\n'
                                  '  <link rel="stylesheet" href="/css/visitante.css">', 1)
        content = content.replace("<title>Editor de cerca", "<title>Minhas Cercas", 1)
        content = content.replace('<main class="workspace">', '<main class="workspace visitor-container">', 1)
        content = content.replace('<div class="page-heading">', toolbar + '<details class="producer-rules visitor-panel">'
                                  '<summary>Regras de validação</summary><div class="page-heading">', 1)
        content = content.replace('<div class="notice info" id="editor-announce"', '</details>\n'
                                  '<div class="notice info" id="editor-announce"', 1)
        content = content.replace('<section class="stack">', '<section class="stack producer-editor-layout">', 1)
        content = content.replace('<div class="table-scroll">\n      <table class="data-table">',
                                  '<div id="producer-delivery-summary" class="panel-body"></div>'
                                  '<div class="table-scroll">\n      <table class="data-table">', 1)
        content = content.replace('</main>', history + '</main>\n'
                                  '<footer class="visitor-footer">VFence — Sistema inteligente de cercamento virtual</footer>', 1)
    if area == "management":
        content = content.replace("</head>", '<link rel="stylesheet" href="/css/gestao.css">\n</head>', 1)
        content = content.replace('class="visitor-page producer-area', 'class="visitor-page producer-area management-area', 1)
        content = content.replace('href="/produtor/cercas"', 'href="/gestao/cercas"')
        if page == "herd":
            technical = await asyncio.to_thread((FRONTEND_DIR / "gestao-coleiras.html").read_text, encoding="utf-8")
            content = content.replace("Meu Rebanho", "Rebanho").replace("</main>", technical + "</main>", 1)
        else:
            content = content.replace("Minhas Cercas", "Cercas")
    return HTMLResponse(content)


def create_runtime_app():
    configured = os.getenv("VFENCE_PROPERTIES_CONFIG")
    if configured:
        from backend.properties import create_property_app, load_properties

        return create_property_app(load_properties(Path(configured), load_settings()))
    return create_app()


# O Uvicorn procura este nome: `uvicorn backend.main:app`.
app = create_runtime_app()
