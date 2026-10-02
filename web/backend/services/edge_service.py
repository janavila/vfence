"""Gravação do que a Base envia: telemetria, eventos e status de entrega.

Este módulo não está na seção 4 da especificação
------------------------------------------------
Foi acrescentado porque o processamento do lote (seção 9.3) é a lógica
mais delicada do lado da Base, e deixá-la dentro do router misturaria
decisão de negócio com tradução de HTTP. Além disso, aqui ela fica
testável sem subir servidor.

Entrega "pelo menos uma vez", e o que isso exige
------------------------------------------------
A Base reenvia um lote até receber confirmação. Se a resposta se perder
na volta, ela manda tudo de novo — e não pode gravar duas vezes. A
chave `(base_id, edge_seq)` da tabela `edge_items` garante isso: item já
visto é ignorado sem erro.

O resultado é a propriedade que a seção 8.2 do planejamento descreve:
nada se perde e nada se duplica.

O que é `acked_up_to`
---------------------
O maior `edge_seq` CONTÍNUO já gravado para aquela Base. A Base pode
apagar da fila local tudo até esse número.

A palavra "contínuo" é o ponto: se chegaram os itens 1, 2, 3 e 7, o
confirmado é 3, não 7. O item 7 está gravado (e não será gravado de
novo), mas a Base precisa continuar guardando o 4, o 5 e o 6, que ainda
não chegaram.

O Central não decide zona
-------------------------
A zona (SEGURO, ATENCAO, CRITICO, FORA, GNSS_INVALIDO) vem calculada
pela coleira e é apenas gravada. Se chegar um valor fora da lista, o
Central grava a telemetria com zona vazia e registra um evento
`invalid_zone`, em vez de recusar o lote. Recusar criaria um item
envenenado: a Base reenviaria para sempre o mesmo lote e pararia de
entregar todo o resto.

Onde se conecta
---------------
`routers/edge.py` chama `process_batch()`. As mensagens devolvidas vão
para o WebSocket, e quem as envia é o router — porque o envio é
assíncrono e esta função roda em thread separada.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from backend.clock import ISO_FORMAT, utc_now_iso
from backend.config import Settings
from backend.db import Database
from backend.services import delivery, fence_service

logger = logging.getLogger("edge")

# Zonas aceitas. É a mesma lista do parser do Monitor (app/protocol.py:8).
# O Central só CONFERE o valor; quem decide zona é a coleira.
VALID_ZONES = {"SEGURO", "ATENCAO", "CRITICO", "FORA", "GNSS_INVALIDO"}

ITEM_TYPES = {"telemetry", "delivery", "event"}


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def apply_heartbeat(
    db: Database,
    *,
    base_id: str,
    fence_version: int | None,
    serial: str | None,
    queue_size: int | None,
) -> dict:
    """Guarda o estado reportado pela Base e devolve o estado desejado.

    É o encontro dos dois estados da seção 3.2 do planejamento: a Base
    conta qual cerca ela tem (`fence_version`), e o Central responde
    qual ela deveria ter (`desired_fence_version`). A diferença entre os
    dois é o que dispara o download — e é o que torna o sistema
    autocorretivo depois de uma queda de rede.
    """
    now = utc_now_iso()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE bases SET last_heartbeat = ?, serial_status = ?, queue_size = ?,
                             fence_version_reported = ?
            WHERE base_id = ?
            """,
            (now, serial, queue_size, fence_version, base_id),
        )

    desired = fence_service.active_version(db)
    if desired is not None and fence_version != desired:
        logger.info(
            "base=%s heartbeat: has fence=%s, wants fence=%s (serial=%s, queue=%s)",
            base_id, fence_version, desired, serial, queue_size,
        )
    return {"desired_fence_version": desired, "server_time": now}


def base_is_online(base: dict, settings: Settings, now: datetime | None = None) -> bool:
    """A Base está online? Mais de `BASE_OFFLINE_APOS_S` sem heartbeat = não.

    Este valor é CALCULADO, nunca gravado: "online" envelhece sozinho, e
    uma coluna no banco ficaria mentindo até alguém atualizá-la.

    O parâmetro `now` existe para o teste poder adiantar o relógio sem
    esperar o tempo passar nem substituir o módulo `datetime`. Em uso
    normal fica vazio e a função usa a hora de agora.
    """
    last = base.get("last_heartbeat")
    if not last:
        return False
    try:
        moment = datetime.strptime(last, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        # Hora gravada em formato inesperado: tratamos como offline em vez
        # de derrubar a tela inteira por causa de uma linha estranha.
        logger.warning("base=%s last_heartbeat em formato inesperado: %r", base.get("base_id"), last)
        return False
    reference = now or datetime.now(timezone.utc)
    return reference - moment <= timedelta(seconds=settings.base_offline_after_s)


# ---------------------------------------------------------------------------
# Lote
# ---------------------------------------------------------------------------


def process_batch(
    db: Database, settings: Settings, base_id: str, items: list[dict]
) -> tuple[int, list[tuple[str, dict]]]:
    """Grava os itens de um lote e devolve `(acked_up_to, mensagens)`.

    Os itens são processados em ordem de `edge_seq`, não na ordem em que
    chegaram no JSON: a Base pode montar o lote fora de ordem, e o
    estado de uma entrega depende da sequência.
    """
    messages: list[tuple[str, dict]] = []
    active = fence_service.active_version(db)
    stored = 0
    skipped = 0

    for item in sorted(items, key=lambda entry: entry.get("edge_seq", 0)):
        edge_seq = item["edge_seq"]
        item_type = item["type"]
        data = item.get("data") or {}

        if not _claim(db, base_id, edge_seq, item_type):
            skipped += 1
            continue
        stored += 1

        if item_type == "telemetry":
            message = _store_telemetry(db, base_id, edge_seq, data)
            if message:
                messages.append(("telemetry", message))
        elif item_type == "event":
            message = _store_event(db, base_id, edge_seq, data)
            if message:
                messages.append(("event", message))
        elif item_type == "delivery":
            message = _apply_delivery(db, base_id, data, active)
            if message:
                messages.append(("delivery", message))

    acked = acked_up_to(db, base_id)
    logger.info(
        "base=%s batch received (%d stored, %d already known, acked_up_to=%d)",
        base_id, stored, skipped, acked,
    )
    return acked, messages


def _claim(db: Database, base_id: str, edge_seq: int, item_type: str) -> bool:
    """Registra o item como recebido. Devolve `False` se já era conhecido.

    É aqui que a idempotência acontece: `INSERT OR IGNORE` na chave
    `(base_id, edge_seq)`. O `rowcount` diz se a linha foi inserida
    agora (item novo) ou ignorada (reenvio).
    """
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO edge_items (base_id, edge_seq, type, received_at)
            VALUES (?,?,?,?)
            """,
            (base_id, edge_seq, item_type, utc_now_iso()),
        )
        return cursor.rowcount == 1


def acked_up_to(db: Database, base_id: str) -> int:
    """Maior `edge_seq` contínuo já gravado para a Base. Zero se não houver.

    "Contínuo" é contado A PARTIR DO MENOR item que o Central tem, não a
    partir de 1. A primeira versão desta função exigia começar em 1, e
    estava errada: a fila local da Base é persistente e só cresce, então
    ela apaga o que já foi confirmado. Depois de um tempo em operação, o
    menor item que o Central tem é 1531, não 1 — e é exatamente o caso
    do exemplo da seção 9.3 da especificação, que espera
    `acked_up_to: 1533` para os itens 1531, 1532 e 1533.

    Com a regra errada, o Central respondia 0 e a Base reenviaria a fila
    inteira para sempre, sem nunca conseguir limpá-la.

    Caminho rápido: se a quantidade de itens é igual ao intervalo entre o
    menor e o maior, não existe buraco e a resposta é o maior. É o caso
    normal e resolve com uma consulta só. Só quando há buraco é que
    percorremos a lista para achar o primeiro.
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT MIN(edge_seq) AS menor, MAX(edge_seq) AS maior, COUNT(*) AS quantos
            FROM edge_items WHERE base_id = ?
            """,
            (base_id,),
        ).fetchone()
        if not row or row["quantos"] == 0:
            return 0

        menor, maior, quantos = row["menor"], row["maior"], row["quantos"]
        if quantos == maior - menor + 1:
            return maior

        sequencias = conn.execute(
            "SELECT edge_seq FROM edge_items WHERE base_id = ? ORDER BY edge_seq",
            (base_id,),
        ).fetchall()

    esperado = menor
    for linha in sequencias:
        if linha["edge_seq"] != esperado:
            break
        esperado += 1
    return esperado - 1


# ---------------------------------------------------------------------------
# Um gravador por tipo de item
# ---------------------------------------------------------------------------


def _ensure_collar(conn, collar_id: str, base_id: str) -> None:
    """Cadastra a coleira se ela ainda não existir.

    A seção 7 manda cadastrar as coleiras do `.env` e também "quando
    aparecem na telemetria". Isso evita perder dados de uma coleira nova
    só porque ninguém lembrou de acrescentá-la ao `COLARES_CONHECIDOS`.
    """
    conn.execute(
        "INSERT OR IGNORE INTO collars (collar_id, base_id) VALUES (?,?)",
        (collar_id, base_id),
    )


def _store_telemetry(db: Database, base_id: str, edge_seq: int, data: dict) -> dict | None:
    """Grava uma posição e atualiza o último estado da coleira."""
    collar_id = data.get("collar_id")
    if not collar_id:
        logger.warning("base=%s telemetry without collar_id (edge_seq=%s)", base_id, edge_seq)
        return None

    zone = (data.get("zone") or "").upper() or None
    invalid_zone = zone is not None and zone not in VALID_ZONES
    if invalid_zone:
        logger.warning(
            "base=%s collar=%s unknown zone %r: stored as empty",
            base_id, collar_id, data.get("zone"),
        )
        zone = None

    ts = data.get("ts") or utc_now_iso()
    with db.connect() as conn:
        _ensure_collar(conn, collar_id, base_id)
        conn.execute(
            """
            INSERT INTO telemetry (base_id, edge_seq, collar_id, ts, lat, lon, zone,
                                   satellites, hdop, battery_pct, fence_version, rssi, snr)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                base_id, edge_seq, collar_id, ts,
                data.get("lat"), data.get("lon"), zone,
                data.get("satellites"), data.get("hdop"), data.get("battery_pct"),
                data.get("fence_version"), data.get("rssi"), data.get("snr"),
            ),
        )
        # Último estado conhecido. COALESCE preserva o valor anterior
        # quando o campo não vem no pacote — um pacote sem bateria não
        # apaga a última bateria conhecida.
        conn.execute(
            """
            UPDATE collars SET
                base_id = ?,
                fence_version_reported = COALESCE(?, fence_version_reported),
                last_zone = COALESCE(?, last_zone),
                last_lat = COALESCE(?, last_lat),
                last_lon = COALESCE(?, last_lon),
                battery_pct = COALESCE(?, battery_pct),
                rssi = COALESCE(?, rssi),
                snr = COALESCE(?, snr),
                last_seen = ?
            WHERE collar_id = ?
            """,
            (
                base_id, data.get("fence_version"), zone,
                data.get("lat"), data.get("lon"), data.get("battery_pct"),
                data.get("rssi"), data.get("snr"), ts, collar_id,
            ),
        )
        if invalid_zone:
            conn.execute(
                """
                INSERT INTO events (base_id, edge_seq, collar_id, kind, detail, ts)
                VALUES (?,?,?,'invalid_zone',?,?)
                """,
                (base_id, edge_seq, collar_id, f"zona desconhecida: {data.get('zone')!r}", ts),
            )
        row = dict(conn.execute("SELECT * FROM collars WHERE collar_id = ?", (collar_id,)).fetchone())

    return {
        "collar_id": collar_id,
        "ts": ts,
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "zone": zone,
        "satellites": data.get("satellites"),
        "hdop": data.get("hdop"),
        "battery_pct": data.get("battery_pct"),
        "fence_version": data.get("fence_version"),
        "rssi": data.get("rssi"),
        "snr": data.get("snr"),
        "collar": row,
    }


def _store_event(db: Database, base_id: str, edge_seq: int, data: dict) -> dict | None:
    """Grava um evento relatado pela coleira (ex.: mudança de zona)."""
    kind = data.get("kind")
    if not kind:
        logger.warning("base=%s event without kind (edge_seq=%s)", base_id, edge_seq)
        return None

    zone = (data.get("zone") or "").upper() or None
    if zone is not None and zone not in VALID_ZONES:
        zone = None
    ts = data.get("ts") or utc_now_iso()
    collar_id = data.get("collar_id")

    with db.connect() as conn:
        if collar_id:
            _ensure_collar(conn, collar_id, base_id)
        cursor = conn.execute(
            """
            INSERT INTO events (base_id, edge_seq, collar_id, kind, zone, detail, ts)
            VALUES (?,?,?,?,?,?,?)
            """,
            (base_id, edge_seq, collar_id, kind, zone, data.get("detail"), ts),
        )
        event_id = cursor.lastrowid

    logger.info("base=%s collar=%s event %s zone=%s", base_id, collar_id, kind, zone)
    return {
        "id": event_id,
        "collar_id": collar_id,
        "kind": kind,
        "zone": zone,
        "detail": data.get("detail"),
        "ts": ts,
    }


def _apply_delivery(
    db: Database, base_id: str, data: dict, active_version: int | None
) -> dict | None:
    """Repassa um relato de entrega para `delivery.apply_report()`."""
    collar_id = data.get("collar_id")
    fence_version = data.get("fence_version")
    status = data.get("status")
    if not collar_id or fence_version is None or not status:
        logger.warning("base=%s incomplete delivery report: %r", base_id, data)
        return None

    expected = None
    try:
        expected = fence_service.get_by_version(db, int(fence_version))["crc32"]
    except fence_service.FenceNotFound:
        logger.warning(
            "base=%s collar=%s delivery report for unknown fence=%s",
            base_id, collar_id, fence_version,
        )
        return None

    return delivery.apply_report(
        db,
        fence_version=int(fence_version),
        collar_id=collar_id,
        status=status,
        crc32_reported=data.get("crc32"),
        expected_crc32=expected,
        active_version=active_version,
        base_id=base_id,
    )
