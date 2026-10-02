"""Testes da geometria (fase F2).

A conferência mais importante está em `test_quadrado_de_conferencia`: é a
exigida pela seção 5.3 da especificação e é o teste que o firmware da
coleira também precisa passar.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from backend.services import geometry
from backend.services.geometry import GeoPoint, LocalProjection, PlanePoint

CASOS = json.loads(
    (Path(__file__).parent / "casos_geometricos.json").read_text(encoding="utf-8")
)


def caso(nome: str) -> dict:
    return next(c for c in CASOS["casos"] if c["nome"] == nome)


def pontos_de(nome: str) -> list[GeoPoint]:
    return [GeoPoint(p["lat"], p["lon"]) for p in caso(nome)["points"]]


def plano_de(nome: str) -> list[PlanePoint]:
    pontos = pontos_de(nome)
    return LocalProjection.centered_on(pontos).project_all(pontos)


# ---------------------------------------------------------------------------
# A conferência obrigatória da especificação
# ---------------------------------------------------------------------------


def test_quadrado_de_conferencia():
    """Seção 5.3: o quadrado (0,0)→(0,1)→(1,1)→(1,0) é horário e dá A = −1.

    Este teste não passa por projeção nenhuma: usa coordenadas de plano
    direto, para conferir a fórmula do laço isoladamente. É o mesmo caso
    que o firmware precisa reproduzir.
    """
    esperado = caso("quadrado_de_conferencia")["expected"]
    quadrado = [PlanePoint(*par) for par in caso("quadrado_de_conferencia")["plane_points"]]

    assert geometry.signed_area_m2(quadrado) == pytest.approx(esperado["signed_area"])
    assert geometry.signed_area_m2(quadrado) == pytest.approx(-1.0)
    assert geometry.orientation(quadrado) == "clockwise"
    assert geometry.area_m2(quadrado) == pytest.approx(1.0)
    assert geometry.perimeter_m(quadrado) == pytest.approx(esperado["perimeter_m"])


def test_a_forma_equivalente_do_planejamento_concorda():
    """O planejamento (5.3) cita `S = Σ (xᵢ₊₁ − xᵢ)·(yᵢ₊₁ + yᵢ)`, com S > 0
    indicando horário. Vale sempre `S = −2A`, então os dois concordam.

    Conferir isso aqui evita alguém implementar uma fórmula no navegador
    e a outra no servidor e achar que há divergência de regra.
    """
    quadrado = [PlanePoint(0, 0), PlanePoint(0, 1), PlanePoint(1, 1), PlanePoint(1, 0)]

    soma = 0.0
    for i in range(len(quadrado)):
        atual, seguinte = quadrado[i], quadrado[(i + 1) % len(quadrado)]
        soma += (seguinte.x - atual.x) * (seguinte.y + atual.y)

    assert soma == pytest.approx(2.0)
    assert soma == pytest.approx(-2 * geometry.signed_area_m2(quadrado))


# ---------------------------------------------------------------------------
# Projeção
# ---------------------------------------------------------------------------


def test_projecao_coloca_a_referencia_na_origem():
    projecao = LocalProjection(-31.3062, -54.06395)

    origem = projecao.project(GeoPoint(-31.3062, -54.06395))

    assert origem.x == pytest.approx(0.0)
    assert origem.y == pytest.approx(0.0)


def test_eixo_x_aponta_para_leste_e_y_para_o_norte():
    """Convenção da seção 5.3. Errar o sinal aqui inverteria o sentido de
    TODAS as cercas e faria o VAL-06 recusar as corretas."""
    projecao = LocalProjection(-31.3062, -54.06395)

    leste = projecao.project(GeoPoint(-31.3062, -54.0630))
    norte = projecao.project(GeoPoint(-31.3050, -54.06395))

    assert leste.x > 0 and leste.y == pytest.approx(0.0)
    assert norte.y > 0 and norte.x == pytest.approx(0.0)


def test_um_grau_de_longitude_vale_menos_que_um_de_latitude_nesta_latitude():
    """É exatamente por isso que não dá para medir em graus.

    Em Bagé (≈31° sul), um grau de longitude vale cerca de 85% de um de
    latitude, porque os meridianos se aproximam em direção ao polo.
    """
    projecao = LocalProjection(-31.3062, -54.06395)

    um_grau_lon = projecao.project(GeoPoint(-31.3062, -53.06395)).x
    um_grau_lat = projecao.project(GeoPoint(-30.3062, -54.06395)).y

    assert um_grau_lon / um_grau_lat == pytest.approx(math.cos(math.radians(-31.3062)), rel=1e-6)
    assert 0.84 < um_grau_lon / um_grau_lat < 0.86


def test_haversine_concorda_com_a_projecao_local_em_distancia_curta():
    """As duas formas de medir precisam dar quase o mesmo em distância
    curta. Divergência aqui indicaria erro em uma delas."""
    base = GeoPoint(-31.306200, -54.063950)
    perto = GeoPoint(-31.306000, -54.063700)

    pela_esfera = geometry.haversine_m(base, perto)
    plano = LocalProjection.centered_on([base, perto]).project_all([base, perto])
    pelo_plano = math.dist(plano[0], plano[1])

    assert pela_esfera == pytest.approx(pelo_plano, rel=1e-4)


def test_haversine_pega_a_troca_de_latitude_por_longitude():
    """O ponto trocado cai no oceano Índico, a milhares de quilômetros."""
    base = GeoPoint(-31.306200, -54.063950)
    trocado = GeoPoint(-54.063950, -31.306200)

    assert geometry.haversine_m(base, trocado) > 2_000_000


# ---------------------------------------------------------------------------
# Área, perímetro e sentido em coordenadas reais
# ---------------------------------------------------------------------------


def test_area_e_perimetro_da_cerca_de_exemplo():
    """Valores da seção 5.5: ≈ 0,211 ha e ≈ 184,0 m.

    A tolerância de 1% não é folga: é a diferença esperada entre a esfera
    que o Central usa e o elipsoide WGS84 do firmware.
    """
    esperado = caso("cerca_de_exemplo")["expected"]
    plano = plano_de("cerca_de_exemplo")

    assert geometry.area_ha(plano) == pytest.approx(esperado["area_ha"], rel=0.01)
    assert geometry.perimeter_m(plano) == pytest.approx(esperado["perimeter_m"], rel=0.01)
    assert geometry.orientation(plano) == "clockwise"


def test_inverter_a_ordem_inverte_so_o_sinal_da_area():
    """Área e perímetro não mudam; o SENTIDO muda. É nisso que VAL-06 se apoia."""
    normal = plano_de("cerca_de_exemplo")
    invertida = plano_de("cerca_de_exemplo_invertida")

    assert geometry.orientation(normal) == "clockwise"
    assert geometry.orientation(invertida) == "counterclockwise"
    assert geometry.signed_area_m2(normal) == pytest.approx(
        -geometry.signed_area_m2(invertida), rel=1e-6
    )
    assert geometry.area_m2(normal) == pytest.approx(geometry.area_m2(invertida), rel=1e-6)


def test_perimetro_inclui_o_lado_que_fecha():
    """O último ponto liga ao primeiro sem p1 ser repetido na lista.

    Esquecer esse lado é um erro clássico: o perímetro sairia 25% menor
    num quadrado, e o produtor veria um número errado na tela.
    """
    quadrado = [PlanePoint(0, 0), PlanePoint(0, 10), PlanePoint(10, 10), PlanePoint(10, 0)]

    assert geometry.perimeter_m(quadrado) == pytest.approx(40.0)
    assert len(geometry.side_lengths_m(quadrado)) == 4


def test_pontos_em_linha_reta_nao_tem_sentido():
    """Três pontos colineares não formam área: o sentido é indefinido."""
    reta = [PlanePoint(0, 0), PlanePoint(1, 1), PlanePoint(2, 2)]

    assert geometry.orientation(reta) is None
    assert geometry.area_m2(reta) == pytest.approx(0.0)


def test_menos_de_tres_pontos_nao_tem_area_nem_sentido():
    assert geometry.orientation([PlanePoint(0, 0), PlanePoint(1, 1)]) is None
    assert geometry.area_m2([PlanePoint(0, 0), PlanePoint(1, 1)]) == 0.0
    assert geometry.signed_area_m2([]) == 0.0


# ---------------------------------------------------------------------------
# Lados que se cruzam
# ---------------------------------------------------------------------------


def test_segmentos_que_se_cruzam_de_verdade():
    a1, a2 = PlanePoint(0, 0), PlanePoint(10, 10)
    b1, b2 = PlanePoint(0, 10), PlanePoint(10, 0)

    assert geometry.segments_intersect(a1, a2, b1, b2)


def test_segmentos_paralelos_nao_se_cruzam():
    assert not geometry.segments_intersect(
        PlanePoint(0, 0), PlanePoint(10, 0), PlanePoint(0, 5), PlanePoint(10, 5)
    )


def test_segmentos_colineares_sobrepostos_contam_como_cruzamento():
    """O caso que o teste de orientação sozinho não pega.

    Quando os quatro pontos estão em linha reta, todos os produtos
    vetoriais dão zero e o teste de separação não decide nada. São dois
    lados da cerca deitados um sobre o outro — tão inválido quanto um
    cruzamento em X.
    """
    assert geometry.segments_intersect(
        PlanePoint(0, 0), PlanePoint(10, 0), PlanePoint(5, 0), PlanePoint(15, 0)
    )


def test_segmentos_colineares_separados_nao_se_cruzam():
    assert not geometry.segments_intersect(
        PlanePoint(0, 0), PlanePoint(10, 0), PlanePoint(20, 0), PlanePoint(30, 0)
    )


def test_ponta_tocando_o_outro_segmento_conta():
    assert geometry.segments_intersect(
        PlanePoint(0, 0), PlanePoint(10, 0), PlanePoint(5, 0), PlanePoint(5, 10)
    )


def test_gravata_borboleta_tem_os_lados_1_e_3_cruzados():
    cruzamentos = geometry.self_intersections(plano_de("gravata_borboleta"))

    assert cruzamentos == [(1, 3)]


def test_cerca_normal_nao_tem_cruzamento():
    assert geometry.self_intersections(plano_de("cerca_de_exemplo")) == []
    assert geometry.self_intersections(plano_de("cerca_boa_sem_nenhum_aviso")) == []


def test_lados_vizinhos_nao_contam_como_cruzamento():
    """Lados vizinhos compartilham um ponto de propósito. Se contassem,
    TODA cerca seria recusada."""
    quadrado = [PlanePoint(0, 0), PlanePoint(0, 10), PlanePoint(10, 10), PlanePoint(10, 0)]

    assert geometry.self_intersections(quadrado) == []


def test_triangulo_nunca_tem_cruzamento():
    """Com 3 lados, todos são vizinhos entre si: não há par a comparar."""
    triangulo = [PlanePoint(0, 0), PlanePoint(5, 10), PlanePoint(10, 0)]

    assert geometry.self_intersections(triangulo) == []


def test_quantidade_de_comparacoes_com_32_pontos():
    """A especificação afirma "no máximo 464 pares". Conferindo a conta:
    C(32,2) = 496 pares, menos 32 vizinhos = 464."""
    quantidade = 32
    pares = quantidade * (quantidade - 1) // 2

    assert pares - quantidade == 464


# ---------------------------------------------------------------------------
# Distância ponto → lado
# ---------------------------------------------------------------------------


def test_distancia_a_um_lado_perpendicular():
    assert geometry.point_to_segment_m(
        PlanePoint(5, 3), PlanePoint(0, 0), PlanePoint(10, 0)
    ) == pytest.approx(3.0)


def test_distancia_quando_a_projecao_cai_fora_do_lado():
    """Mede até a PONTA mais próxima, não até a reta infinita.

    Errar isso faria a regra VAL-09 achar que uma cerca comprida e fina
    é larga, porque a reta de um lado passa longe.
    """
    assert geometry.point_to_segment_m(
        PlanePoint(20, 0), PlanePoint(0, 0), PlanePoint(10, 0)
    ) == pytest.approx(10.0)


def test_distancia_a_um_lado_degenerado():
    """Lado com as duas pontas no mesmo lugar não trava a conta."""
    assert geometry.point_to_segment_m(
        PlanePoint(3, 4), PlanePoint(0, 0), PlanePoint(0, 0)
    ) == pytest.approx(5.0)


def test_distancias_aos_lados_nao_adjacentes_de_um_quadrado():
    """Num quadrado de 10 m, cada canto está a 10 m dos dois lados opostos."""
    quadrado = [PlanePoint(0, 0), PlanePoint(0, 10), PlanePoint(10, 10), PlanePoint(10, 0)]

    distancias = geometry.distances_to_non_adjacent_sides_m(quadrado, 0)

    assert len(distancias) == 2  # 4 lados menos os 2 que tocam o ponto
    assert sorted(distancias) == pytest.approx([10.0, 10.0])
