"""O espelho em JavaScript concorda com o servidor? (fase F5)

Por que este teste existe
-------------------------
`frontend/js/geometry.js` repete as contas do servidor para o produtor
ver os problemas enquanto desenha. Duas implementações da mesma regra
divergem com o tempo — é o que sempre acontece. Este teste roda os
MESMOS casos de `casos_geometricos.json` nos dois lados e compara.

Se divergirem, o produtor veria "pode enviar" na tela e levaria um erro
do servidor ao clicar em Enviar. É o pior tipo de defeito de interface:
o sistema parece quebrado sem estar.

Sobre o Node
------------
O Node é usado APENAS como ferramenta de teste, para executar o mesmo
arquivo `.js` que o navegador carrega. Não é dependência do projeto: não
está no `requirements.txt`, não participa da execução e não há etapa de
compilação. Se o Node não estiver instalado na máquina, estes testes são
pulados e o restante da suíte continua valendo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.config import load_settings
from backend.services.canonical import to_e6
from backend.services.geometry import GeoPoint
from backend.services.validation import SEVERITY_ERROR, SEVERITY_WARNING, validate_fence

PASTA = Path(__file__).resolve().parent
CASOS = json.loads((PASTA / "casos_geometricos.json").read_text(encoding="utf-8"))
REFERENCIA = CASOS["_base_de_referencia"]

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None,
    reason="Node não instalado: o espelho JavaScript não pode ser conferido aqui "
           "(o navegador roda o mesmo arquivo sem nenhuma ferramenta)",
)


@pytest.fixture(scope="module")
def resultado_js() -> dict:
    """Roda o espelho JavaScript uma vez e devolve o resultado."""
    processo = subprocess.run(
        [node, str(PASTA / "espelho_geometry.mjs")],
        capture_output=True, text=True, timeout=60,
    )
    assert processo.returncode == 0, processo.stderr
    return json.loads(processo.stdout)


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_LAT", str(REFERENCIA["base_lat"]))
    monkeypatch.setenv("BASE_LON", str(REFERENCIA["base_lon"]))
    monkeypatch.setenv("BASE_RAIO_MAX_KM", str(REFERENCIA["base_raio_max_km"]))
    monkeypatch.setenv("LORA_ALCANCE_M", str(REFERENCIA["lora_alcance_m"]))
    monkeypatch.setenv("GNSS_ERRO_ESPERADO_M", str(REFERENCIA["gnss_erro_esperado_m"]))
    monkeypatch.setenv("GNSS_FATOR_SEGURANCA", str(REFERENCIA["gnss_fator_seguranca"]))
    return load_settings(env_file=None)


# ---------------------------------------------------------------------------
# As conferências de base
# ---------------------------------------------------------------------------


def test_quadrado_de_conferencia_no_javascript(resultado_js):
    """A mesma exigência da seção 5.3, agora no navegador: A = −1."""
    conferencias = resultado_js["conferencias"]

    assert conferencias["quadrado_area_com_sinal"] == pytest.approx(-1.0)
    assert conferencias["quadrado_sentido"] == "clockwise"
    assert conferencias["quadrado_perimetro"] == pytest.approx(4.0)


def test_arredondamento_para_micrograus_bate_com_o_python(resultado_js):
    """O `Math.round` do JavaScript arredonda meio para CIMA, e o nosso
    contrato é meio para LONGE DO ZERO.

    Em números negativos — e todas as coordenadas do projeto são
    negativas — os dois divergem. Daí o `Math.abs` com o sinal
    reaplicado em `paraMicrograus()`. Este teste é a trava.
    """
    do_js = resultado_js["conferencias"]["micrograus"]

    assert do_js == [to_e6(-31.306), to_e6(0.0000005), to_e6(-0.0000005)]
    assert do_js == [-31306000, 1, -1]
    # Prova de que o Math.round cru daria outro resultado em negativo:
    assert round(-0.5) != -1


# ---------------------------------------------------------------------------
# Os casos compartilhados, nos dois lados
# ---------------------------------------------------------------------------

NOMES = [c["nome"] for c in CASOS["casos"] if c.get("points")]


@pytest.mark.parametrize("nome", NOMES)
def test_o_javascript_concorda_com_o_python(nome, resultado_js, config):
    """Mesmo caso, mesmas regras violadas, mesma área e mesmo perímetro."""
    caso = next(c for c in CASOS["casos"] if c["nome"] == nome)
    do_js = resultado_js["casos"][nome]

    do_python = validate_fence(
        [GeoPoint(p["lat"], p["lon"]) for p in caso["points"]],
        caso["margin_attention_m"],
        caso["margin_critical_m"],
        config,
    )

    erros_python = sorted({v.rule for v in do_python.violations if v.severity == SEVERITY_ERROR})
    avisos_python = sorted({v.rule for v in do_python.violations if v.severity == SEVERITY_WARNING})

    assert do_js["errors"] == erros_python, f"erros diferentes no caso {nome}"
    assert do_js["warnings"] == avisos_python, f"avisos diferentes no caso {nome}"
    assert do_js["valid"] == do_python.valid
    assert do_js["orientation"] == do_python.orientation
    assert do_js["area_ha"] == pytest.approx(do_python.area_ha, rel=1e-9, abs=1e-12)
    assert do_js["perimeter_m"] == pytest.approx(do_python.perimeter_m, rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("nome", NOMES)
def test_as_mensagens_ao_produtor_sao_iguais_nos_dois_lados(nome, resultado_js, config):
    """Mesma regra, mesmo texto.

    Se as mensagens divergissem, o produtor leria uma coisa ao desenhar
    e outra ao enviar — e não teria como saber qual acreditar.
    """
    caso = next(c for c in CASOS["casos"] if c["nome"] == nome)

    do_python = validate_fence(
        [GeoPoint(p["lat"], p["lon"]) for p in caso["points"]],
        caso["margin_attention_m"],
        caso["margin_critical_m"],
        config,
    )

    assert sorted(resultado_js["casos"][nome]["messages"]) == sorted(
        v.message for v in do_python.violations
    )


@pytest.mark.parametrize("nome", NOMES)
def test_os_indices_destacados_no_mapa_sao_iguais(nome, resultado_js, config):
    """Os pontos e lados a destacar em vermelho também precisam bater."""
    caso = next(c for c in CASOS["casos"] if c["nome"] == nome)

    do_python = validate_fence(
        [GeoPoint(p["lat"], p["lon"]) for p in caso["points"]],
        caso["margin_attention_m"],
        caso["margin_critical_m"],
        config,
    )

    lados_python = [list(e) for v in do_python.violations for e in v.edges]
    pontos_python = [p for v in do_python.violations for p in v.points]

    assert sorted(resultado_js["casos"][nome]["edges"]) == sorted(lados_python)
    assert sorted(resultado_js["casos"][nome]["points"]) == sorted(pontos_python)


# ---------------------------------------------------------------------------
# Faixas de margem (item SHOULD da seção 11.3)
# ---------------------------------------------------------------------------


def test_faixa_de_margem_e_desenhavel_numa_cerca_folgada(resultado_js):
    conferencias = resultado_js["conferencias"]

    assert conferencias["faixa_cerca_boa"] == 4  # um ponto por canto
    assert conferencias["faixa_area_menor"] is True  # desenhada para DENTRO


def test_faixa_de_margem_e_omitida_quando_nao_cabe(resultado_js):
    """Num triângulo de 2 m com margem de 8 m, o contorno deslocado se
    dobra sobre si mesmo. Devolver `null` faz o editor explicar em vez de
    desenhar uma faixa falsa — mostrar a faixa errada seria pior que não
    mostrar nenhuma."""
    assert resultado_js["conferencias"]["faixa_triangulo_pequeno"] is None
