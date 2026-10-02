"""Entrada e saída das rotas usadas pela Base (contrato I2, seção 9)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HeartbeatIn(BaseModel):
    """`POST /api/edge/heartbeat` — a Base diz o que tem."""

    model_config = ConfigDict(extra="forbid")

    base_id: str
    fence_version: int | None = None
    serial: str | None = None
    queue_size: int | None = None


class HeartbeatOut(BaseModel):
    """A resposta: o que a Base DEVERIA ter, e a hora do servidor.

    `server_time` existe porque o Raspberry Pi 3 não tem relógio de
    bateria (seção 7.3 do planejamento): sem internet para o NTP, a hora
    do heartbeat é a melhor referência que a Base tem.
    """

    desired_fence_version: int | None
    server_time: str


class FenceForBaseOut(BaseModel):
    """`GET /api/edge/fences/{version}` — a cerca na forma canônica.

    Note que aqui NÃO vão graus: só inteiros. A Base, o gateway e a
    coleira trabalham apenas com microgradus e centímetros, para o CRC
    bater em todas as camadas (seção 5.6 do planejamento).
    """

    version: int
    margin_attention_cm: int
    margin_critical_cm: int
    points_e6: list[list[int]]
    crc32: str


class BatchItemIn(BaseModel):
    """Um item da fila local da Base.

    `data` é um dicionário livre de propósito: cada tipo de item tem
    campos diferentes, e o conteúdo é conferido em
    `services/edge_service.py`, que sabe tratar campo ausente sem
    derrubar o lote inteiro. Um modelo rígido aqui faria a Base levar
    422 e reenviar o mesmo lote para sempre.
    """

    model_config = ConfigDict(extra="forbid")

    edge_seq: int = Field(ge=0)
    type: Literal["telemetry", "delivery", "event"]
    data: dict[str, Any] = Field(default_factory=dict)


class BatchIn(BaseModel):
    """`POST /api/edge/batch` — a Base entrega o que acumulou."""

    model_config = ConfigDict(extra="forbid")

    base_id: str
    items: list[BatchItemIn] = Field(default_factory=list)


class BatchOut(BaseModel):
    """A resposta: até onde a Base pode limpar a fila local."""

    acked_up_to: int
