"""Entrada e saída das rotas de cerca."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PointIn(BaseModel):
    """Um ponto enviado pelo navegador.

    Sem limite de faixa de propósito: latitude 91 é recusada pela regra
    VAL-02, com mensagem em português. Ver a explicação no `__init__.py`
    deste pacote.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float


class PointOut(BaseModel):
    """Um ponto devolvido pelo Central.

    É sempre o ponto JÁ na forma canônica (microgradus ÷ 10⁶), para a
    tela mostrar exatamente o que a coleira vai receber.
    """

    lat: float
    lon: float


class FenceIn(BaseModel):
    """Corpo de `POST /api/fences` e de `POST /api/fences/validate`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    margin_attention_m: float
    margin_critical_m: float
    points: list[PointIn]
    accept_warnings: bool = False


class ViolationOut(BaseModel):
    """Uma regra que não passou (seção 5.4)."""

    rule: str
    severity: str
    message: str
    points: list[int] = Field(default_factory=list)
    edges: list[list[int]] = Field(default_factory=list)


class ValidationOut(BaseModel):
    """Resposta de `POST /api/fences/validate`."""

    valid: bool
    violations: list[ViolationOut]
    area_ha: float
    perimeter_m: float
    orientation: str | None


class FenceOut(BaseModel):
    """Uma versão de cerca, completa."""

    version: int
    name: str
    status: str
    margin_attention_m: float
    margin_critical_m: float
    points: list[PointOut]
    area_ha: float
    perimeter_m: float
    crc32: str
    warnings: list[ViolationOut]
    created_at: str
    reactivated_from: int | None = None


class FenceSummaryOut(BaseModel):
    """Uma linha do histórico. Sem os pontos, para a lista ficar leve."""

    version: int
    name: str
    status: str
    point_count: int
    area_ha: float
    perimeter_m: float
    crc32: str
    created_at: str
    reactivated_from: int | None = None


class DeliveryOut(BaseModel):
    """Situação da entrega de uma versão em uma coleira (seção 8).

    `status` é o nome técnico, usado pelo código e pela Base.
    `status_label` é o texto em português que aparece na tela, para a
    tradução viver em um lugar só do sistema.
    """

    fence_version: int
    collar_id: str
    status: str
    status_label: str
    attempts: int
    crc32_reported: str | None = None
    detail: str | None = None
    updated_at: str
