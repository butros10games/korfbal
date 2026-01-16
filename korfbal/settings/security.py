"""Host / CORS / CSRF / security settings."""

from __future__ import annotations

from urllib.parse import urlparse

from .env import env, env_bool, env_int, env_list, sorted_hosts
from .runtime import DEBUG


KORFBAL_ORIGIN = "https://api.korfbal.butrosgroot.com"
WEB_KORFBAL_ORIGIN = "https://korfbal.butrosgroot.com"
KWT_ORIGIN = "https://api.korfbal.localhost"
WEB_KWT_ORIGIN = "https://korfbal.localhost"


def origin_variants(origin: str) -> list[str]:
    """Return predictable origin variants (e.g. `www.`/`web.`).

    Helps keep CORS/CSRF configuration robust when the SPA is served from
    multiple hostnames.
    """
    origin = (origin or "").strip().rstrip("/")
    if not origin:
        return []

    parsed = urlparse(origin)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return [origin]

    base = netloc
    for prefix in ("www.", "web."):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break

    variants = {
        f"{scheme}://{base}",
        f"{scheme}://www.{base}",
        f"{scheme}://web.{base}",
        origin,
    }

    return sorted(v for v in variants if v)


WEB_APP_ORIGIN = env(
    "WEB_APP_ORIGIN",
    WEB_KWT_ORIGIN if DEBUG else WEB_KORFBAL_ORIGIN,
).rstrip("/")

default_hosts = "korfbal.butrosgroot.com,api.korfbal.butrosgroot.com"
ALLOWED_HOSTS = sorted_hosts(env_list("ALLOWED_HOSTS", default_hosts))

_default_csrf_trusted = ",".join([
    KORFBAL_ORIGIN,
    *origin_variants(WEB_KORFBAL_ORIGIN),
])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", _default_csrf_trusted)

_default_cors_allowed = ",".join(origin_variants(WEB_KORFBAL_ORIGIN))
CORS_ALLOWED_ORIGINS = sorted_hosts(
    env_list("CORS_ALLOWED_ORIGINS", _default_cors_allowed),
)
CORS_ALLOW_CREDENTIALS = env_bool("CORS_ALLOW_CREDENTIALS", True)

if DEBUG:
    ALLOWED_HOSTS = sorted_hosts([
        *ALLOWED_HOSTS,
        "localhost",
        "127.0.0.1",
        "korfbal.localhost",
        "api.korfbal.localhost",
        "web.korfbal.localhost",
        "kwt.localhost",
        "api.kwt.localhost",
        "web.kwt.localhost",
        "bg.localhost",
    ])
    CSRF_TRUSTED_ORIGINS = sorted({*CSRF_TRUSTED_ORIGINS, KWT_ORIGIN, WEB_KWT_ORIGIN})
    CORS_ALLOWED_ORIGINS = sorted_hosts([
        *CORS_ALLOWED_ORIGINS,
        WEB_KWT_ORIGIN,
        KWT_ORIGIN,
        "http://localhost:4173",
        "http://localhost:5173",
        # Expo web dev server (React Native for Web)
        "http://localhost:19006",
        # Metro bundler/dev server ports that might host the web UI
        "http://localhost:8081",
        "http://localhost:3000",
    ])

SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
_cookie_domain_default = ".korfbal.butrosgroot.com" if not DEBUG else ""
_csrf_cookie_domain = env("CSRF_COOKIE_DOMAIN", _cookie_domain_default).strip()
_session_cookie_domain = env("SESSION_COOKIE_DOMAIN", _cookie_domain_default).strip()
CSRF_COOKIE_DOMAIN = _csrf_cookie_domain or None
SESSION_COOKIE_DOMAIN = _session_cookie_domain or None
X_FRAME_OPTIONS = env("X_FRAME_OPTIONS", "SAMEORIGIN")
