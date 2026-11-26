"""Menu e rota de retorno das telas da Central."""

from html import escape


MENUS = {
    "visitor": (),
    "producer": (
        ("herd", "/produtor", "Meu Rebanho"),
        ("fences", "/produtor/cercas", "Minhas Cercas"),
    ),
    "management": (
        ("herd", "/gestao", "Rebanho"),
        ("fences", "/gestao/cercas", "Cercas"),
        ("overview", "/gestao/painel", "Painel técnico"),
        ("history", "/gestao/historico", "Histórico"),
        ("events", "/gestao/eventos", "Eventos"),
    ),
}


def header(area: str, page: str, back: str, *, indicator: bool = False) -> str:
    items = []
    for name, url, label in MENUS[area]:
        current = ' aria-current="page"' if name == page else ""
        items.append(f'<a href="{url}"{current}>{label}</a>')
    if area in {"producer", "management"}:
        label = "Produtor" if area == "producer" else "Gestão"
        return (
            f'<header class="visitor-header producer-header" data-area="{area}">'
            '<div class="visitor-container visitor-header-inner">'
            f'<div class="visitor-brand"><img src="/static/VFence.png" alt="VFence"><span>{label}</span></div>'
            f'<nav class="producer-nav" aria-label="{("Navegação do Produtor" if area == "producer" else "Navegação da Gestão")}">{"".join(items)}</nav>'
            '<button class="button visitor-back" type="button" id="logout-button">Sair</button>'
            '</div></header>'
        )
    status = (
        '<span class="base-indicator" id="base-indicator" data-state="unknown" aria-live="polite">'
        '<i aria-hidden="true"></i><span id="base-indicator-text">Verificando…</span></span>'
    ) if indicator else ""
    logout = (
        '<button class="logout-button" type="button" id="logout-button">Sair</button>'
    ) if area != "visitor" else ""
    return (
        f'<header class="app-header area-header" data-area="{area}">'
        '<div class="header-inner">'
        f'<a class="area-back" data-back href="{escape(back, quote=True)}">'
        '<span aria-hidden="true">←</span> Voltar</a>'
        '<a class="area-brand" href="/"><img src="/static/VFence.png" alt="">VFence</a>'
        f'<nav class="primary-nav" aria-label="Navegação da área">{"".join(items)}</nav>'
        f'{status}{logout}</div></header>'
    )
