"""Configuração do Central: lê o .env e entrega um objeto Settings.

O que faz
---------
Transforma o arquivo de texto `.env` (seção 14 da especificação) em um
objeto Python com os tipos certos: número é número, caminho é `Path`,
lista separada por vírgula é `tuple`. Também confere, já na subida, se
os valores fazem sentido — é muito melhor o servidor recusar a subir com
uma mensagem clara do que funcionar errado por semanas.

Duas escolhas que valem explicação
----------------------------------
1. **Função em vez de valores no corpo da classe.** O Monitor declara
   `Settings` com `os.getenv(...)` direto no valor padrão de cada campo
   (`app/config.py:15`). Python avalia aqueles `os.getenv` UMA vez, no
   momento em que o módulo é importado. O efeito colateral aparece nos
   testes do Monitor, que precisam mexer em `os.environ` ANTES do
   `import` para funcionar (`tests/test_api.py:1-3`). Aqui usamos uma
   função `load_settings()`, chamada quando o app é criado: os testes
   apontam o `DATA_DIR` para uma pasta temporária sem depender de ordem
   de import.

2. **Nomes das variáveis do .env em português, nomes dos campos em
   inglês.** As variáveis (`MARGEM_ATENCAO_PADRAO_M`) são copiadas sem
   alteração da especificação, porque o responsável pela Base e o
   contrato da fase F7 vão ler esses nomes. Já os campos do código
   (`margin_attention_default_m`) seguem a regra do projeto: código em
   inglês, `snake_case`. Este módulo é a fronteira entre os dois mundos,
   e é o único lugar onde os nomes em português aparecem.

Onde se conecta
---------------
`main.py` chama `load_settings()` ao criar o app e guarda o resultado em
`app.state.settings`, de onde as rotas o acessam. `db.py` usa os campos
da Base e das coleiras para o cadastro inicial.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Pasta web/ — raiz do Central. Usada para resolver caminhos relativos
# do .env (como DATA_DIR=./data) sempre em relação ao projeto, e não ao
# diretório de onde o comando foi executado.
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"

logger = logging.getLogger("config")


class ConfigError(RuntimeError):
    """Um valor do .env está ausente ou inválido."""


# --------------------------------------------------------------------------
# Conversores: cada um lê uma variável de ambiente e devolve o tipo certo,
# com uma mensagem de erro que diz QUAL variável está errada.
# --------------------------------------------------------------------------


def _flag(name: str, default: bool) -> bool:
    """Lê um valor de liga/desliga do .env, aceitando as formas usuais."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _text(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _secret(name: str, default: str) -> str:
    """Lê um segredo, distinguindo "não definido" de "definido como vazio".

    O `_text()` acima trata valor vazio como ausente e devolve o padrão.
    Para a maioria das variáveis isso é conveniente; para a SENHA é
    perigoso: quem escrevesse `ADMIN_PASSWORD=` no `.env` — querendo
    justamente não ter senha — receberia de volta a senha de exemplo
    `troque-esta-senha`, que funciona. O sistema ficaria acessível com uma
    senha pública, e o `.env` diria o contrário.

    Aqui, variável AUSENTE usa o padrão; variável PRESENTE e vazia volta
    vazia, e a conferência em `_check()` recusa a subida.
    """
    value = os.getenv(name)
    return default if value is None else value.strip()


def _integer(name: str, default: int) -> int:
    raw = _text(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} precisa ser um número inteiro (recebido: {raw!r})") from exc


def _decimal(name: str, default: float) -> float:
    raw = _text(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} precisa ser um número (recebido: {raw!r})") from exc


def _id_list(name: str, default: str) -> tuple[str, ...]:
    """Lê uma lista separada por vírgula, como COLARES_CONHECIDOS=COL01,COL02.

    Remove espaços e itens vazios, e preserva a ordem sem repetir.
    """
    raw = _text(name, default)
    seen: list[str] = []
    for item in raw.split(","):
        collar_id = item.strip().upper()
        if collar_id and collar_id not in seen:
            seen.append(collar_id)
    return tuple(seen)


# --------------------------------------------------------------------------
# O objeto de configuração
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuração do Central, já validada.

    `frozen=True` deixa o objeto imutável: nenhuma parte do sistema
    consegue mudar a configuração em tempo de execução por acidente.
    """

    # Servidor
    app_host: str
    app_port: int
    log_level: str
    data_dir: Path

    # Base de desenvolvimento
    base_id: str
    base_token: str
    base_lat: float
    base_lon: float
    base_offline_after_s: int
    known_collars: tuple[str, ...]

    # Regras da cerca
    margin_attention_default_m: float
    margin_critical_default_m: float
    base_max_radius_km: float
    lora_range_m: float
    gnss_expected_error_m: float
    gnss_safety_factor: float

    # Login (fase F8)
    auth_enabled: bool
    admin_user: str
    admin_password: str
    session_secret: str

    # ---- caminhos derivados -------------------------------------------
    # Não vêm do .env: são calculados a partir de DATA_DIR, para que
    # exista um lugar só definindo onde ficam banco e logs.

    @property
    def db_path(self) -> Path:
        """Arquivo do banco SQLite."""
        return self.data_dir / "vfence.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        """Arquivo de log do serviço (seção 10 da especificação)."""
        return self.log_dir / "central.log"

    @property
    def recommended_critical_margin_m(self) -> float:
        """Folga de margem crítica recomendada pelo erro do GNSS (VAL-12).

        Com os valores provisórios (erro 3,0 m × fator 1,5) dá 4,5 m — o
        mesmo número que aparece no exemplo de log da seção 10.
        """
        return self.gnss_expected_error_m * self.gnss_safety_factor


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Lê o .env mais as variáveis de ambiente e devolve um Settings validado.

    `env_file=None` pula a leitura do arquivo e usa só o ambiente — é o
    que os testes fazem, para não depender do `.env` de quem está
    rodando (`tests/conftest.py`).

    Precedência: variável de ambiente já definida ganha do arquivo. Isso
    é o padrão do `load_dotenv` e é o comportamento desejado, porque
    permite sobrescrever um valor numa execução pontual, por exemplo:

        APP_PORT=9000 uvicorn backend.main:app
    """
    if env_file is not None and env_file.exists():
        load_dotenv(env_file, override=False)

    data_dir = Path(_text("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = (PROJECT_DIR / data_dir).resolve()

    settings = Settings(
        app_host=_text("APP_HOST", "0.0.0.0"),
        app_port=_integer("APP_PORT", 8000),
        log_level=_text("LOG_LEVEL", "INFO").upper(),
        data_dir=data_dir,
        base_id=_text("BASE_ID", "BASE01").upper(),
        base_token=_text("BASE_TOKEN", "troque-este-token"),
        base_lat=_decimal("BASE_LAT", -31.306200),
        base_lon=_decimal("BASE_LON", -54.063950),
        base_offline_after_s=_integer("BASE_OFFLINE_APOS_S", 30),
        known_collars=_id_list("COLARES_CONHECIDOS", "COL01,COL02"),
        margin_attention_default_m=_decimal("MARGEM_ATENCAO_PADRAO_M", 5.0),
        margin_critical_default_m=_decimal("MARGEM_CRITICA_PADRAO_M", 2.0),
        base_max_radius_km=_decimal("BASE_RAIO_MAX_KM", 5),
        lora_range_m=_decimal("LORA_ALCANCE_M", 200),
        gnss_expected_error_m=_decimal("GNSS_ERRO_ESPERADO_M", 3.0),
        gnss_safety_factor=_decimal("GNSS_FATOR_SEGURANCA", 1.5),
        auth_enabled=_flag("AUTH_ENABLED", True),
        admin_user=_text("ADMIN_USER", "produtor"),
        admin_password=_secret("ADMIN_PASSWORD", "troque-esta-senha"),
        session_secret=_secret("SESSION_SECRET", "troque-esta-chave"),
    )
    _check(settings)
    return settings


def _check(settings: Settings) -> None:
    """Recusa configurações impossíveis já na subida do servidor.

    A ideia é falhar alto e cedo: um BASE_LAT digitado como 31.3 em vez
    de -31.3 coloca a Base no hemisfério norte e faria TODA cerca do
    produtor ser recusada pela regra VAL-03, com uma mensagem confusa.
    É muito mais barato recusar aqui.
    """
    if not -90 <= settings.base_lat <= 90:
        raise ConfigError(f"BASE_LAT precisa estar entre -90 e 90 (recebido: {settings.base_lat})")
    if not -180 <= settings.base_lon <= 180:
        raise ConfigError(f"BASE_LON precisa estar entre -180 e 180 (recebido: {settings.base_lon})")
    if not settings.base_id:
        raise ConfigError("BASE_ID não pode ficar vazio")
    if not settings.base_token:
        raise ConfigError("BASE_TOKEN não pode ficar vazio")

    # Mesma relação que a regra VAL-08 exige das margens de cada cerca:
    # se os PADRÕES já estiverem errados, o editor abriria sempre inválido.
    attention = settings.margin_attention_default_m
    critical = settings.margin_critical_default_m
    if not 0 < critical < attention:
        raise ConfigError(
            "as margens padrão precisam satisfazer "
            "0 < MARGEM_CRITICA_PADRAO_M < MARGEM_ATENCAO_PADRAO_M "
            f"(recebido: crítica={critical}, atenção={attention})"
        )

    if settings.base_max_radius_km <= 0:
        raise ConfigError("BASE_RAIO_MAX_KM precisa ser maior que zero")
    if settings.gnss_expected_error_m <= 0:
        raise ConfigError("GNSS_ERRO_ESPERADO_M precisa ser maior que zero")
    if settings.gnss_safety_factor <= 0:
        raise ConfigError("GNSS_FATOR_SEGURANCA precisa ser maior que zero")
    if settings.base_offline_after_s <= 0:
        raise ConfigError("BASE_OFFLINE_APOS_S precisa ser maior que zero")

    # Com o login ligado, os três valores de acesso precisam existir de
    # verdade. Subir com senha vazia deixaria o sistema aberto sem que
    # ninguém percebesse.
    if settings.auth_enabled:
        if not settings.admin_user:
            raise ConfigError("ADMIN_USER não pode ficar vazio com AUTH_ENABLED=true")
        if not settings.admin_password:
            raise ConfigError("ADMIN_PASSWORD não pode ficar vazio com AUTH_ENABLED=true")
        if not settings.session_secret:
            raise ConfigError("SESSION_SECRET não pode ficar vazio com AUTH_ENABLED=true")

        # Os valores de exemplo FUNCIONAM, de propósito: o sistema precisa
        # rodar logo após `cp .env.example .env`. Mas ninguém pode chegar
        # ao ensaio de campo sem perceber que a senha é pública, então o
        # aviso é alto e aparece em toda subida.
        if settings.admin_password == "troque-esta-senha":
            logger.warning(
                "ADMIN_PASSWORD ainda e a senha de exemplo do .env.example: "
                "troque antes de usar fora da bancada"
            )
        if settings.session_secret == "troque-esta-chave":
            logger.warning(
                "SESSION_SECRET ainda e a chave de exemplo do .env.example: "
                "gere uma com python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
