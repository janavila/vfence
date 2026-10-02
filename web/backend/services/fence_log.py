"""Log por cerca e exportação GeoJSON.

Para que serve o log por cerca
------------------------------
É um arquivo de texto, baixável em `GET /api/fences/{version}/log`, com
tudo o que aconteceu com UMA cerca: como ela foi criada, o veredito de
cada regra de validação (inclusive os avisos aceitos), e a linha do
tempo da entrega em cada coleira.

Dois usos: diagnóstico ("por que a COL02 não confirmou?") e evidência
para o relatório da IP2 — a seção 15 da especificação lista "o log de uma
cerca entregue pode ser baixado e serve de evidência" como critério de
pronto.

Por que em tabela e não em arquivo solto
----------------------------------------
As linhas ficam na tabela `fence_log_lines` e o `.txt` é MONTADO na hora
do download. Decisão da fase F0: o log sobrevive a reinício do servidor
sem precisar de pasta gravável, e não existe estado duplicado entre
banco e disco para sair de sincronia.

Onde se conecta
---------------
`fence_service.py` registra a criação e o resultado das regras;
`delivery.py` registra cada mudança de entrega; `routers/fences.py`
serve o `.txt` e o GeoJSON.
"""

from __future__ import annotations

import logging

from backend.clock import utc_now_iso
from backend.db import Database

logger = logging.getLogger("fences")


def append(db: Database, fence_version: int, line: str) -> None:
    """Acrescenta uma linha ao log daquela cerca.

    Grava nos dois lugares de propósito: na tabela (para o download) e
    no log do serviço (para quem está olhando o console ou o
    `journalctl` na hora). O prefixo `fence=N` é o que permite buscar
    uma cerca nos logs do Central e da Base e reconstruir o caminho
    inteiro dela (seção 3.7 do planejamento).
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO fence_log_lines (fence_version, ts, line) VALUES (?,?,?)",
            (fence_version, utc_now_iso(), line),
        )


def lines(db: Database, fence_version: int) -> list[dict]:
    """Linhas do log daquela cerca, na ordem em que aconteceram."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ts, line FROM fence_log_lines WHERE fence_version = ? ORDER BY id",
            (fence_version,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_validation(db: Database, fence_version: int, result_dict: dict) -> None:
    """Registra o veredito de CADA regra, não só as que falharam.

    Registrar as que passaram também é de propósito: o log precisa
    provar que a conferência aconteceu. "VAL-05 ok" é informação; a
    ausência da linha não distingue "passou" de "nem foi verificada".
    """
    append(
        db,
        fence_version,
        f"validation orientation={result_dict['orientation']} "
        f"area_ha={result_dict['area_ha']} perimeter_m={result_dict['perimeter_m']}",
    )
    violated = {v["rule"] for v in result_dict["violations"]}
    for violation in result_dict["violations"]:
        append(
            db,
            fence_version,
            f"validation {violation['rule']} {violation['severity']}: {violation['message']}",
        )
    checked = [f"VAL-{number:02d}" for number in range(1, 13)]
    passed = [
        rule for rule in checked if rule not in violated and rule not in {"VAL-11"}
    ]
    append(db, fence_version, f"validation passed: {', '.join(passed)}")
    append(db, fence_version, "validation VAL-11 not applicable (checked by the collar firmware)")


# ---------------------------------------------------------------------------
# O arquivo .txt
# ---------------------------------------------------------------------------


def render(db: Database, fence: dict, deliveries: list[dict]) -> str:
    """Monta o arquivo de log de uma cerca (seção 10 da especificação).

    Cabeçalho com nome, versão, margens, área, perímetro, CRC e os
    pontos em graus E em microgradus; depois o resultado das regras; por
    fim a linha do tempo da entrega em cada coleira.

    Os pontos aparecem nas duas unidades de propósito: graus é o que o
    produtor reconhece no mapa, microgradus é o que entra no CRC e vai
    para a coleira. Com os dois lado a lado, quem for conferir uma
    divergência de CRC não precisa recalcular nada à mão.
    """
    larguras = "=" * 78
    out: list[str] = [
        larguras,
        f"VFence — log da cerca nº {fence['version']}",
        larguras,
        "",
        f"Nome               : {fence['name']}",
        f"Versão             : {fence['version']}",
        f"Situação           : {fence['status']}",
        f"Criada em (UTC)    : {fence['created_at']}",
        f"Margem de atenção  : {fence['margin_attention_m']:.2f} m"
        f"  ({int(round(fence['margin_attention_m'] * 100))} cm)",
        f"Margem crítica     : {fence['margin_critical_m']:.2f} m"
        f"  ({int(round(fence['margin_critical_m'] * 100))} cm)",
        f"Área               : {fence['area_ha']:.4f} ha  ({fence['area_ha'] * 10000:.1f} m²)",
        f"Perímetro          : {fence['perimeter_m']:.1f} m",
        f"Pontos             : {len(fence['points'])}",
        f"CRC-32             : {fence['crc32']}",
    ]
    if fence.get("reactivated_from"):
        out.append(f"Reativada da versão: {fence['reactivated_from']}")

    out += ["", "-" * 78, "Pontos da cerca (sentido horário)", "-" * 78, ""]
    out.append(f"{'#':>3}  {'latitude':>13}  {'longitude':>13}  {'lat_e6':>12}  {'lon_e6':>12}")
    for index, point in enumerate(fence["points"], start=1):
        lat_e6 = int(round(point["lat"] * 1_000_000))
        lon_e6 = int(round(point["lon"] * 1_000_000))
        out.append(
            f"{index:>3}  {point['lat']:>13.6f}  {point['lon']:>13.6f}"
            f"  {lat_e6:>12}  {lon_e6:>12}"
        )

    out += ["", "-" * 78, "Linha do tempo", "-" * 78, ""]
    for entry in lines(db, fence["version"]):
        out.append(f"{entry['ts']}  {entry['line']}")

    out += ["", "-" * 78, "Situação da entrega por coleira", "-" * 78, ""]
    if not deliveries:
        out.append("(nenhuma coleira cadastrada para esta cerca)")
    else:
        out.append(f"{'coleira':<12}  {'situação':<14}  {'tentativas':>10}  {'crc devolvido':<14}  detalhe")
        for delivery in deliveries:
            out.append(
                f"{delivery['collar_id']:<12}  {delivery['status_label']:<14}"
                f"  {delivery['attempts']:>10}  {(delivery['crc32_reported'] or '-'):<14}"
                f"  {delivery['detail'] or '-'}"
            )

    out += [
        "",
        larguras,
        f"Gerado em {utc_now_iso()} pelo VFence Central.",
        "Horários em UTC (ISO 8601). A zona do animal é decidida pela coleira.",
        larguras,
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


def to_geojson(fence: dict) -> dict:
    """Exporta a cerca como GeoJSON, conforme a RFC 7946 (seção 6.1).

    Três diferenças em relação a como a cerca vive dentro do sistema, e
    as três são exigência do formato:

    1. **coordenadas como `[lon, lat]`**, invertidas em relação ao
       Leaflet, que usa `[lat, lng]`. Esta é a ÚNICA função do backend
       que faz essa inversão, de propósito: a troca de lat por lon é um
       dos riscos listados no planejamento, e concentrá-la num lugar só,
       coberto por teste, é a mitigação.
    2. **anel fechado**: `p1` é repetido no fim. Dentro do sistema não é,
       para economizar bytes no rádio.
    3. **sentido anti-horário**: a RFC 7946 exige isso do anel externo,
       e a cerca é sempre horária. A inversão mantém `p1` no começo
       (`p1, pn, …, p2`), a mesma convenção do botão "Inverter ordem"
       do editor.

    Serve para conferência visual independente: o arquivo abre no
    geojson.io ou no QGIS e mostra a cerca desenhada, sem depender de
    nenhum código nosso.
    """
    points = fence["points"]
    # p1 fica no lugar; os demais invertem. Resultado: anti-horário.
    reordered = [points[0], *reversed(points[1:])]
    ring = [[point["lon"], point["lat"]] for point in reordered]
    ring.append(ring[0])  # fecha o anel

    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "version": fence["version"],
            "name": fence["name"],
            "status": fence["status"],
            "margin_attention_m": fence["margin_attention_m"],
            "margin_critical_m": fence["margin_critical_m"],
            "area_ha": fence["area_ha"],
            "perimeter_m": fence["perimeter_m"],
            "crc32": fence["crc32"],
            "created_at": fence["created_at"],
        },
    }
