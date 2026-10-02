"""As regras VAL-01 a VAL-12: o que é uma cerca aceitável.

Duas severidades, dois comportamentos
-------------------------------------
- **erro** impede salvar. A cerca está errada e não há como enviar.
- **aviso** permite salvar, mas o produtor precisa marcar "Estou ciente
  dos avisos" na revisão. A cerca funciona; só pode se comportar de modo
  inesperado por causa do erro do GPS ou do alcance do rádio.

Essa distinção é de projeto, não de implementação: margem menor que o
erro do GPS não é um erro de desenho, é uma escolha com consequência. O
sistema avisa e deixa a decisão com quem conhece o terreno.

As mesmas regras rodam nos dois lados
-------------------------------------
Este módulo é a decisão FINAL, no servidor. O `frontend/js/geometry.js`
(fase F5) repete as mesmas contas no navegador, para o produtor ver o
problema enquanto desenha, sem esperar resposta de rede. Mas o navegador
é só conveniência: quem recusa é aqui.

VAL-11 não aparece aqui de propósito: ela é a conferência do CRC DENTRO
da coleira, feita pelo firmware. O Central só compara o CRC devolvido,
o que acontece em `delivery.py`.

Onde se conecta
---------------
`routers/fences.py` chama `validate_fence()` em `POST /api/fences/validate`
(só valida) e em `POST /api/fences` (valida antes de salvar). O resultado
também alimenta o arquivo de log da cerca, que registra o veredito de
cada regra como evidência para o relatório.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.config import Settings
from backend.services import geometry
from backend.services.canonical import from_e6, to_e6
from backend.services.geometry import GeoPoint, LocalProjection, PlanePoint

MIN_POINTS = 3
MAX_POINTS = 32

# Distância mínima entre pontos vizinhos (VAL-04). Abaixo disso o par de
# pontos não acrescenta nada à cerca e só gasta bytes no rádio.
MIN_POINT_DISTANCE_M = 1.0

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Violation:
    """Uma regra que não passou.

    `points` e `edges` servem para a tela destacar o problema no mapa.
    Os índices começam em 1, como o produtor conta ("o ponto 3"), não
    em 0 como o Python.
    """

    rule: str
    severity: str
    message: str
    points: list[int] = field(default_factory=list)
    edges: list[list[int]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "points": list(self.points),
            "edges": [list(edge) for edge in self.edges],
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """O veredito completo sobre uma cerca."""

    valid: bool
    violations: list[Violation]
    area_ha: float
    perimeter_m: float
    orientation: str | None

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == SEVERITY_WARNING]

    def as_dict(self) -> dict:
        """Formato de resposta da seção 5.4 da especificação."""
        return {
            "valid": self.valid,
            "violations": [v.as_dict() for v in self.violations],
            "area_ha": round(self.area_ha, 4),
            "perimeter_m": round(self.perimeter_m, 1),
            "orientation": self.orientation,
        }


def _format_number(value: float, digits: int = 1) -> str:
    """Formata número para o produtor ler: vírgula decimal, sem zero sobrando.

    `1,5 m` e não `1.5 m`; `47 m` e não `47.0 m`.
    """
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


# ===========================================================================
# A função principal
# ===========================================================================


def validate_fence(
    points: Sequence[GeoPoint],
    margin_attention_m: float,
    margin_critical_m: float,
    settings: Settings,
) -> ValidationResult:
    """Aplica todas as regras e devolve o veredito.

    A ORDEM importa. As regras estruturais vêm primeiro: sem quantidade
    de pontos válida e sem coordenadas dentro da faixa possível, não há
    como projetar, calcular área nem medir distância. Nesses casos a
    função devolve logo, com área e perímetro zerados — é melhor dizer
    "a cerca precisa ter de 3 a 32 pontos" do que também despejar dez
    erros derivados que só confundiriam o produtor.
    """
    violations: list[Violation] = []

    # --- VAL-08: margens (independe da geometria, pode vir junto) -------
    violations.extend(_val08_margins(margin_attention_m, margin_critical_m))

    # --- VAL-01: quantidade de pontos -----------------------------------
    count_errors = _val01_point_count(len(points))
    if count_errors:
        violations.extend(count_errors)
        return _result(violations, 0.0, 0.0, None)

    # --- VAL-02: coordenadas em faixa possível --------------------------
    coordinate_errors = _val02_coordinates(points)
    if coordinate_errors:
        violations.extend(coordinate_errors)
        return _result(violations, 0.0, 0.0, None)

    # A partir daqui trabalhamos com os pontos JÁ arredondados para
    # microgradus. Parece detalhe, mas garante uma propriedade que vale
    # para o sistema inteiro: a área, o perímetro e os avisos calculados
    # aqui são os da cerca que a coleira realmente vai receber, não os de
    # uma versão com casas decimais que serão descartadas depois. Sem
    # isso, um lado de exatamente 1,0000001 m passaria em VAL-04 na tela
    # e viraria 0,9999 m no rádio.
    points = canonicalize(points)

    projection = LocalProjection.centered_on(points)
    plane = projection.project_all(points)
    orientation = geometry.orientation(plane)
    area = geometry.area_ha(plane)
    perimeter = geometry.perimeter_m(plane)

    violations.extend(_val03_near_base(points, settings))
    violations.extend(_val04_duplicate_points(plane))

    # VAL-05 antes de VAL-06, e VAL-06 só se não houver cruzamento: o
    # sentido de giro de uma cerca com lados cruzados é INDEFINIDO. Uma
    # "gravata borboleta" tem área com sinal zero (os dois laços se
    # cancelam), então a conferência de sentido acusaria "os pontos estão
    # em linha reta" — mensagem errada para o problema real. Melhor dar
    # ao produtor um erro só, e o certo: dois lados se cruzam.
    crossing_errors = _val05_self_intersection(plane)
    violations.extend(crossing_errors)
    if not crossing_errors:
        violations.extend(_val06_orientation(orientation))

    violations.extend(_val07_short_sides(plane, settings))
    violations.extend(_val09_small_area(plane, margin_attention_m))
    violations.extend(_val10_lora_range(points, settings))
    violations.extend(_val12_gnss_margin(margin_critical_m, settings))

    return _result(violations, area, perimeter, orientation)


def canonicalize(points: Sequence[GeoPoint]) -> list[GeoPoint]:
    """Arredonda os pontos para microgradus e volta para graus.

    Só deve ser chamada depois de VAL-02 passar: uma coordenada absurda
    estouraria o limite de `int32` e daria um erro cru em vez da
    mensagem em português da regra.

    O espelho desta função no navegador é `arredondarParaMicrograus()`,
    em `frontend/js/geometry.js`.
    """
    return [GeoPoint(from_e6(to_e6(p.lat)), from_e6(to_e6(p.lon))) for p in points]


def _result(
    violations: list[Violation], area: float, perimeter: float, orientation: str | None
) -> ValidationResult:
    has_error = any(v.severity == SEVERITY_ERROR for v in violations)
    return ValidationResult(
        valid=not has_error,
        violations=violations,
        area_ha=area,
        perimeter_m=perimeter,
        orientation=orientation,
    )


# ===========================================================================
# Uma função por regra. Isoladas de propósito: a especificação marca
# várias delas como provisórias, e ajustar uma regra não deve obrigar a
# reler a função inteira.
# ===========================================================================


def _val01_point_count(quantity: int) -> list[Violation]:
    """VAL-01 — de 3 a 32 pontos.

    O limite de 32 não é arbitrário: 32 pontos × 8 bytes = 256 bytes, e o
    rádio SX1276 aceita no máximo 255 bytes por pacote. Daí vem a
    necessidade de fragmentar a cerca (seção 8.4 do planejamento).
    """
    if MIN_POINTS <= quantity <= MAX_POINTS:
        return []
    return [
        Violation(
            "VAL-01",
            SEVERITY_ERROR,
            f"A cerca precisa ter de {MIN_POINTS} a {MAX_POINTS} pontos. Agora tem {quantity}.",
        )
    ]


def _val02_coordinates(points: Sequence[GeoPoint]) -> list[Violation]:
    """VAL-02 — latitude em [−90, 90] e longitude em [−180, 180]."""
    invalid = [
        index
        for index, point in enumerate(points, start=1)
        if not (-90 <= point.lat <= 90 and -180 <= point.lon <= 180)
    ]
    if not invalid:
        return []
    if len(invalid) == 1:
        message = f"O ponto {invalid[0]} tem coordenadas inválidas."
    else:
        listed = ", ".join(str(index) for index in invalid)
        message = f"Os pontos {listed} têm coordenadas inválidas."
    return [Violation("VAL-02", SEVERITY_ERROR, message, points=invalid)]


def _val03_near_base(points: Sequence[GeoPoint], settings: Settings) -> list[Violation]:
    """VAL-03 — todo ponto a até BASE_RAIO_MAX_KM da Base.

    Esta é a regra que pega latitude e longitude trocadas, o erro mais
    comum de quem digita coordenadas. Trocar −31,3 por −54,06 joga o
    ponto a mais de 2.500 km daqui, o que nenhuma cerca de piquete
    justifica. Por isso a mensagem sugere a causa, e não só o sintoma.
    """
    base = GeoPoint(settings.base_lat, settings.base_lon)
    limit_m = settings.base_max_radius_km * 1000
    measured = [
        (index, geometry.haversine_m(point, base))
        for index, point in enumerate(points, start=1)
    ]
    offenders = [item for item in measured if item[1] > limit_m]
    if not offenders:
        return []
    index, distance = offenders[0]
    return [
        Violation(
            "VAL-03",
            SEVERITY_ERROR,
            (
                f"O ponto {index} está a {_format_number(distance / 1000)} km da Base. "
                "Confira se latitude e longitude não estão trocadas."
            ),
            points=[item[0] for item in offenders],
        )
    ]


def _val04_duplicate_points(plane: Sequence[PlanePoint]) -> list[Violation]:
    """VAL-04 — nenhum ponto a menos de 1 m do anterior, incluindo pn → p1."""
    quantity = len(plane)
    offenders: list[list[int]] = []
    for index, length in enumerate(geometry.side_lengths_m(plane)):
        if length < MIN_POINT_DISTANCE_M:
            first = index + 1
            second = (index + 1) % quantity + 1
            offenders.append([first, second])
    if not offenders:
        return []
    first, second = offenders[0]
    return [
        Violation(
            "VAL-04",
            SEVERITY_ERROR,
            (
                f"Os pontos {first} e {second} estão praticamente no mesmo lugar. "
                "Apague um deles."
            ),
            points=sorted({index for pair in offenders for index in pair}),
            edges=offenders,
        )
    ]


def _val05_self_intersection(plane: Sequence[PlanePoint]) -> list[Violation]:
    """VAL-05 — os lados da cerca não podem se cruzar.

    Uma cerca com lados cruzados (uma "gravata borboleta") não tem
    dentro e fora bem definidos: o teste de pertencimento da coleira
    daria resposta sem sentido em parte da área.
    """
    crossing = geometry.self_intersections(plane)
    if not crossing:
        return []
    return [
        Violation(
            "VAL-05",
            SEVERITY_ERROR,
            (
                "Dois lados da cerca se cruzam (destacados em vermelho). "
                "Apague ou mova um dos pontos."
            ),
            edges=[list(pair) for pair in crossing],
        )
    ]


def _val06_orientation(orientation: str | None) -> list[Violation]:
    """VAL-06 — sentido obrigatoriamente horário, sem normalização automática.

    Por que não inverter sozinho (DEC-15): a inversão silenciosa mudaria
    a ordem dos pontos e, com ela, o CRC da cerca — e o produtor nunca
    saberia que o sistema desenhou algo diferente do que ele marcou. O
    botão "Inverter ordem" no editor deixa a escolha visível.
    """
    if orientation == "clockwise":
        return []
    if orientation is None:
        return [
            Violation(
                "VAL-06",
                SEVERITY_ERROR,
                (
                    "Os pontos estão todos em linha reta e não formam uma área. "
                    "Mova um dos pontos para fora da linha."
                ),
            )
        ]
    return [
        Violation(
            "VAL-06",
            SEVERITY_ERROR,
            (
                "Os pontos foram marcados no sentido anti-horário. Marque seguindo "
                "o sentido dos ponteiros do relógio, ou use o botão Inverter ordem."
            ),
        )
    ]


def _val07_short_sides(plane: Sequence[PlanePoint], settings: Settings) -> list[Violation]:
    """VAL-07 — aviso quando um lado é menor que o erro esperado do GPS.

    Um lado de 2 m com GPS que erra 3 m significa que a coleira pode se
    achar dos dois lados daquele trecho de cerca, alternando a zona sem
    o animal se mover.
    """
    limit = settings.gnss_expected_error_m
    quantity = len(plane)
    found: list[Violation] = []
    for index, length in enumerate(geometry.side_lengths_m(plane)):
        if length >= limit or length < MIN_POINT_DISTANCE_M:
            # Lados abaixo de 1 m já são erro em VAL-04; não avisamos duas vezes.
            continue
        first = index + 1
        second = (index + 1) % quantity + 1
        found.append(
            Violation(
                "VAL-07",
                SEVERITY_WARNING,
                (
                    f"O lado entre os pontos {first} e {second} tem "
                    f"{_format_number(length)} m, menos que o erro do GPS "
                    f"(~{_format_number(limit)} m). Nessa parte a coleira pode se confundir."
                ),
                points=[first, second],
                edges=[[first, second]],
            )
        )
    return found


def val09_is_small_for_margin(
    plane: Sequence[PlanePoint], margin_attention_m: float
) -> bool:
    """O critério da regra VAL-09, isolado porque está "a confirmar".

    A especificação (seção 5.4) define dois sinais, e qualquer um deles
    basta para o aviso:

    1. algum ponto fica a menos de `dA` de TODOS os lados não adjacentes
       — ou seja, aquele canto da cerca é mais estreito que a margem;
    2. a área é menor que `dA² × 4`.

    O segundo é uma heurística grosseira: com dA = 5 m dá 100 m², uma
    área de 10 m × 10 m. O documento marca o critério como provisório, a
    calibrar com os dados GNSS da IP1 — por isso está nesta função
    separada, onde dá para trocar sem mexer no resto.
    """
    if margin_attention_m <= 0:
        return False

    if geometry.area_m2(plane) < (margin_attention_m**2) * 4:
        return True

    for vertex_index in range(len(plane)):
        distances = geometry.distances_to_non_adjacent_sides_m(plane, vertex_index)
        if distances and max(distances) < margin_attention_m:
            return True
    return False


def _val09_small_area(
    plane: Sequence[PlanePoint], margin_attention_m: float
) -> list[Violation]:
    """VAL-09 — aviso de cerca pequena para a margem de atenção."""
    if not val09_is_small_for_margin(plane, margin_attention_m):
        return []
    return [
        Violation(
            "VAL-09",
            SEVERITY_WARNING,
            (
                "A cerca é pequena para a margem de atenção escolhida: o animal "
                "pode receber aviso em quase toda a área."
            ),
        )
    ]


def _val10_lora_range(points: Sequence[GeoPoint], settings: Settings) -> list[Violation]:
    """VAL-10 — aviso de ponto além do alcance de rádio medido na IP1.

    É aviso, e não erro, porque o geofencing funciona SEM rádio: a
    coleira já tem a cerca gravada e apita sozinha. O que fica instável
    naquela região é a atualização da cerca e a telemetria.
    """
    base = GeoPoint(settings.base_lat, settings.base_lon)
    limit = settings.lora_range_m
    measured = [
        (index, geometry.haversine_m(point, base))
        for index, point in enumerate(points, start=1)
    ]
    offenders = [item for item in measured if item[1] > limit]
    if not offenders:
        return []
    index, distance = max(offenders, key=lambda item: item[1])
    return [
        Violation(
            "VAL-10",
            SEVERITY_WARNING,
            (
                f"O ponto {index} está a {_format_number(distance)} m da Base, além do "
                f"alcance testado do rádio (~{_format_number(limit)} m). A cerca funciona, "
                "mas a atualização e os dados dessa região podem falhar."
            ),
            points=[item[0] for item in offenders],
        )
    ]


def _val08_margins(
    margin_attention_m: float, margin_critical_m: float
) -> list[Violation]:
    """VAL-08 — é obrigatório que 0 < dC < dA.

    A margem crítica é a faixa mais perto da cerca, onde o aviso ao
    animal é mais forte. Se ela fosse maior que a de atenção, a ordem
    dos avisos se inverteria e o animal receberia o alerta forte antes
    do fraco.
    """
    if 0 < margin_critical_m < margin_attention_m:
        return []
    return [
        Violation(
            "VAL-08",
            SEVERITY_ERROR,
            (
                "A margem crítica precisa ser maior que zero e menor que a "
                "margem de atenção."
            ),
        )
    ]


def _val12_gnss_margin(margin_critical_m: float, settings: Settings) -> list[Violation]:
    """VAL-12 — aviso quando dC é menor que a folga recomendada pelo GPS.

    A folga é `GNSS_FATOR_SEGURANCA × GNSS_ERRO_ESPERADO_M`. Com os
    valores provisórios (1,5 × 3,0 m) dá 4,5 m — e a margem crítica
    padrão de 2 m já dispara este aviso, o que é esperado e coerente
    com a seção 5.4 da especificação.
    """
    recommended = settings.recommended_critical_margin_m
    if margin_critical_m >= recommended:
        return []
    return [
        Violation(
            "VAL-12",
            SEVERITY_WARNING,
            (
                f"A margem crítica ({_format_number(margin_critical_m)} m) é menor que a "
                f"folga recomendada para o erro do GPS ({_format_number(recommended)} m). "
                "Você pode enviar mesmo assim."
            ),
        )
    ]
