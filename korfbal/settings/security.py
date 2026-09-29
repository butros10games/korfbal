"""Host / CORS / CSRF / security settings."""

from __future__ import annotations

from urllib.parse import urlparse

from .env import env, env_bool, env_int, env_list, sorted_hosts
from .runtime import DEBUG


CSRF_FAILURE_VIEW = "korfbal.api_errors.csrf_failure"

KORFBAL_ORIGIN = "https://api.korfbal.butrosgroot.com"
WEB_KORFBAL_ORIGIN = "https://korfbal.butrosgroot.com"
# KorfConnect domain served by the same deployment alongside the original one.
KORFCONNECT_ORIGIN = "https://api.korfconnect.nl"
WEB_KORFCONNECT_ORIGIN = "https://korfconnect.nl"
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


def merge_origins(*origins: str) -> list[str]:
    """Return a stable list with every hostname variant for the given origins."""
    merged: set[str] = set()
    for origin in origins:
        merged.update(origin_variants(origin))
    return sorted_hosts(list(merged))


WEB_APP_ORIGIN = env(
    "WEB_APP_ORIGIN",
    WEB_KWT_ORIGIN if DEBUG else WEB_KORFBAL_ORIGIN,
).rstrip("/")

default_hosts = (
    "korfbal.butrosgroot.com,api.korfbal.butrosgroot.com,"
    "korfconnect.nl,www.korfconnect.nl,api.korfconnect.nl"
)
ALLOWED_HOSTS = sorted_hosts(env_list("ALLOWED_HOSTS", default_hosts))

_default_csrf_trusted = ",".join([
    KORFBAL_ORIGIN,
    *origin_variants(WEB_KORFBAL_ORIGIN),
    KORFCONNECT_ORIGIN,
    *origin_variants(WEB_KORFCONNECT_ORIGIN),
])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", _default_csrf_trusted)

_default_cors_allowed = ",".join([
    *origin_variants(WEB_KORFBAL_ORIGIN),
    *origin_variants(WEB_KORFCONNECT_ORIGIN),
])
CORS_ALLOWED_ORIGINS = sorted_hosts(
    env_list("CORS_ALLOWED_ORIGINS", _default_cors_allowed),
)
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", False)
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
    CORS_ALLOWED_ORIGINS = merge_origins(*CORS_ALLOWED_ORIGINS, WEB_KORFBAL_ORIGIN)
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
# Peers allowed to supply X-Real-IP / X-Forwarded-* headers. The app ports are
# not published publicly, so only the edge proxy on a private network reaches
# them; forwarded headers from any other peer are stripped by middleware.
KORFBAL_TRUSTED_PROXIES = env_list(
    "KORFBAL_TRUSTED_PROXIES",
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7",
)

SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_NAME = env("SESSION_COOKIE_NAME", "sessionid")
CSRF_COOKIE_VERSION = env("CSRF_COOKIE_VERSION", "")
_default_csrf_cookie_name = "csrftoken" if DEBUG else "korfbal_csrftoken"
_version_suffix = f"_v{CSRF_COOKIE_VERSION}" if CSRF_COOKIE_VERSION else ""
CSRF_COOKIE_NAME = env(
    "CSRF_COOKIE_NAME",
    f"{_default_csrf_cookie_name}{_version_suffix}",
)
_cookie_domain_default = ".korfbal.butrosgroot.com" if not DEBUG else ""
_csrf_cookie_domain = env("CSRF_COOKIE_DOMAIN", _cookie_domain_default).strip()
_session_cookie_domain = env("SESSION_COOKIE_DOMAIN", _cookie_domain_default).strip()
CSRF_COOKIE_DOMAIN = _csrf_cookie_domain or None
SESSION_COOKIE_DOMAIN = _session_cookie_domain or None
# Parent domains the deployment answers on. A cookie scoped to one of them is
# re-scoped to the domain of the requesting host, because browsers reject a
# `.korfbal.butrosgroot.com` cookie set by `api.korfconnect.nl`.
_default_cookie_domains = "" if DEBUG else ".korfbal.butrosgroot.com,.korfconnect.nl"
KORFBAL_COOKIE_DOMAINS = env_list("KORFBAL_COOKIE_DOMAINS", _default_cookie_domains)
X_FRAME_OPTIONS = env("X_FRAME_OPTIONS", "SAMEORIGIN")
