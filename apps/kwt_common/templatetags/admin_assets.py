"""Content-version local admin sources served from the immutable static bucket."""

from functools import cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static


register = template.Library()


@cache
def _asset_digest(path: str) -> str:
    """Read the app source once per process, outside remote storage."""
    source = finders.find(path)
    if isinstance(source, str):
        return sha256(Path(source).read_bytes()).hexdigest()[:16]
    return ""


@register.simple_tag
def admin_asset(path: str) -> str:
    """Refresh immutable browser caches whenever an app-owned asset changes."""
    url = static(path)
    digest = _asset_digest(path)
    if not digest:
        return url
    parts = urlsplit(url)
    query = urlencode([*parse_qsl(parts.query), ("v", digest)])
    return urlunsplit(parts._replace(query=query))
