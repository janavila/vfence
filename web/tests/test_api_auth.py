"""Testes do login (fase F8, item CT-08 do backlog).

Critério de aceite: "rotas protegidas". Além disso, conferimos aqui o
que NÃO pode ser protegido — porque proteger a coisa errada quebraria o
sistema de forma difícil de perceber:

- `/api/edge/*`: a Base é um Raspberry sem navegador. Exigir cookie dela
  impediria a sincronização da cerca, e ninguém entenderia por quê.
- `/api/health`: é a verificação de "está no ar". Protegida, qualquer
  monitoramento externo veria o sistema como quebrado.
"""

from __future__ import annotations

import pytest

from backend.security import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    read_session_token,
    verify_password,
)
from tests.conftest import TEST_PASSWORD, TEST_USER


# ===========================================================================
# As funções de segurança
# ===========================================================================


def test_senha_e_guardada_com_scrypt_e_sal_aleatorio():
    """Duas pessoas com a mesma senha têm hashes diferentes.

    É o papel do sal: impede o uso de tabelas pré-calculadas, em que
    alguém já tem o hash das senhas mais comuns.
    """
    primeiro = hash_password("mesma-senha")
    segundo = hash_password("mesma-senha")

    assert primeiro != segundo
    assert primeiro.startswith("scrypt$")
    assert verify_password("mesma-senha", primeiro)
    assert verify_password("mesma-senha", segundo)


def test_senha_errada_nao_confere():
    guardado = hash_password("senha-certa")

    assert verify_password("senha-errada", guardado) is False
    assert verify_password("", guardado) is False


@pytest.mark.parametrize("estragado", ["", "sem-dolar", "md5$aa$bb", "scrypt$zz$bb", "scrypt$aa"])
def test_hash_estragado_nao_derruba_a_conferencia(estragado):
    """Uma linha corrompida devolve `False`, não uma exceção."""
    assert verify_password("qualquer", estragado) is False


def test_cookie_de_sessao_assinado_e_lido_de_volta():
    token = create_session_token("produtor", "chave-secreta", 3600)

    assert read_session_token(token, "chave-secreta") == "produtor"


def test_cookie_assinado_com_outra_chave_e_recusado():
    """Trocar o SESSION_SECRET invalida todas as sessões — é a saída se
    algum dia for preciso desconectar todo mundo."""
    token = create_session_token("produtor", "chave-antiga", 3600)

    assert read_session_token(token, "chave-nova") is None


def test_cookie_alterado_e_recusado():
    """Sem a conferência da assinatura, qualquer pessoa escreveria
    `{"user": "produtor"}` no próprio cookie e entraria."""
    token = create_session_token("produtor", "chave", 3600)
    alterado = token[:-1] + ("0" if token[-1] != "0" else "1")

    assert read_session_token(alterado, "chave") is None


def test_cookie_com_conteudo_trocado_e_recusado():
    """Tentativa direta: trocar os dados e manter a assinatura antiga."""
    import base64
    import json

    token = create_session_token("produtor", "chave", 3600)
    _, assinatura = token.rsplit(".", 1)
    falso = base64.urlsafe_b64encode(
        json.dumps({"user": "invasor", "exp": 9999999999}).encode()
    ).decode().rstrip("=")

    assert read_session_token(f"{falso}.{assinatura}", "chave") is None


def test_sessao_expirada_e_recusada():
    assert read_session_token(create_session_token("produtor", "chave", -1), "chave") is None


@pytest.mark.parametrize("estragado", ["", "sem-ponto", "aaa.bbb", "....", "!!!.!!!"])
def test_cookie_estragado_nao_derruba_a_leitura(estragado):
    assert read_session_token(estragado, "chave") is None


# ===========================================================================
# POST /api/auth/login
# ===========================================================================


def test_login_com_credenciais_certas_grava_o_cookie(client_anonimo):
    resposta = client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER, "password": TEST_PASSWORD}
    )

    assert resposta.status_code == 200
    assert resposta.json() == {"user": TEST_USER, "authenticated": True}
    assert SESSION_COOKIE in resposta.cookies


def test_cookie_de_sessao_e_httponly(client_anonimo):
    """`HttpOnly` impede o JavaScript da página de ler o cookie, o que
    limita o estrago de uma eventual injeção de script."""
    resposta = client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER, "password": TEST_PASSWORD}
    )

    cabecalho = resposta.headers["set-cookie"].lower()
    assert "httponly" in cabecalho
    assert "samesite=lax" in cabecalho
    assert "path=/" in cabecalho


@pytest.mark.parametrize(
    "usuario,senha",
    [
        ("produtor", "senha-errada"),
        ("outro-usuario", TEST_PASSWORD),
        ("outro-usuario", "senha-errada"),
    ],
)
def test_credenciais_erradas_respondem_401(client_anonimo, usuario, senha):
    resposta = client_anonimo.post(
        "/api/auth/login", json={"user": usuario, "password": senha}
    )

    assert resposta.status_code == 401
    assert resposta.json() == {"detail": "invalid_credentials"}


def test_a_mensagem_de_erro_nao_revela_qual_campo_errou(client_anonimo):
    """Dizer "esse usuário não existe" contaria a quem está adivinhando
    qual metade ele já acertou."""
    senha_errada = client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER, "password": "x"}
    ).json()
    usuario_errado = client_anonimo.post(
        "/api/auth/login", json={"user": "ninguem", "password": TEST_PASSWORD}
    ).json()

    assert senha_errada == usuario_errado


def test_usuario_nao_diferencia_maiusculas(client_anonimo):
    """"Produtor" e "produtor" são a mesma pessoa. A SENHA, sim,
    diferencia — nunca se afrouxa a senha."""
    assert client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER.upper(), "password": TEST_PASSWORD}
    ).status_code == 200
    assert client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER, "password": TEST_PASSWORD.upper()}
    ).status_code == 401


# ===========================================================================
# Sessão e saída
# ===========================================================================


def test_session_diz_que_ninguem_esta_logado(client_anonimo):
    assert client_anonimo.get("/api/auth/session").json() == {
        "user": None, "authenticated": False
    }


def test_session_diz_quem_esta_logado(client):
    assert client.get("/api/auth/session").json() == {
        "user": TEST_USER, "authenticated": True
    }


def test_logout_apaga_o_cookie(client_anonimo):
    """Usa o login de VERDADE, não o cookie posto à mão pela fixture.

    Motivo: o `delete_cookie` do servidor casa o cookie por nome, caminho
    e domínio. Um cookie escrito direto no cliente de teste não carrega
    domínio, então a remoção não o encontraria — e o teste falharia por
    artefato do arrumador de testes, não por defeito do sistema.
    """
    client_anonimo.post(
        "/api/auth/login", json={"user": TEST_USER, "password": TEST_PASSWORD}
    )
    assert client_anonimo.get("/api/auth/session").json()["authenticated"] is True

    client_anonimo.post("/api/auth/logout")

    assert client_anonimo.get("/api/auth/session").json()["authenticated"] is False
    assert client_anonimo.get("/api/fences").status_code == 401


# ===========================================================================
# O que fica protegido
# ===========================================================================


@pytest.mark.parametrize(
    "rota",
    ["/api/fences", "/api/fences/active", "/api/collars", "/api/events",
     "/api/bases", "/api/settings"],
)
def test_api_do_navegador_exige_login(client_anonimo, rota):
    resposta = client_anonimo.get(rota)

    assert resposta.status_code == 401
    assert resposta.json() == {"detail": "not_authenticated"}


def test_criar_cerca_exige_login(client_anonimo, cerca_limpa):
    assert client_anonimo.post("/api/fences", json=cerca_limpa).status_code == 401


@pytest.mark.parametrize("pagina", ["/", "/editor", "/historico", "/rebanho", "/eventos"])
def test_paginas_redirecionam_para_o_login(client_anonimo, pagina):
    """Página devolve redirecionamento, não 401: é o que o produtor espera
    ao digitar o endereço no navegador."""
    resposta = client_anonimo.get(pagina, follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login"


def test_websocket_sem_login_e_recusado(client_anonimo):
    """O middleware de sessão só vê requisições HTTP, então a conferência
    do WebSocket fica na própria rota. Sem este teste, qualquer pessoa na
    rede receberia a telemetria do rebanho sem fazer login."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client_anonimo.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_websocket_com_login_funciona(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"


# ===========================================================================
# O que NÃO pode ficar protegido
# ===========================================================================


def test_as_rotas_da_base_nao_exigem_cookie(client_anonimo, base_headers, cerca_limpa):
    """A Base é um Raspberry sem navegador: exigir cookie dela impediria a
    sincronização da cerca, e ninguém entenderia por quê.

    Ela se autentica com o token no cabeçalho, que é o contrato I2.
    """
    # Criamos a cerca com um cliente logado, como o produtor faria.
    logado = client_anonimo
    from backend.security import create_session_token
    logado.cookies.set(
        SESSION_COOKIE,
        create_session_token(TEST_USER, logado.app.state.settings.session_secret, 3600),
    )
    logado.post("/api/fences", json=cerca_limpa)
    logado.cookies.clear()

    # E agora a Base, SEM cookie nenhum.
    assert logado.post(
        "/api/edge/heartbeat", json={"base_id": "BASE01"}, headers=base_headers
    ).status_code == 200
    assert logado.get("/api/edge/fences/1", headers=base_headers).status_code == 200
    assert logado.post(
        "/api/edge/batch", json={"base_id": "BASE01", "items": []}, headers=base_headers
    ).status_code == 200


def test_health_nao_exige_login(client_anonimo):
    """É a verificação de "está no ar". Protegida, qualquer monitoramento
    externo veria o sistema como quebrado."""
    assert client_anonimo.get("/api/health").status_code == 200


def test_a_tela_de_login_e_seus_arquivos_carregam_sem_login(client_anonimo):
    """A tela de login precisa do CSS, do logotipo e do Leaflet antes de
    alguém entrar."""
    for caminho in ["/login", "/css/style.css", "/js/ui.js",
                    "/static/VFence.png", "/vendor/leaflet/leaflet.css", "/favicon.ico"]:
        assert client_anonimo.get(caminho).status_code == 200, caminho


def test_a_tela_de_login_tem_os_campos_e_nao_vaza_a_senha(client_anonimo):
    pagina = client_anonimo.get("/login").text

    assert 'id="user"' in pagina
    assert 'type="password"' in pagina
    assert TEST_PASSWORD not in pagina


def test_a_documentacao_continua_acessivel(client_anonimo):
    """`/docs` fica aberto de propósito: é a referência que o responsável
    pela Base usa para implementar o lado dele, e não expõe dado nenhum
    do rebanho — só a forma das rotas."""
    assert client_anonimo.get("/docs").status_code == 200
    assert client_anonimo.get("/openapi.json").status_code == 200


# ===========================================================================
# Desligar o login
# ===========================================================================


def test_com_auth_enabled_false_o_sistema_fica_aberto(monkeypatch, tmp_path):
    """`AUTH_ENABLED=false` existe para a bancada e as demonstrações, onde
    pedir senha a cada teste atrapalha. NÃO é da especificação, e o
    padrão é `true`."""
    from fastapi.testclient import TestClient

    from backend.config import load_settings
    from backend.main import create_app

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    app = create_app(settings=load_settings(env_file=None), log_to_file=False)

    with TestClient(app) as aberto:
        assert aberto.get("/api/fences").status_code == 200
        assert aberto.get("/", follow_redirects=False).status_code == 200
        assert aberto.get("/api/auth/session").json()["authenticated"] is True


def test_auth_enabled_e_true_por_padrao(monkeypatch, tmp_path):
    """O padrão seguro: quem não configurar nada fica com o login ligado."""
    from backend.config import load_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    assert load_settings(env_file=None).auth_enabled is True


def test_senha_vazia_com_login_ligado_impede_o_servidor_de_subir(monkeypatch, tmp_path):
    """Subir com senha vazia deixaria o sistema aberto sem ninguém
    perceber. Melhor recusar alto e cedo."""
    from backend.config import ConfigError, load_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "")

    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"):
        load_settings(env_file=None)
