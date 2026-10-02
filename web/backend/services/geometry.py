"""Geometria da cerca: metros, área, perímetro, sentido e cruzamentos.

O problema de medir em graus
----------------------------
Graus de latitude e de longitude não têm o mesmo tamanho em metros, e a
proporção muda com a latitude. Em Bagé (≈31° sul), um grau de longitude
vale cerca de 85% de um grau de latitude. Então calcular distância ou
área direto em graus dá resultado errado.

A solução: projeção local (seção 5.3 da especificação)
------------------------------------------------------
Escolhemos um ponto de referência (a média dos pontos da cerca) e
transformamos tudo em um plano em metros em volta dele:

    R = 6371008.8                        raio médio da Terra
    x = rad(lon − lon0) · R · cos(rad(lat0))      leste, em metros
    y = rad(lat − lat0) · R                       norte, em metros

É uma aproximação: trata a Terra como esfera e o pedaço de mundo como
plano. Para cercas de até alguns quilômetros o erro é de centímetros,
muito abaixo dos 3 m de erro do GPS. Para o tamanho de um piquete, é
mais que suficiente.

Atenção para a fase F7: o firmware da coleira usa o elipsoide WGS84, não
esfera. A diferença chega a ~1 m em 200 m, então os casos geométricos
compartilhados precisam comparar com tolerância.

O sentido, e por que ele é obrigatório
--------------------------------------
A fórmula do laço (*shoelace*) calcula a área COM SINAL de um polígono.
O sinal diz o sentido do desenho:

    A = ½ · Σ (xᵢ·yᵢ₊₁ − xᵢ₊₁·yᵢ)      com p_(n+1) = p₁

    A < 0 → horário        (aceito)
    A > 0 → anti-horário   (recusado pela regra VAL-06)
    área  = |A|

Forçar um sentido único (DEC-15) tem dois motivos práticos: o CRC fica
sempre igual para a mesma cerca, sem ambiguidade; e a distância COM
SINAL até a borda — que a coleira usa para decidir as margens de alerta —
depende da orientação para saber o que é dentro e o que é fora.

Conferência obrigatória: o quadrado (0,0) → (0,1) → (1,1) → (1,0) é
horário e dá A = −1. Está no `test_geometry.py`.

Onde se conecta
---------------
`validation.py` usa tudo daqui para decidir as regras VAL-03 a VAL-10.
O arquivo `frontend/js/geometry.js` (fase F5) é o espelho deste módulo no
navegador, para o produtor ver o mesmo resultado antes de enviar.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

# Raio médio da Terra em metros (IUGG), o mesmo valor da especificação.
EARTH_RADIUS_M = 6371008.8

# Tolerância para comparações com zero. Trabalhamos em metros, então
# 1e-9 é um nanômetro: qualquer coisa menor é ruído de ponto flutuante.
EPSILON = 1e-9


class GeoPoint(NamedTuple):
    """Ponto em graus. Sempre `lon`, nunca `long`."""

    lat: float
    lon: float


class PlanePoint(NamedTuple):
    """Ponto no plano local, em metros. `x` para leste, `y` para norte."""

    x: float
    y: float


# ---------------------------------------------------------------------------
# Projeção
# ---------------------------------------------------------------------------


class LocalProjection:
    """Converte graus em metros em volta de um ponto de referência.

    A referência é guardada no objeto para que a MESMA projeção seja
    usada em todos os cálculos de uma cerca. Projetar pontos diferentes
    com referências diferentes daria distâncias inconsistentes.
    """

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0 = lat0
        self.lon0 = lon0
        # Quanto um radiano de longitude vale em metros nesta latitude.
        self._lon_scale = EARTH_RADIUS_M * math.cos(math.radians(lat0))

    @classmethod
    def centered_on(cls, points: Sequence[GeoPoint]) -> LocalProjection:
        """Cria a projeção centrada na média dos pontos (seção 5.3)."""
        if not points:
            raise ValueError("é preciso pelo menos um ponto para criar a projeção")
        lat0 = sum(point.lat for point in points) / len(points)
        lon0 = sum(point.lon for point in points) / len(points)
        return cls(lat0, lon0)

    def project(self, point: GeoPoint) -> PlanePoint:
        """Converte um ponto em graus para metros no plano local."""
        x = math.radians(point.lon - self.lon0) * self._lon_scale
        y = math.radians(point.lat - self.lat0) * EARTH_RADIUS_M
        return PlanePoint(x, y)

    def project_all(self, points: Sequence[GeoPoint]) -> list[PlanePoint]:
        return [self.project(point) for point in points]


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Distância em metros entre dois pontos quaisquer da Terra.

    Usada onde a projeção local não serve: a distância de um ponto da
    cerca até a Base (regras VAL-03 e VAL-10). A projeção local é
    centrada na cerca, então ela perde precisão para pontos distantes —
    e VAL-03 justamente procura pontos distantes, como latitude e
    longitude trocadas, que jogam o ponto a milhares de quilômetros.
    """
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(inner)))


# ---------------------------------------------------------------------------
# Área, perímetro e sentido
# ---------------------------------------------------------------------------


def signed_area_m2(plane: Sequence[PlanePoint]) -> float:
    """Área com sinal pela fórmula do laço. Negativo = horário.

    O polígono é fechado implicitamente: o último ponto liga ao primeiro
    sem `p1` ser repetido na lista.
    """
    quantity = len(plane)
    if quantity < 3:
        return 0.0
    total = 0.0
    for index in range(quantity):
        current = plane[index]
        following = plane[(index + 1) % quantity]
        total += current.x * following.y - following.x * current.y
    return total / 2


def area_m2(plane: Sequence[PlanePoint]) -> float:
    """Área em metros quadrados, sempre positiva."""
    return abs(signed_area_m2(plane))


def area_ha(plane: Sequence[PlanePoint]) -> float:
    """Área em hectares — a unidade que o produtor usa. 1 ha = 10.000 m²."""
    return area_m2(plane) / 10_000


def perimeter_m(plane: Sequence[PlanePoint]) -> float:
    """Soma dos lados, incluindo o lado que fecha de pn para p1."""
    quantity = len(plane)
    if quantity < 2:
        return 0.0
    total = 0.0
    for index in range(quantity):
        current = plane[index]
        following = plane[(index + 1) % quantity]
        total += math.dist(current, following)
    return total


def orientation(plane: Sequence[PlanePoint]) -> str | None:
    """Devolve "clockwise", "counterclockwise", ou None se não der para dizer.

    None acontece com menos de 3 pontos ou com todos os pontos em linha
    reta (área zero): nos dois casos não existe sentido de giro.
    """
    if len(plane) < 3:
        return None
    signed = signed_area_m2(plane)
    if abs(signed) <= EPSILON:
        return None
    return "clockwise" if signed < 0 else "counterclockwise"


def side_lengths_m(plane: Sequence[PlanePoint]) -> list[float]:
    """Comprimento de cada lado, na ordem. O último é o lado pn → p1."""
    quantity = len(plane)
    if quantity < 2:
        return []
    return [
        math.dist(plane[index], plane[(index + 1) % quantity])
        for index in range(quantity)
    ]


# ---------------------------------------------------------------------------
# Lados que se cruzam
# ---------------------------------------------------------------------------


def _cross(origin: PlanePoint, a: PlanePoint, b: PlanePoint) -> float:
    """Produto vetorial dos vetores origin→a e origin→b.

    O SINAL do resultado diz para que lado a curva vira:
      > 0  anti-horário (b está à esquerda de origin→a)
      < 0  horário
      = 0  os três pontos estão em linha reta
    """
    return (a.x - origin.x) * (b.y - origin.y) - (a.y - origin.y) * (b.x - origin.x)


def _sign(value: float) -> int:
    if value > EPSILON:
        return 1
    if value < -EPSILON:
        return -1
    return 0


def _on_segment(point: PlanePoint, start: PlanePoint, end: PlanePoint) -> bool:
    """O ponto está dentro do retângulo que envolve o segmento?

    Só é chamada quando já se sabe que os três pontos são colineares;
    aí esta conferência basta para saber se o ponto está no segmento.
    """
    return (
        min(start.x, end.x) - EPSILON <= point.x <= max(start.x, end.x) + EPSILON
        and min(start.y, end.y) - EPSILON <= point.y <= max(start.y, end.y) + EPSILON
    )


def segments_intersect(
    a1: PlanePoint, a2: PlanePoint, b1: PlanePoint, b2: PlanePoint
) -> bool:
    """Os segmentos a1→a2 e b1→b2 se cruzam ou se sobrepõem?

    Teste de orientação clássico: dois segmentos se cruzam quando cada um
    separa as pontas do outro. O caso colinear (os quatro pontos em linha
    reta) é tratado à parte, porque aí todos os produtos vetoriais dão
    zero e o teste de separação não decide nada — é o caso de dois lados
    da cerca deitados um sobre o outro.
    """
    d1 = _sign(_cross(a1, a2, b1))
    d2 = _sign(_cross(a1, a2, b2))
    d3 = _sign(_cross(b1, b2, a1))
    d4 = _sign(_cross(b1, b2, a2))

    # Cruzamento "limpo": cada segmento separa as pontas do outro.
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True

    # Casos colineares ou de ponta tocando o outro segmento.
    if d1 == 0 and _on_segment(b1, a1, a2):
        return True
    if d2 == 0 and _on_segment(b2, a1, a2):
        return True
    if d3 == 0 and _on_segment(a1, b1, b2):
        return True
    if d4 == 0 and _on_segment(a2, b1, b2):
        return True
    return False


def self_intersections(plane: Sequence[PlanePoint]) -> list[tuple[int, int]]:
    """Lista os pares de lados que se cruzam, numerados a partir de 1.

    Só compara lados NÃO adjacentes: lados vizinhos compartilham um
    ponto de propósito, e isso não é cruzamento. O lado `i` vai do ponto
    `i` ao ponto `i+1`; o último lado fecha de `n` para `1`.

    Com 32 pontos são 496 pares possíveis menos 32 vizinhos = 464
    comparações, custo desprezível.
    """
    quantity = len(plane)
    if quantity < 4:
        # Com 3 lados, todos são vizinhos entre si: não há o que comparar.
        return []

    crossing: list[tuple[int, int]] = []
    for i in range(quantity):
        for j in range(i + 1, quantity):
            adjacent = (j == i + 1) or (i == 0 and j == quantity - 1)
            if adjacent:
                continue
            if segments_intersect(
                plane[i], plane[(i + 1) % quantity],
                plane[j], plane[(j + 1) % quantity],
            ):
                crossing.append((i + 1, j + 1))
    return crossing


# ---------------------------------------------------------------------------
# Distância ponto → lado
# ---------------------------------------------------------------------------


def point_to_segment_m(point: PlanePoint, start: PlanePoint, end: PlanePoint) -> float:
    """Menor distância de um ponto a um segmento (não à reta infinita).

    Projeta o ponto sobre o segmento e limita o resultado ao trecho
    entre as duas pontas: se a projeção cai fora, a menor distância é
    até a ponta mais próxima.
    """
    segment_x = end.x - start.x
    segment_y = end.y - start.y
    length_squared = segment_x * segment_x + segment_y * segment_y
    if length_squared <= EPSILON:
        # Segmento degenerado (duas pontas no mesmo lugar).
        return math.dist(point, start)
    position = ((point.x - start.x) * segment_x + (point.y - start.y) * segment_y) / length_squared
    position = max(0.0, min(1.0, position))
    closest = PlanePoint(start.x + position * segment_x, start.y + position * segment_y)
    return math.dist(point, closest)


def distances_to_non_adjacent_sides_m(
    plane: Sequence[PlanePoint], vertex_index: int
) -> list[float]:
    """Distância de um ponto a cada lado que NÃO o contém.

    Os dois lados que tocam o ponto têm distância zero por definição e
    não dizem nada sobre a largura da cerca. O que interessa é a
    distância até o "outro lado" da área — é isso que a regra VAL-09 usa
    para perceber que uma cerca é estreita demais para as margens.
    """
    quantity = len(plane)
    point = plane[vertex_index]
    previous_side = (vertex_index - 1) % quantity
    distances: list[float] = []
    for side in range(quantity):
        if side in (vertex_index, previous_side):
            continue
        distances.append(
            point_to_segment_m(point, plane[side], plane[(side + 1) % quantity])
        )
    return distances
