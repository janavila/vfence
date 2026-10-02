"""Peças compartilhadas pelos testes do Central.

O que é um conftest
-------------------
Arquivo que o pytest carrega sozinho, sem ninguém importar. O que for
declarado aqui como *fixture* fica disponível para todos os testes da
pasta: basta um teste pedir o nome como parâmetro.

O problema que estas fixtures resolvem
--------------------------------------
Um teste nunca pode tocar no banco de verdade (`data/vfence.db`) nem
depender do `.env` de quem está rodando. As fixtures abaixo entregam, a
cada teste, um Central completo apontando para uma pasta temporária que
o pytest apaga depois.

Compare com os testes do Monitor (`tests/test_api.py:1-3`), que precisam
escrever em `os.environ` ANTES da linha de `import` para funcionar. Aqui
não precisa: `create_app()` lê a configuração quando é chamada, então o
`monkeypatch` da fixture vale (decisão explicada em `backend/main.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.db import Database
from backend.main import create_app
from backend.security import SESSION_COOKIE, create_session_token

# Valores usados pelos testes. O token é fixo de propósito: alguns testes
# conferem que o banco guarda o HASH dele, e não ele mesmo.
TEST_BASE_ID = "BASE01"
TEST_BASE_TOKEN = "token-de-teste-da-base"
TEST_BASE_LAT = -31.306200
TEST_BASE_LON = -54.063950
TEST_COLLARS = ("COL01", "COL02")
TEST_USER = "produtor"
TEST_PASSWORD = "senha-de-teste"
TEST_SESSION_SECRET = "chave-de-teste"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Pasta temporária que faz o papel de `web/data/` neste teste."""
    return tmp_path / "data"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Settings:
    """Configuração de teste, montada só a partir do ambiente.

    `monkeypatch.setenv` desfaz as alterações no fim do teste, então um
    teste não contamina o seguinte. E `env_file=None` garante que o
    `.env` de quem está rodando não entre na conta.
    """
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")  # testes sem log de INFO
    monkeypatch.setenv("BASE_ID", TEST_BASE_ID)
    monkeypatch.setenv("BASE_TOKEN", TEST_BASE_TOKEN)
    monkeypatch.setenv("BASE_LAT", str(TEST_BASE_LAT))
    monkeypatch.setenv("BASE_LON", str(TEST_BASE_LON))
    monkeypatch.setenv("COLARES_CONHECIDOS", ",".join(TEST_COLLARS))
    # O login fica LIGADO nos testes, como em uso real: é a configuração
    # que precisa estar coberta. O cliente de teste faz login sozinho.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_USER", TEST_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
    return load_settings(env_file=None)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Uma aplicação Central nova, com banco na pasta temporária.

    `log_to_file=False` evita criar `data/logs/central.log` a cada teste.
    """
    return create_app(settings=settings, log_to_file=False)


@pytest.fixture
def client_anonimo(app: FastAPI):
    """Cliente de teste SEM fazer login.

    Usado por dois grupos de testes: os que conferem que as rotas
    protegidas recusam quem não entrou, e os da Base — que precisam
    funcionar com o token no cabeçalho, sem cookie de sessão nenhum.

    Usado como gerenciador de contexto (`with`) de propósito: é isso que
    dispara o `lifespan` da aplicação, que é onde o banco é criado e a
    Base é cadastrada. Sem o `with`, as tabelas não existiriam.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(client_anonimo, settings: Settings):
    """Cliente de teste JÁ logado, como o navegador do produtor.

    A suíte inteira roda com o login LIGADO — a mesma configuração de uso
    real — em vez de testar um sistema aberto que ninguém vai usar.

    O cookie é criado direto, em vez de chamar `POST /api/auth/login`.
    O motivo é tempo: o scrypt leva ~100 ms de propósito, e com mais de
    250 testes fazendo login a suíte passava de 4 s para 17 s. O cookie
    produzido aqui é idêntico ao que a rota devolve — ela usa a MESMA
    função — e a rota de login em si é testada por inteiro em
    `test_api_auth.py`, inclusive com senha errada.
    """
    token = create_session_token(
        settings.admin_user, settings.session_secret, 3600
    )
    client_anonimo.cookies.set(SESSION_COOKIE, token)
    return client_anonimo


@pytest.fixture
def database(client, settings: Settings) -> Database:
    """Acesso direto ao banco já criado e povoado.

    Depende de `client` para garantir que o `lifespan` já rodou. Serve
    aos testes que conferem o banco sem passar pela API.
    """
    return Database(settings.db_path)


@pytest.fixture
def base_headers(settings: Settings) -> dict:
    """Cabeçalho de autenticação da Base, como o Monitor vai enviar."""
    return {"Authorization": f"Bearer {settings.base_token}"}


@pytest.fixture
def cerca_limpa() -> dict:
    """Corpo de uma cerca sem nenhum erro e sem nenhum aviso.

    Margens folgadas (dA = 8 m, dC = 5 m, acima da folga recomendada de
    4,5 m) para os testes não precisarem de `accept_warnings` quando o
    assunto não é aviso.
    """
    return {
        "name": "Piquete A",
        "margin_attention_m": 8.0,
        "margin_critical_m": 5.0,
        "points": [
            {"lat": -31.305800, "lon": -54.064600},
            {"lat": -31.305800, "lon": -54.063300},
            {"lat": -31.306600, "lon": -54.063300},
            {"lat": -31.306600, "lon": -54.064600},
        ],
    }
