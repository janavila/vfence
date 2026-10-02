"""Segredos do Central: como guardamos e conferimos tokens.

Por que este módulo existe
--------------------------
O banco guarda `bases.token_hash`, nunca o token em texto puro (seção 7
da especificação e seção 10 do planejamento). Se alguém copiar o arquivo
`data/vfence.db`, não leva o segredo da Base junto.

A escolha do algoritmo
----------------------
Para o token da Base usamos SHA-256 da biblioteca padrão. Vale explicar
por que isso basta aqui e NÃO basta para senha de usuário:

- O token da Base é um valor aleatório e longo, escolhido por nós. Não
  dá para adivinhá-lo por tentativa e erro, então o hash só precisa ser
  rápido e irreversível.
- Uma senha de pessoa é curta e previsível. Para ela é preciso um hash
  deliberadamente LENTO, que torna a tentativa e erro caríssima. Quando
  chegar a fase F8, a senha do produtor vai usar `hashlib.scrypt`, que
  também é da biblioteca padrão — ou seja, sem dependência nova.

A comparação usa `secrets.compare_digest` em vez de `==` porque o `==`
de strings pode terminar mais cedo na primeira letra diferente, e esse
tempo a mais ou a menos vaza informação sobre o segredo.

Onde se conecta
---------------
`db.py` chama `hash_token` ao cadastrar a Base a partir do `.env`;
`routers/edge.py` (fase F4) vai chamar `verify_token` em toda requisição
vinda da Base.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def hash_token(token: str) -> str:
    """Devolve o SHA-256 do token, em 64 dígitos hexadecimais minúsculos."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    """Confere um token recebido contra o hash guardado no banco."""
    return secrets.compare_digest(hash_token(token), expected_hash)


# ===========================================================================
# Senha do produtor e sessão em cookie (fase F8)
# ===========================================================================

# Parâmetros do scrypt. São os recomendados pela documentação do Python
# para uso interativo: custam cerca de 100 ms e ~64 MB de memória por
# conferência. O custo é o PONTO — é ele que torna a tentativa e erro
# inviável para quem roubar o hash.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16

SESSION_COOKIE = "vfence_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Gera o hash de uma senha com scrypt, no formato `scrypt$sal$hash`.

    Por que scrypt e não SHA-256 (ver a explicação no topo do módulo):
    senha de pessoa é curta e previsível, então o hash precisa ser
    deliberadamente LENTO. O scrypt também exige MEMÓRIA, o que atrapalha
    quem tentaria acelerar o ataque com placa de vídeo.

    O sal é aleatório e guardado junto: ele garante que duas pessoas com
    a mesma senha tenham hashes diferentes, impedindo o uso de tabelas
    pré-calculadas.

    Vem da biblioteca padrão — nenhuma dependência nova.
    """
    if salt is None:
        salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Confere uma senha contra o hash guardado."""
    try:
        algorithm, salt_hex, _ = stored.split("$", 2)
    except ValueError:
        return False
    if algorithm != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


# ---------------------------------------------------------------------------
# Sessão assinada
# ---------------------------------------------------------------------------
#
# A sessão é um cookie ASSINADO, sem nada guardado no servidor. O cookie
# carrega quem é o usuário e quando expira; a assinatura HMAC com o
# SESSION_SECRET prova que o conteúdo não foi alterado.
#
# Por que sem estado no servidor: não precisamos de tabela de sessões,
# não há o que limpar, e reiniciar o Central não desconecta o produtor no
# meio do desenho de uma cerca. A contrapartida é que não dá para
# invalidar uma sessão específica antes de ela expirar — aceitável para
# um sistema de usuário único (DEC-11) na rede local da propriedade.
#
# Trocar o SESSION_SECRET invalida TODAS as sessões de uma vez, o que é
# a saída se algum dia for preciso.


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def create_session_token(user: str, secret: str, lifetime_s: int) -> str:
    """Cria o valor do cookie de sessão: `dados.assinatura`."""
    payload = json.dumps(
        {"user": user, "exp": int(time.time()) + lifetime_s}, separators=(",", ":")
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload, secret)}"


def read_session_token(token: str, secret: str) -> str | None:
    """Devolve o usuário do cookie, ou `None` se inválido ou expirado.

    Confere a assinatura ANTES de olhar o conteúdo: sem isso, qualquer
    pessoa poderia escrever `{"user": "produtor"}` no próprio cookie e
    entrar. A comparação usa `compare_digest` pelo mesmo motivo de
    `verify_token`.
    """
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode(encoded + padding)
    except Exception:
        return None
    if not secrets.compare_digest(_sign(payload, secret), signature):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
        return None
    user = data.get("user")
    return user if isinstance(user, str) and user else None
