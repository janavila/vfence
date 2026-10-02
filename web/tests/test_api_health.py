"""Testes da fase F1, parte da API e do frontend (item CT-01 do backlog).

Critério de aceite da fase: o servidor sobe, `/api/health` responde, `/`
serve o frontend com cabeçalho e logotipo, e o Leaflet é servido pelo
próprio Central — sem CDN.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from backend import __version__
from backend.clock import ISO_FORMAT


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


def test_health_responde_o_contrato_da_especificacao(client):
    """A resposta tem as três chaves da seção 6: status, version e time."""
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "version", "time"}
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_health_devolve_a_hora_em_utc_terminando_em_z(client):
    """A hora segue o formato de gravação do Central (seção 7).

    Este teste existe porque é o erro mais fácil de cometer: o
    `.isoformat()` do Python produz "+00:00" em vez de "Z", e os dois
    textos não casam em comparação nem em ordenação.
    """
    moment = client.get("/api/health").json()["time"]

    assert moment.endswith("Z")
    # Se o formato estiver errado, o strptime levanta ValueError e o
    # teste falha dizendo exatamente qual texto chegou.
    datetime.strptime(moment, ISO_FORMAT)


# ---------------------------------------------------------------------------
# Páginas e arquivos do frontend
# ---------------------------------------------------------------------------


def test_raiz_serve_a_pagina_inicial_com_cabecalho_e_logotipo(client):
    """`/` devolve o HTML do Início, com o cabeçalho e o logotipo."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "VFence" in page
    assert '<img src="/static/VFence.png" alt="VFence">' in page
    assert 'href="/css/style.css"' in page
    assert 'src="/js/ui.js"' in page


def test_logotipo_e_favicon_sao_servidos(client):
    """O PNG está no lugar esperado pela seção 4 e também vira favicon."""
    logo = client.get("/static/VFence.png")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/png"

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/png"


def test_css_e_javascript_proprios_sao_servidos(client):
    """A folha de estilo e o módulo de interface respondem."""
    assert client.get("/css/style.css").status_code == 200
    assert client.get("/js/ui.js").status_code == 200


def test_leaflet_e_servido_localmente_sem_cdn(client):
    """O Leaflet 1.9.4 vem do próprio Central (seção 3 da especificação).

    O Central roda na rede local de uma propriedade rural: depender de
    CDN significaria mapa quebrado sempre que a internet caísse. Este
    teste é a trava que impede alguém voltar a apontar para o unpkg,
    como ainda acontece no Monitor (app/templates/index.html:9).
    """
    script = client.get("/vendor/leaflet/leaflet.js")
    styles = client.get("/vendor/leaflet/leaflet.css")
    marker = client.get("/vendor/leaflet/images/marker-icon.png")

    assert script.status_code == 200
    assert styles.status_code == 200
    assert marker.status_code == 200
    assert "Leaflet 1.9.4" in script.text[:400]


def test_nenhuma_pagina_aponta_para_cdn_ou_fonte_externa(client):
    """Nenhum endereço externo no HTML (seções 3 e 11.1)."""
    page = client.get("/").text

    for forbidden in ("unpkg.com", "cdn.jsdelivr", "cdnjs", "fonts.googleapis", "fonts.gstatic"):
        assert forbidden not in page


# ---------------------------------------------------------------------------
# Documentação automática
# ---------------------------------------------------------------------------


def test_documentacao_automatica_esta_no_ar(client):
    """`/docs` e o esquema OpenAPI respondem (vantagem do FastAPI)."""
    assert client.get("/docs").status_code == 200

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/health" in schema.json()["paths"]


# ---------------------------------------------------------------------------
# Isolamento: importar o módulo não pode escrever no disco
# ---------------------------------------------------------------------------


def test_importar_o_modulo_nao_cria_arquivos_na_pasta_data(tmp_path):
    """Só importar `backend.main` não pode criar pasta nem arquivo.

    Este teste nasceu de um defeito real: a configuração do log ficava no
    corpo de `create_app()`, e como o módulo cria a aplicação no fim
    (`app = create_app()`), o simples `import backend.main` já criava
    `data/logs/central.log` na pasta de verdade — inclusive durante os
    testes, que deveriam ser isolados. A correção foi mover a
    configuração do log para dentro do `lifespan`.

    O teste roda em um PROCESSO SEPARADO, com `DATA_DIR` apontando para
    uma pasta vazia. A primeira versão conferia a pasta `data/` real e
    passava a falhar assim que alguém rodasse o servidor à mão — ela
    media "a pasta está vazia", não "o import não escreveu".
    """
    data_dir = tmp_path / "data"
    ambiente = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    resultado = subprocess.run(
        [sys.executable, "-c", "import backend.main"],
        env=ambiente, capture_output=True, text=True, timeout=60,
    )

    assert resultado.returncode == 0, resultado.stderr
    assert not data_dir.exists(), (
        f"importar backend.main criou {data_dir} com {list(data_dir.rglob('*'))}"
    )
