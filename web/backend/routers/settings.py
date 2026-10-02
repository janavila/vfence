"""GET /api/settings — o que o navegador precisa saber da configuração.

Esta rota NÃO está na seção 6 da especificação
----------------------------------------------
Foi acrescentada porque o editor não funciona sem ela, e a seção 6 não
previu nenhuma forma de o navegador descobrir:

- `BASE_LAT` e `BASE_LON`, para abrir o mapa no lugar certo e marcar a
  Base (exigência explícita da seção 11.3);
- as margens padrão, que a seção 11.3 manda pré-preencher nos campos;
- os limites das regras VAL-03, VAL-07, VAL-10 e VAL-12, para o
  `geometry.js` repetir no navegador exatamente as contas do servidor.

A alternativa seria escrever esses valores dentro do HTML na mão, o que
faria o `.env` e a tela saírem de sincronia no primeiro ajuste de margem.

É uma rota NOVA, não uma alteração de contrato existente: nenhuma rota
da seção 6 mudou de forma.

Segurança
---------
Devolve apenas o que a tela usa. `BASE_TOKEN`, `ADMIN_PASSWORD` e
`SESSION_SECRET` nunca saem daqui — e existe um teste que falha se
alguém acrescentar um deles por descuido.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["configuracao"])


class SettingsOut(BaseModel):
    """Configuração visível ao navegador."""

    base_id: str
    base_lat: float
    base_lon: float
    base_offline_after_s: int
    margin_attention_default_m: float
    margin_critical_default_m: float
    base_max_radius_km: float
    lora_range_m: float
    gnss_expected_error_m: float
    gnss_safety_factor: float
    recommended_critical_margin_m: float
    known_collars: list[str]
    min_points: int
    max_points: int


@router.get("/settings", response_model=SettingsOut, summary="Configuração para a tela")
async def settings_route(request: Request) -> dict:
    """Devolve a configuração que o editor e o painel precisam."""
    settings = request.app.state.settings
    return {
        "base_id": settings.base_id,
        "base_lat": settings.base_lat,
        "base_lon": settings.base_lon,
        "base_offline_after_s": settings.base_offline_after_s,
        "margin_attention_default_m": settings.margin_attention_default_m,
        "margin_critical_default_m": settings.margin_critical_default_m,
        "base_max_radius_km": settings.base_max_radius_km,
        "lora_range_m": settings.lora_range_m,
        "gnss_expected_error_m": settings.gnss_expected_error_m,
        "gnss_safety_factor": settings.gnss_safety_factor,
        "recommended_critical_margin_m": settings.recommended_critical_margin_m,
        "known_collars": list(settings.known_collars),
        "min_points": 3,
        "max_points": 32,
    }
