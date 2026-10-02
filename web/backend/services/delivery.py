"""Estados da entrega de uma cerca em cada coleira (seção 8).

O caminho normal
----------------
    pending ──(a Base baixa a cerca)──▶ at_base ──(a Base relata)──▶ transmitting ──▶ confirmed
                                                                             └──────▶ failed

Quem muda cada estado
---------------------
| Estado        | Quem muda | Quando                                     | Na tela      |
|---------------|-----------|--------------------------------------------|--------------|
| pending       | Central   | cerca criada, uma linha por coleira        | Salva        |
| at_base       | Central   | a Base chamou GET /api/edge/fences/{v}    | Na Base      |
| transmitting  | Base      | começou a enviar pelo rádio               | Transmitindo |
| confirmed     | Base      | a coleira confirmou E o CRC bate          | Confirmada   |
| failed        | Base      | desistiu, ou o CRC veio diferente         | Falhou       |

A regra que dá sentido ao sistema
---------------------------------
`confirmed` só vale se o CRC devolvido pela coleira for IGUAL ao da
cerca. É isso que transforma "a coleira respondeu" em "a coleira tem
exatamente a cerca que o produtor desenhou". CRC diferente vira `failed`
com `detail = "crc_mismatch"`.

Dois casos que a especificação não previu, decididos na fase F0:

- **`confirmed` sem o campo `crc32`**: tratado como `failed` com
  `detail = "crc_missing"`. Assumir sucesso sem prova seria pior: o
  produtor veria "Confirmada" sem ninguém ter conferido nada.
- **volta de `failed` para `transmitting`**: aceita. É o reenvio
  legítimo descrito na seção 8.5 do planejamento, quando a coleira
  reaparece com versão antiga.

Status atrasado de versão antiga
--------------------------------
Se a Base relatar algo sobre uma versão que não é mais a ativa, o
Central registra no log da cerca mas NÃO mexe na cerca ativa (seção 8).
Isso evita que um pacote atrasado reabra uma entrega já encerrada.

Onde se conecta
---------------
`fence_service.py` cria as linhas `pending`; `routers/edge.py` chama
`mark_at_base()` no download e `apply_report()` em cada item de lote;
`routers/fences.py` lê com `list_for_fence()`.
"""

from __future__ import annotations

import logging

from backend.clock import utc_now_iso
from backend.db import Database
from backend.services import fence_log

logger = logging.getLogger("delivery")

# Estados válidos, na ordem do avanço normal. A ordem importa para
# `_is_regression()`, que decide se um relato atrasado deve ser ignorado.
STATUS_ORDER = ("pending", "at_base", "transmitting", "confirmed", "failed")

# Estados que a Base pode relatar em POST /api/edge/batch. `pending` e
# `at_base` são decisão do Central e não aceitamos da Base.
BASE_REPORTABLE = {"transmitting", "confirmed", "failed"}

# Texto que aparece na tela, em português do produtor (seção 8).
STATUS_LABELS = {
    "pending": "Salva",
    "at_base": "Na Base",
    "transmitting": "Transmitindo",
    "confirmed": "Confirmada",
    "failed": "Falhou",
}


def label(status: str) -> str:
    """Traduz o estado técnico para o texto da tela."""
    return STATUS_LABELS.get(status, status)


def as_dict(row: dict) -> dict:
    """Acrescenta o texto da tela a uma linha de `deliveries`."""
    return {**row, "status_label": label(row["status"])}


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------


def create_pending(db: Database, fence_version: int, collar_ids: list[str]) -> None:
    """Cria uma linha `pending` por coleira conhecida.

    Chamada quando a cerca é criada. Se não houver coleira cadastrada,
    não cria nada — e a tela mostra "nenhuma coleira cadastrada" em vez
    de um acompanhamento vazio sem explicação.
    """
    if not collar_ids:
        logger.warning(
            "fence=%s no known collars: nothing to deliver", fence_version
        )
        return

    now = utc_now_iso()
    with db.connect() as conn:
        conn.executemany(
            """
            INSERT INTO deliveries (fence_version, collar_id, status, attempts, updated_at)
            VALUES (?, ?, 'pending', 0, ?)
            ON CONFLICT(fence_version, collar_id) DO NOTHING
            """,
            [(fence_version, collar_id, now) for collar_id in collar_ids],
        )
    for collar_id in collar_ids:
        fence_log.append(
            db, fence_version, f"delivery collar={collar_id} pending (fence saved)"
        )
    logger.info(
        "fence=%s deliveries created (%d collars: %s)",
        fence_version,
        len(collar_ids),
        ",".join(collar_ids),
    )


# ---------------------------------------------------------------------------
# Transições
# ---------------------------------------------------------------------------


def mark_at_base(db: Database, fence_version: int, base_id: str) -> int:
    """Passa as entregas `pending` da versão para `at_base`.

    É o efeito colateral de `GET /api/edge/fences/{version}` (seção 9.2):
    a Base baixou a cerca, então ela já está na propriedade, esperando o
    rádio. Só `pending` avança — uma entrega que já estava
    `transmitting` não volta atrás por causa de um novo download.
    """
    now = utc_now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE deliveries SET status = 'at_base', updated_at = ?
            WHERE fence_version = ? AND status = 'pending'
            """,
            (now, fence_version),
        )
        changed = cursor.rowcount
    if changed:
        fence_log.append(
            db,
            fence_version,
            f"delivery base={base_id} downloaded fence: {changed} collar(s) now at_base",
        )
    logger.info("fence=%s downloaded base=%s (%d pending -> at_base)", fence_version, base_id, changed)
    return changed


def _is_regression(current: str, incoming: str) -> bool:
    """O relato recebido é um retrocesso que deve ser ignorado?

    Um pacote atrasado pode chegar depois de um mais novo. Deixar o
    estado voltar faria a tela piscar de "Confirmada" para
    "Transmitindo" sem nada ter acontecido.

    A única volta permitida é `failed → transmitting`: é o reenvio
    legítimo de quando a coleira reaparece com versão antiga (seção 8.5
    do planejamento).
    """
    if current == "failed" and incoming == "transmitting":
        return False
    if current == "confirmed":
        # Entrega confirmada é ponto final. Nem transmitting nem failed
        # atrasados a reabrem.
        return True
    return STATUS_ORDER.index(incoming) < STATUS_ORDER.index(current)


def apply_report(
    db: Database,
    *,
    fence_version: int,
    collar_id: str,
    status: str,
    crc32_reported: str | None,
    expected_crc32: str | None,
    active_version: int | None,
    base_id: str,
) -> dict | None:
    """Aplica o que a Base relatou sobre uma coleira.

    Devolve a linha atualizada (para o WebSocket avisar o navegador) ou
    `None` quando nada mudou.

    A ordem das conferências é a da seção 8:

    1. estado relatado tem de ser um dos que a Base pode relatar;
    2. versão antiga só vai para o log, sem mexer na cerca ativa;
    3. `confirmed` exige CRC igual ao da cerca, senão vira `failed`;
    4. retrocesso de estado é ignorado.
    """
    if status not in BASE_REPORTABLE:
        logger.warning(
            "fence=%s collar=%s base=%s rejected delivery status %r",
            fence_version, collar_id, base_id, status,
        )
        return None

    detail: str | None = None

    # --- A regra do CRC (o que dá sentido a "Confirmada") --------------
    if status == "confirmed":
        if not crc32_reported:
            status, detail = "failed", "crc_missing"
            logger.warning(
                "fence=%s collar=%s confirmed without crc: marked failed",
                fence_version, collar_id,
            )
        elif expected_crc32 and crc32_reported.upper() != expected_crc32.upper():
            status, detail = "failed", "crc_mismatch"
            logger.warning(
                "fence=%s collar=%s crc mismatch (reported=%s expected=%s): marked failed",
                fence_version, collar_id, crc32_reported, expected_crc32,
            )

    now = utc_now_iso()
    stale = active_version is not None and fence_version != active_version

    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM deliveries WHERE fence_version = ? AND collar_id = ?",
            (fence_version, collar_id),
        ).fetchone()

        # --- Versão antiga: só log (seção 8) ---------------------------
        if stale:
            fence_log.append(
                db,
                fence_version,
                f"delivery collar={collar_id} late report {status}"
                f"{f' ({detail})' if detail else ''} ignored: active fence is {active_version}",
            )
            logger.info(
                "fence=%s collar=%s late report %s ignored (active=%s)",
                fence_version, collar_id, status, active_version,
            )
            return None

        if current is not None and _is_regression(current["status"], status):
            logger.info(
                "fence=%s collar=%s report %s ignored (already %s)",
                fence_version, collar_id, status, current["status"],
            )
            return None

        attempts = (current["attempts"] if current else 0) + (
            1 if status == "transmitting" else 0
        )

        conn.execute(
            """
            INSERT INTO deliveries
                (fence_version, collar_id, status, attempts, crc32_reported, detail, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(fence_version, collar_id) DO UPDATE SET
                status = excluded.status,
                attempts = excluded.attempts,
                crc32_reported = COALESCE(excluded.crc32_reported, deliveries.crc32_reported),
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (
                fence_version, collar_id, status, attempts,
                crc32_reported.upper() if crc32_reported else None,
                detail, now,
            ),
        )
        updated = dict(
            conn.execute(
                "SELECT * FROM deliveries WHERE fence_version = ? AND collar_id = ?",
                (fence_version, collar_id),
            ).fetchone()
        )

    suffix = f" (crc={crc32_reported})" if crc32_reported else ""
    if detail:
        suffix = f" ({detail}, reported crc={crc32_reported or 'none'})"
    fence_log.append(
        db, fence_version, f"delivery collar={collar_id} {status}{suffix}"
    )
    logger.info("fence=%s collar=%s %s%s", fence_version, collar_id, status, suffix)
    return as_dict(updated)


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def list_for_fence(db: Database, fence_version: int) -> list[dict]:
    """Entregas de uma versão, em ordem de coleira."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE fence_version = ? ORDER BY collar_id",
            (fence_version,),
        ).fetchall()
    return [as_dict(dict(row)) for row in rows]


def count_confirmed(db: Database, fence_version: int) -> tuple[int, int]:
    """Quantas coleiras confirmaram, de quantas no total.

    Usado pelo painel de Início: "3 de 4 coleiras confirmaram".
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed
            FROM deliveries WHERE fence_version = ?
            """,
            (fence_version,),
        ).fetchone()
    return int(row["confirmed"] or 0), int(row["total"] or 0)
