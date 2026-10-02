"""Criar, ativar, reativar e consultar cercas.

O que este módulo garante
-------------------------
1. **Versões só crescem** (seção 3.3 do planejamento). Editar a cerca
   não altera a anterior: cria uma versão nova e maior. Assim "maior
   versão = mais recente" vale em todas as camadas, e o log de uma
   versão antiga continua verdadeiro para sempre.
2. **Uma cerca ativa por vez** (premissa P3). Ativar a nova desativa a
   anterior, na MESMA transação. O banco também garante isso pelo
   índice único parcial `idx_fences_single_active`, então um erro de
   programação aqui é recusado pelo SQLite em vez de passar.
3. **A conversão para inteiro acontece uma vez.** Os pontos são
   guardados em microgradus e o CRC é calculado sobre eles. O que a API
   devolve são esses mesmos inteiros convertidos de volta — ou seja, a
   tela mostra exatamente o que a coleira recebe.

Reativar não é "voltar"
-----------------------
`POST /api/fences/{version}/reactivate` copia os PONTOS de uma versão
antiga para uma versão NOVA, que passa a ser a ativa. A coluna
`reactivated_from` registra a origem. Nunca reabrimos uma versão
encerrada: isso manteria duas verdades sobre o mesmo número.

Onde se conecta
---------------
`routers/fences.py` é o único a chamar este módulo. Ele por sua vez usa
`validation`, `canonical`, `crc`, `delivery` e `fence_log`.
"""

from __future__ import annotations

import json
import logging

from backend.clock import utc_now_iso
from backend.config import Settings
from backend.db import Database
from backend.services import delivery, fence_log
from backend.services.canonical import (
    canonical_bytes,
    from_cm,
    from_e6,
    points_to_e6,
    to_cm,
)
from backend.services.crc import crc32_hex
from backend.services.geometry import GeoPoint
from backend.services.validation import ValidationResult, validate_fence

logger = logging.getLogger("fences")


class FenceNotFound(LookupError):
    """A versão pedida não existe."""


class FenceRefused(ValueError):
    """A cerca não passou na validação.

    Carrega o `ValidationResult` para o router montar o corpo de erro da
    seção 6 sem precisar validar de novo.
    """

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("fence_invalid")
        self.result = result


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def _row_to_fence(conn, row) -> dict:
    """Monta o dicionário de uma cerca, com os pontos já em graus."""
    points = conn.execute(
        "SELECT lat_e6, lon_e6 FROM fence_points WHERE fence_id = ? ORDER BY seq",
        (row["id"],),
    ).fetchall()
    return {
        "version": row["version"],
        "name": row["name"],
        "status": row["status"],
        "margin_attention_m": from_cm(row["margin_attention_cm"]),
        "margin_critical_m": from_cm(row["margin_critical_cm"]),
        "points": [
            {"lat": from_e6(p["lat_e6"]), "lon": from_e6(p["lon_e6"])} for p in points
        ],
        "area_ha": row["area_m2"] / 10_000,
        "perimeter_m": row["perimeter_m"],
        "crc32": row["crc32"],
        "warnings": json.loads(row["warnings_json"]),
        "created_at": row["created_at"],
        "reactivated_from": row["reactivated_from"],
    }


def get_by_version(db: Database, version: int) -> dict:
    """Uma versão completa. Levanta `FenceNotFound` se não existir."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM fences WHERE version = ?", (version,)).fetchone()
        if row is None:
            raise FenceNotFound(version)
        return _row_to_fence(conn, row)


def get_active(db: Database) -> dict | None:
    """A cerca que está valendo agora, ou `None` se nunca houve uma."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM fences WHERE status = 'active'").fetchone()
        return _row_to_fence(conn, row) if row else None


def active_version(db: Database) -> int | None:
    """Só o número da versão ativa. É o `desired_fence_version` do heartbeat."""
    with db.connect() as conn:
        row = conn.execute("SELECT version FROM fences WHERE status = 'active'").fetchone()
    return row["version"] if row else None


def list_summaries(db: Database) -> list[dict]:
    """Histórico, mais recente primeiro, sem os pontos (lista leve)."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT f.*, (SELECT COUNT(*) FROM fence_points p WHERE p.fence_id = f.id) AS point_count
            FROM fences f ORDER BY f.version DESC
            """
        ).fetchall()
    return [
        {
            "version": row["version"],
            "name": row["name"],
            "status": row["status"],
            "point_count": row["point_count"],
            "area_ha": row["area_m2"] / 10_000,
            "perimeter_m": row["perimeter_m"],
            "crc32": row["crc32"],
            "created_at": row["created_at"],
            "reactivated_from": row["reactivated_from"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Validação (sem salvar)
# ---------------------------------------------------------------------------


def validate(
    points: list[GeoPoint],
    margin_attention_m: float,
    margin_critical_m: float,
    settings: Settings,
) -> ValidationResult:
    """Aplica as regras sem tocar no banco. É o `POST /api/fences/validate`."""
    return validate_fence(points, margin_attention_m, margin_critical_m, settings)


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------


def _next_version(conn) -> int:
    """Próximo número de versão: sempre maior que qualquer já usado.

    Baseado no MAIOR já existente, não na contagem de linhas. Com a
    contagem, apagar uma cerca faria o próximo número repetir um já
    usado — e um CRC antigo passaria a valer para uma cerca diferente.
    """
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS maximum FROM fences").fetchone()
    return int(row["maximum"]) + 1


def create_fence(
    db: Database,
    settings: Settings,
    *,
    name: str,
    margin_attention_m: float,
    margin_critical_m: float,
    points: list[GeoPoint],
    result: ValidationResult,
    reactivated_from: int | None = None,
) -> dict:
    """Grava uma versão nova e a torna a ativa.

    Pressupõe que a validação já passou: quem chama (o router) é
    responsável por recusar antes de chegar aqui. Esta função não
    revalida para não haver duas respostas possíveis para a mesma cerca.

    Tudo em UMA transação: desativar a anterior, inserir a nova, gravar
    os pontos. Se qualquer passo falhar, nada acontece — e nunca existe
    um instante com duas cercas ativas ou com uma cerca sem pontos.
    """
    # A conversão para inteiro acontece aqui, e só aqui.
    points_e6 = points_to_e6([(p.lat, p.lon) for p in points])
    attention_cm = to_cm(margin_attention_m)
    critical_cm = to_cm(margin_critical_m)
    created_at = utc_now_iso()
    warnings = [v.as_dict() for v in result.warnings]

    with db.connect() as conn:
        version = _next_version(conn)
        blob = canonical_bytes(version, attention_cm, critical_cm, points_e6)
        crc32 = crc32_hex(blob)

        # Desativar ANTES de inserir: o índice único parcial do banco
        # não admite duas linhas 'active' nem por um instante.
        conn.execute("UPDATE fences SET status = 'inactive' WHERE status = 'active'")

        cursor = conn.execute(
            """
            INSERT INTO fences (version, name, status, margin_attention_cm, margin_critical_cm,
                                area_m2, perimeter_m, crc32, warnings_json, reactivated_from, created_at)
            VALUES (?,?,'active',?,?,?,?,?,?,?,?)
            """,
            (
                version, name.strip(), attention_cm, critical_cm,
                result.area_ha * 10_000, result.perimeter_m, crc32,
                json.dumps(warnings, ensure_ascii=False), reactivated_from, created_at,
            ),
        )
        fence_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO fence_points (fence_id, seq, lat_e6, lon_e6) VALUES (?,?,?,?)",
            [
                (fence_id, seq, point.lat_e6, point.lon_e6)
                for seq, point in enumerate(points_e6, start=1)
            ],
        )

    logger.info(
        "fence=%s created (%d points, %.3f ha, %.1f m, crc=%s)",
        version, len(points_e6), result.area_ha, result.perimeter_m, crc32,
    )
    logger.info("fence=%s orientation checked: %s", version, result.orientation)

    # --- log da cerca -------------------------------------------------
    fence_log.append(
        db,
        version,
        f"fence created name={name.strip()!r} points={len(points_e6)} "
        f"dA={margin_attention_m}m dC={margin_critical_m}m crc={crc32}"
        + (f" reactivated_from={reactivated_from}" if reactivated_from else ""),
    )
    fence_log.record_validation(db, version, result.as_dict())
    for warning in warnings:
        logger.warning(
            "fence=%s %s %s (accepted by user)", version, warning["rule"], warning["message"]
        )
        fence_log.append(
            db, version, f"warning accepted by user: {warning['rule']} {warning['message']}"
        )
    fence_log.append(db, version, "fence activated (previous fence deactivated)")

    # --- entregas pendentes, uma por coleira conhecida ----------------
    collar_ids = [row["collar_id"] for row in db.list_collars()]
    delivery.create_pending(db, version, collar_ids)

    return get_by_version(db, version)


def reactivate(db: Database, settings: Settings, version: int) -> dict:
    """Cria uma versão NOVA com os pontos de uma versão antiga.

    Revalida de propósito: a Base pode ter mudado de posição no `.env`
    desde então, e as regras VAL-03 e VAL-10 dependem disso. Reativar
    uma cerca que hoje estaria fora do alcance precisa avisar de novo.

    Avisos são aceitos automaticamente: o produtor já os aceitou quando
    criou a cerca original, e a tela de Histórico pede confirmação antes
    de chamar esta rota.
    """
    original = get_by_version(db, version)
    points = [GeoPoint(p["lat"], p["lon"]) for p in original["points"]]

    result = validate_fence(
        points, original["margin_attention_m"], original["margin_critical_m"], settings
    )
    if not result.valid:
        # Acontece se a Base mudou de lugar e a cerca antiga ficou longe.
        logger.warning(
            "fence=%s reactivation refused: %s",
            version, ", ".join(v.rule for v in result.errors),
        )
        raise FenceRefused(result)

    return create_fence(
        db, settings,
        name=original["name"],
        margin_attention_m=original["margin_attention_m"],
        margin_critical_m=original["margin_critical_m"],
        points=points,
        result=result,
        reactivated_from=version,
    )
