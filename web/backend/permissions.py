"""Browser authorization. Edge authentication is handled by its own router."""

from urllib.parse import urlsplit

from backend.security import SESSION_COOKIE, read_session_token

PRODUCER_HOME = "/produtor"
MANAGEMENT_HOME = "/gestao"


def role_for(settings, user):
    if user is None:
        return None
    if user == settings.admin_user:
        return "management"
    if settings.producer_user and user == settings.producer_user:
        return "producer"
    return None


def session_user(connection):
    settings = connection.app.state.settings
    if not settings.auth_enabled:
        return settings.admin_user
    user = read_session_token(connection.cookies.get(SESSION_COOKIE, ""), settings.session_secret)
    return user if role_for(settings, user) else None


def producer_path(path):
    return path == PRODUCER_HOME or path.startswith(("/produtor/", "/api/producer/"))


def browser_origin_matches(connection):
    origin = connection.headers.get("origin")
    if not origin:
        return True
    try:
        url = urlsplit(origin)
    except ValueError:
        return False
    return (url.scheme in {"http", "https"}
            and url.netloc.lower() == connection.headers.get("host", "").lower())
