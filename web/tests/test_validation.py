"""Testes das regras VAL-01 a VAL-12 (fase F2).

Dois grupos de testes:

1. **Por regra**: um teste por regra, com uma cerca feita para disparar
   só aquela. Serve de documentação executável do que cada regra faz.
2. **Pelos casos compartilhados**: roda o `casos_geometricos.json`
   inteiro e confere o conjunto EXATO de erros e avisos de cada caso. É
   este grupo que o `geometry.js` (fase F5) e o firmware precisam
   reproduzir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.config import load_settings
from backend.services.geometry import GeoPoint, LocalProjection
from backend.services.validation import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    val09_is_small_for_margin,
    validate_fence,
)

CASOS = json.loads(
    (Path(__file__).parent / "casos_geometricos.json").read_text(encoding="utf-8")
)
REFERENCIA = CASOS["_base_de_referencia"]

# Cerca folgada, usada como base dos testes por regra: ela passa limpa,
# então qualquer violação que apareça vem da alteração do teste.
CERCA_LIMPA = [
    GeoPoint(-31.305800, -54.064600),
    GeoPoint(-31.305800, -54.063300),
    GeoPoint(-31.306600, -54.063300),
    GeoPoint(-31.306600, -54.064600),
]
MARGEM_ATENCAO = 8.0
MARGEM_CRITICA = 5.0


@pytest.fixture
def config(monkeypatch, tmp_path):
    """Configuração alinhada com `_base_de_referencia` do JSON.

    Os casos compartilhados assumem esta posição de Base e estes limites;
    se o teste usasse outra, as regras VAL-03 e VAL-10 dariam outro
    resultado e a comparação com o firmware perderia sentido.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_LAT", str(REFERENCIA["base_lat"]))
    monkeypatch.setenv("BASE_LON", str(REFERENCIA["base_lon"]))
    monkeypatch.setenv("BASE_RAIO_MAX_KM", str(REFERENCIA["base_raio_max_km"]))
    monkeypatch.setenv("LORA_ALCANCE_M", str(REFERENCIA["lora_alcance_m"]))
    monkeypatch.setenv("GNSS_ERRO_ESPERADO_M", str(REFERENCIA["gnss_erro_esperado_m"]))
    monkeypatch.setenv("GNSS_FATOR_SEGURANCA", str(REFERENCIA["gnss_fator_seguranca"]))
    return load_settings(env_file=None)


def regras(resultado, severidade) -> set[str]:
    """Conjunto de regras violadas com uma dada severidade."""
    return {v.rule for v in resultado.violations if v.severity == severidade}


def validar(pontos, config, atencao=MARGEM_ATENCAO, critica=MARGEM_CRITICA):
    return validate_fence(pontos, atencao, critica, config)


# ===========================================================================
# 1. Um teste por regra
# ===========================================================================


def test_cerca_limpa_passa_sem_erro_e_sem_aviso(config):
    """O caminho feliz: precisa poder ser salva sem o produtor marcar nada."""
    resultado = validar(CERCA_LIMPA, config)

    assert resultado.valid is True
    assert resultado.violations == []
    assert resultado.orientation == "clockwise"
    assert resultado.area_ha > 0
    assert resultado.perimeter_m > 0


def test_val01_poucos_pontos(config):
    resultado = validar(CERCA_LIMPA[:2], config)

    assert not resultado.valid
    assert regras(resultado, SEVERITY_ERROR) == {"VAL-01"}
    assert "de 3 a 32 pontos" in resultado.errors[0].message
    assert "Agora tem 2" in resultado.errors[0].message


def test_val01_pontos_demais(config):
    muitos = [GeoPoint(-31.3058 - i * 0.00001, -54.0646) for i in range(33)]

    resultado = validar(muitos, config)

    assert regras(resultado, SEVERITY_ERROR) == {"VAL-01"}


def test_val01_para_antes_de_calcular_geometria(config):
    """Sem quantidade válida de pontos não há área nem sentido.

    E o Central NÃO despeja erros derivados: o produtor recebe uma
    mensagem só, a que resolve o problema.
    """
    resultado = validar(CERCA_LIMPA[:2], config)

    assert resultado.area_ha == 0.0
    assert resultado.perimeter_m == 0.0
    assert resultado.orientation is None
    assert len(resultado.violations) == 1


def test_val02_coordenada_impossivel(config):
    invalida = [GeoPoint(91.0, -54.0646), *CERCA_LIMPA[1:]]

    resultado = validar(invalida, config)

    assert regras(resultado, SEVERITY_ERROR) == {"VAL-02"}
    assert resultado.errors[0].points == [1]
    assert "ponto 1" in resultado.errors[0].message


def test_val02_aponta_todos_os_pontos_errados(config):
    invalida = [GeoPoint(91.0, -54.0646), GeoPoint(-31.3058, 181.0), *CERCA_LIMPA[2:]]

    resultado = validar(invalida, config)

    assert resultado.errors[0].points == [1, 2]
    assert "pontos 1, 2" in resultado.errors[0].message


def test_val03_ponto_longe_da_base(config):
    """A regra que pega latitude e longitude trocadas."""
    longe = [GeoPoint(-54.0646, -31.3058), *CERCA_LIMPA[1:]]

    resultado = validar(longe, config)

    assert "VAL-03" in regras(resultado, SEVERITY_ERROR)
    mensagem = next(v for v in resultado.errors if v.rule == "VAL-03").message
    assert "km da Base" in mensagem
    assert "latitude e longitude não estão trocadas" in mensagem


def test_val04_pontos_praticamente_no_mesmo_lugar(config):
    # 0,000002 grau nos dois eixos ≈ 0,29 m, abaixo do mínimo de 1 m.
    colados = [
        CERCA_LIMPA[0],
        GeoPoint(-31.305802, -54.064598),
        *CERCA_LIMPA[1:],
    ]

    resultado = validar(colados, config)

    assert "VAL-04" in regras(resultado, SEVERITY_ERROR)
    violacao = next(v for v in resultado.errors if v.rule == "VAL-04")
    assert violacao.edges == [[1, 2]]
    assert "Apague um deles" in violacao.message


def test_val04_inclui_o_lado_que_fecha_de_pn_para_p1(config):
    """O lado pn → p1 é o mais fácil de esquecer na implementação."""
    colados = [*CERCA_LIMPA, GeoPoint(-31.305802, -54.064598)]

    resultado = validar(colados, config)

    violacao = next(v for v in resultado.errors if v.rule == "VAL-04")
    assert violacao.edges == [[5, 1]]


def test_val05_lados_cruzados(config):
    cruzada = [CERCA_LIMPA[0], CERCA_LIMPA[2], CERCA_LIMPA[1], CERCA_LIMPA[3]]

    resultado = validar(cruzada, config)

    assert "VAL-05" in regras(resultado, SEVERITY_ERROR)
    violacao = next(v for v in resultado.errors if v.rule == "VAL-05")
    assert violacao.edges == [[1, 3]]
    assert "se cruzam" in violacao.message


def test_val05_suprime_val06_porque_o_sentido_fica_indefinido(config):
    """Uma cerca com lados cruzados tem área com sinal zero.

    Sem esta supressão, o produtor receberia também um VAL-06 dizendo
    "os pontos estão em linha reta" — mensagem errada para o problema.
    """
    cruzada = [CERCA_LIMPA[0], CERCA_LIMPA[2], CERCA_LIMPA[1], CERCA_LIMPA[3]]

    resultado = validar(cruzada, config)

    assert regras(resultado, SEVERITY_ERROR) == {"VAL-05"}


def test_val06_sentido_anti_horario(config):
    resultado = validar(list(reversed(CERCA_LIMPA)), config)

    assert regras(resultado, SEVERITY_ERROR) == {"VAL-06"}
    assert resultado.orientation == "counterclockwise"
    assert "sentido anti-horário" in resultado.errors[0].message
    assert "Inverter ordem" in resultado.errors[0].message


def test_val06_nao_normaliza_sozinho(config):
    """DEC-15: cerca anti-horária é RECUSADA, não corrigida em silêncio.

    A inversão automática mudaria a ordem dos pontos e, com ela, o CRC —
    e o produtor nunca saberia que o sistema desenhou outra coisa.
    """
    resultado = validar(list(reversed(CERCA_LIMPA)), config)

    assert resultado.valid is False


def test_val06_pontos_em_linha_reta(config):
    reta = [
        GeoPoint(-31.3058, -54.0646),
        GeoPoint(-31.3059, -54.0646),
        GeoPoint(-31.3060, -54.0646),
    ]

    resultado = validar(reta, config)

    assert "VAL-06" in regras(resultado, SEVERITY_ERROR)
    assert "em linha reta" in resultado.errors[0].message


def test_val07_lado_menor_que_o_erro_do_gps(config):
    """Aviso: um lado de 2 m com GPS que erra 3 m faz a coleira oscilar
    de zona sem o animal se mover."""
    # Lado de ~2,2 m entre os pontos 1 e 2.
    curta = [
        GeoPoint(-31.305800, -54.064600),
        GeoPoint(-31.305800, -54.064577),
        GeoPoint(-31.306600, -54.063300),
        GeoPoint(-31.306600, -54.064600),
    ]

    resultado = validar(curta, config)

    assert "VAL-07" in regras(resultado, SEVERITY_WARNING)
    violacao = next(v for v in resultado.warnings if v.rule == "VAL-07")
    assert violacao.edges == [[1, 2]]
    assert "erro do GPS" in violacao.message


def test_val07_nao_avisa_duas_vezes_sobre_um_lado_que_ja_e_erro(config):
    """Lado abaixo de 1 m já é erro em VAL-04. Avisar em VAL-07 também
    seria dizer duas coisas sobre o mesmo problema."""
    colados = [
        CERCA_LIMPA[0],
        GeoPoint(-31.305802, -54.064598),
        *CERCA_LIMPA[1:],
    ]

    resultado = validar(colados, config)

    assert "VAL-04" in regras(resultado, SEVERITY_ERROR)
    assert "VAL-07" not in regras(resultado, SEVERITY_WARNING)


def test_val08_margem_critica_maior_que_a_de_atencao(config):
    resultado = validar(CERCA_LIMPA, config, atencao=2.0, critica=5.0)

    assert "VAL-08" in regras(resultado, SEVERITY_ERROR)
    assert "margem crítica" in resultado.errors[0].message


def test_val08_margem_critica_zero(config):
    resultado = validar(CERCA_LIMPA, config, atencao=5.0, critica=0.0)

    assert "VAL-08" in regras(resultado, SEVERITY_ERROR)


def test_val08_e_verificada_mesmo_com_cerca_invalida(config):
    """As margens não dependem da geometria, então são conferidas antes
    do corte por VAL-01: o produtor vê os dois problemas de uma vez."""
    resultado = validar(CERCA_LIMPA[:2], config, atencao=2.0, critica=5.0)

    assert regras(resultado, SEVERITY_ERROR) == {"VAL-01", "VAL-08"}


def test_val09_cerca_pequena_para_a_margem(config):
    pequena = [
        GeoPoint(-31.306200, -54.063950),
        GeoPoint(-31.306200, -54.063927),
        GeoPoint(-31.306220, -54.063939),
    ]

    resultado = validar(pequena, config)

    assert "VAL-09" in regras(resultado, SEVERITY_WARNING)
    violacao = next(v for v in resultado.warnings if v.rule == "VAL-09")
    assert "pequena para a margem" in violacao.message


def test_val09_criterio_isolado_pode_ser_recalibrado(config):
    """A especificação marca o critério da VAL-09 como "a confirmar".

    Por isso ele vive em `val09_is_small_for_margin()`, testável sozinho:
    quando os dados GNSS da IP1 forem calibrados, muda uma função só.
    """
    pontos = CERCA_LIMPA
    plano = LocalProjection.centered_on(pontos).project_all(pontos)

    # Cerca de ~124 m × 89 m: folgada para 8 m, apertada para 200 m.
    assert val09_is_small_for_margin(plano, margin_attention_m=8.0) is False
    assert val09_is_small_for_margin(plano, margin_attention_m=200.0) is True


def test_val10_ponto_fora_do_alcance_do_radio(config):
    """Aviso, não erro: o geofencing funciona sem rádio, porque a cerca
    já está gravada na coleira."""
    pontos = [GeoPoint(p["lat"], p["lon"]) for p in _caso("ponto_fora_do_alcance_lora")["points"]]

    resultado = validar(pontos, config)

    assert resultado.valid is True
    assert "VAL-10" in regras(resultado, SEVERITY_WARNING)
    violacao = next(v for v in resultado.warnings if v.rule == "VAL-10")
    assert "alcance testado do rádio" in violacao.message
    assert "A cerca funciona" in violacao.message


def test_val12_margem_critica_abaixo_da_folga_recomendada(config):
    """Com os valores provisórios, a folga é 1,5 × 3,0 = 4,5 m.

    A margem crítica PADRÃO de 2 m já dispara este aviso, o que é
    esperado e coerente com a seção 5.4 da especificação.
    """
    resultado = validar(CERCA_LIMPA, config, atencao=5.0, critica=2.0)

    assert resultado.valid is True  # é aviso, não erro
    assert "VAL-12" in regras(resultado, SEVERITY_WARNING)
    violacao = next(v for v in resultado.warnings if v.rule == "VAL-12")
    assert "4,5 m" in violacao.message
    assert "pode enviar mesmo assim" in violacao.message


def test_val12_nao_avisa_com_margem_folgada(config):
    resultado = validar(CERCA_LIMPA, config, atencao=8.0, critica=4.5)

    assert "VAL-12" not in regras(resultado, SEVERITY_WARNING)


def test_val11_nao_e_implementada_no_central(config):
    """VAL-11 é a conferência do CRC DENTRO da coleira, feita pelo
    firmware. O Central só compara o CRC devolvido, o que acontece em
    `delivery.py`, não aqui."""
    resultado = validar(CERCA_LIMPA, config)

    assert "VAL-11" not in {v.rule for v in resultado.violations}


# ===========================================================================
# 2. Aviso permite salvar, erro não
# ===========================================================================


def test_aviso_nao_invalida_a_cerca(config):
    resultado = validar(CERCA_LIMPA, config, atencao=5.0, critica=2.0)

    assert resultado.warnings
    assert not resultado.errors
    assert resultado.valid is True


def test_erro_invalida_a_cerca(config):
    resultado = validar(list(reversed(CERCA_LIMPA)), config)

    assert resultado.errors
    assert resultado.valid is False


def test_formato_da_resposta_segue_a_secao_5_4(config):
    resultado = validar(CERCA_LIMPA, config, atencao=5.0, critica=2.0)

    corpo = resultado.as_dict()

    assert set(corpo) == {"valid", "violations", "area_ha", "perimeter_m", "orientation"}
    assert corpo["orientation"] == "clockwise"
    for violacao in corpo["violations"]:
        assert set(violacao) == {"rule", "severity", "message", "points", "edges"}
        assert violacao["severity"] in {"error", "warning"}


def test_mensagens_usam_virgula_decimal(config):
    """Textos da tela são para o produtor: "1,5 m", não "1.5 m"."""
    resultado = validar(CERCA_LIMPA, config, atencao=5.0, critica=2.0)

    mensagem = next(v for v in resultado.warnings if v.rule == "VAL-12").message

    assert "4,5" in mensagem
    assert "4.5" not in mensagem


# ===========================================================================
# 3. Os casos compartilhados, integralmente
# ===========================================================================


def _caso(nome: str) -> dict:
    return next(c for c in CASOS["casos"] if c["nome"] == nome)


CASOS_COM_REGRAS = [c for c in CASOS["casos"] if "errors" in c["expected"]]


@pytest.mark.parametrize("caso", CASOS_COM_REGRAS, ids=lambda c: c["nome"])
def test_casos_geometricos_compartilhados(caso, config):
    """Roda cada caso do `casos_geometricos.json` e confere o conjunto
    EXATO de erros e avisos.

    Este é o teste que o `frontend/js/geometry.js` (fase F5) e o firmware
    da coleira precisam reproduzir. Se as três implementações
    concordarem aqui, a geometria está consistente nas três pontas.
    """
    pontos = [GeoPoint(p["lat"], p["lon"]) for p in caso["points"]]
    esperado = caso["expected"]

    resultado = validate_fence(
        pontos, caso["margin_attention_m"], caso["margin_critical_m"], config
    )

    assert regras(resultado, SEVERITY_ERROR) == set(esperado["errors"])
    assert regras(resultado, SEVERITY_WARNING) == set(esperado["warnings"])
    assert resultado.valid is esperado["valid"]

    if "orientation" in esperado:
        assert resultado.orientation == esperado["orientation"]
    if "area_ha" in esperado:
        assert resultado.area_ha == pytest.approx(esperado["area_ha"], rel=0.01, abs=1e-9)
    if "perimeter_m" in esperado:
        assert resultado.perimeter_m == pytest.approx(esperado["perimeter_m"], rel=0.01, abs=1e-9)
    if "edges" in esperado:
        encontradas = [e for v in resultado.violations for e in v.edges]
        for esperada in esperado["edges"]:
            assert esperada in encontradas
    if "points" in esperado:
        encontrados = [p for v in resultado.violations for p in v.points]
        for esperado_ponto in esperado["points"]:
            assert esperado_ponto in encontrados
