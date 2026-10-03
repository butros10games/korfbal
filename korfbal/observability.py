"""Structured logs and error/performance reporting for the API and workers.

Every log line carries the request (or Celery task) it belongs to, so a Sentry
event, an access-log line and the application's own warnings can be joined on
`request_id`. Nothing here imports models: `LOGGING` is configured before the
app registry is ready.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber


if TYPE_CHECKING:
    from sentry_sdk._types import Event


_request_id: ContextVar[str | None] = ContextVar("korfbal_request_id", default=None)
_task: ContextVar[tuple[str, str] | None] = ContextVar("korfbal_task", default=None)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Standard `LogRecord` attributes; anything else was passed through `extra=`.
# Django passes the `HttpRequest` as `extra["request"]`; its repr is the full URL.
_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "request_id", "task_id", "task_name", "request"}
)
_EMAIL = re.compile(r"[^\s@<>()\[\]\"',;:]+@[^\s@<>()\[\]\"',;:]+\.[A-Za-z]{2,}")
UNMATCHED_ROUTE = "<unmatched>"


_URL = re.compile(r"\bhttps?://[^\s'\"<>]+")
# Celery's task-failure log passes the task's arguments (recipients, OTPs,
# activation links) as `extra["data"]["args"/"kwargs"]`.
_TASK_ARGUMENTS = ("args", "kwargs")


def _origin(match: re.Match[str]) -> str:
    # Logged URLs come from request headers; a malformed one must not turn a
    # logged rejection (a CSRF 403) into a server error.
    try:
        parts = urlsplit(match.group(0))
    except ValueError:
        return "[url]"
    return f"{parts.scheme}://{parts.netloc}"


def redact(text: str) -> str:
    """Drop email addresses and URL paths/queries, which logs often interpolate.

    URLs keep only their origin: paths and queries can carry activation and
    reset tokens (a CSRF failure quotes the full Referer, for example).

    Returns:
        The redacted text.

    """
    return _URL.sub(_origin, _EMAIL.sub("[email]", text))


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def safe_extras(extras: Mapping[str, object]) -> dict[str, object]:
    """Drop request objects and task arguments from `extra=` fields; redact the rest.

    Returns:
        The fields that are safe to ship.

    """
    safe: dict[str, object] = {}
    for key, value in extras.items():
        if key == "request":
            continue
        if key == "data" and isinstance(value, Mapping):
            safe[key] = _redact_value({
                name: item
                for name, item in value.items()
                if name not in _TASK_ARGUMENTS
            })
        else:
            safe[key] = _redact_value(value)
    return safe


def valid_request_id(value: str | None) -> str | None:
    """Accept a caller-supplied request ID only when it is safe to log verbatim."""
    if value and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return None


def current_request_id() -> str | None:
    """Return the request ID bound to the running request, if any."""
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """Attach a request ID to log records emitted inside the block."""
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


TaskToken = Token[tuple[str, str] | None]


def bind_task(task_id: str, task_name: str) -> TaskToken:
    """Attach a Celery task to log records until `unbind_task` is called."""
    return _task.set((task_id, task_name))


def unbind_task(token: TaskToken | None) -> None:
    """Restore the task context that was active before `bind_task`."""
    if token is None:
        _task.set(None)
        return
    try:
        _task.reset(token)
    except ValueError:
        # Celery can run postrun in a different context; clearing is still safe.
        _task.set(None)


class RequestRouteFilter(logging.Filter):
    """Replace the raw request path in request/CSRF log messages by its route.

    Other string arguments (a CSRF failure's Referer) are redacted as well.

    Installed on the logger rather than a handler so Sentry's logging
    integration, which captures before handlers run, sees the rewritten record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the path argument; never drops the record."""
        request = getattr(record, "request", None)
        path = getattr(request, "path", None)
        if path and isinstance(record.args, tuple):
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None) or UNMATCHED_ROUTE
            record.args = tuple(
                route if arg == path else redact(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


class ContextFilter(logging.Filter):
    """Copy the active request/task identifiers onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Annotate the record; never drops it."""
        record.request_id = _request_id.get()
        task = _task.get()
        record.task_id, record.task_name = task or (None, None)
        return True


def _json_default(value: object) -> str:
    return str(value)


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line for the log collector."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record with its context and `extra=` fields."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("request_id", "task_id", "task_name"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        payload.update(
            safe_extras({
                key: value
                for key, value in record.__dict__.items()
                if key not in _RECORD_ATTRIBUTES and not key.startswith("_")
            })
        )
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=_json_default, ensure_ascii=False)


def logging_config(*, log_format: str, level: str) -> dict[str, Any]:
    """Build Django's `LOGGING` dictionary.

    Args:
        log_format: `json` for collectors, `text` for local terminals.
        level: Root log level.

    Returns:
        A `logging.config.dictConfig` dictionary.

    """
    formatter = "json" if log_format == "json" else "text"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context": {"()": "korfbal.observability.ContextFilter"},
            "request_route": {"()": "korfbal.observability.RequestRouteFilter"},
        },
        "formatters": {
            "json": {"()": "korfbal.observability.JsonFormatter"},
            "text": {
                "format": "%(asctime)s %(levelname)s %(name)s "
                "[%(request_id)s] %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "filters": ["context"],
                "formatter": formatter,
            },
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            # The access log already records every 4xx; Django would repeat it.
            "django.request": {"level": "ERROR", "filters": ["request_route"]},
            "django.security.csrf": {"filters": ["request_route"]},
            # Granian/uvicorn-style access chatter stays off; the middleware logs.
            "django.server": {"level": "WARNING"},
            "django.db.backends": {"level": "WARNING"},
        },
    }


# --- Sentry -------------------------------------------------------------------

# Field names Sentry's default scrubber does not cover but this API uses.
SCRUBBED_FIELDS = [
    "otp",
    "code",
    "token",
    "access",
    "refresh",
    "reauthentication_token",
    "credential",
    "assertion",
    "uidb64",
    "client_data_json",
    "attestation_object",
    "authenticator_data",
    "signature",
    "user_handle",
    "push_token",
    "endpoint",
    "p256dh",
]


def traces_sampler(
    sampling_context: Mapping[str, Any], *, default_rate: float
) -> float:
    """Follow the client's decision and skip endpoints that are not useful to trace."""
    parent = sampling_context.get("parent_sampled")
    if parent is not None:
        return float(parent)
    path = ""
    wsgi_environ = sampling_context.get("wsgi_environ")
    if isinstance(wsgi_environ, Mapping):
        path = str(wsgi_environ.get("PATH_INFO", ""))
    asgi_scope = sampling_context.get("asgi_scope")
    if isinstance(asgi_scope, Mapping):
        path = str(asgi_scope.get("path", ""))
    if path.startswith("/metrics") or path.endswith("/events/"):
        return 0.0
    return default_rate


# Request headers worth keeping; the rest (Referer, Authorization, cookies,
# forwarded addresses) can carry tokens or personal data.
_KEPT_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "accept-language",
    "content-length",
    "content-type",
    "host",
    "origin",
    "user-agent",
    "x-request-id",
})
# Span and breadcrumb fields that hold a query string or fragment.
_QUERY_KEYS = ("http.query", "http.fragment", "url.query", "url.fragment")


def _without_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _sanitize_request(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    for key in ("query_string", "cookies", "data", "env"):
        request.pop(key, None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {
            name: value
            for name, value in headers.items()
            if name.lower() in _KEPT_HEADERS
        }
    url = request.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            request.pop("url")
            return
        route = event.get("transaction")
        source = (event.get("transaction_info") or {}).get("source")
        path = route if source == "route" and isinstance(route, str) else None
        request["url"] = (
            f"{parts.scheme}://{parts.netloc}/{(path or UNMATCHED_ROUTE).lstrip('/')}"
        )


def _sanitize_transaction_name(event: dict[str, Any]) -> None:
    # Unresolved paths fall back to the raw path (tokens, IDs); routes are safe.
    info = event.get("transaction_info") or {}
    if "transaction" in event and info.get("source") == "url":
        event["transaction"] = UNMATCHED_ROUTE
        event["transaction_info"] = {**info, "source": "custom"}


def _strip_query_data(data: object) -> None:
    if not isinstance(data, dict):
        return
    for key in _QUERY_KEYS:
        data.pop(key, None)
    for key in ("url", "http.url", "url.full"):
        if isinstance(data.get(key), str):
            data[key] = _without_query(data[key])


def sanitize_event(event: Event, _hint: object = None) -> Event:
    """Drop URLs, headers and log text that can carry tokens or personal data.

    Used for errors and transactions alike: both carry the request, and
    transactions are not passed through `before_send`.

    Returns:
        The same event, sanitized in place.

    """
    data = cast("dict[str, Any]", event)
    _sanitize_request(data)
    _sanitize_transaction_name(data)
    trace = (data.get("contexts") or {}).get("trace")
    if isinstance(trace, dict):
        _strip_query_data(trace.get("data"))
    for span in data.get("spans") or ():
        if isinstance(span, dict):
            _strip_query_data(span.get("data"))
            if isinstance(span.get("description"), str):
                span["description"] = _without_query(span["description"])
    breadcrumbs = data.get("breadcrumbs")
    values = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in values or ():
        sanitize_breadcrumb(crumb)
    for key in ("logentry", "exception", "message"):
        if key in data:
            data[key] = _redact_value(data[key])
    # Logging integration copies `extra=` fields here (request objects, task args).
    if isinstance(data.get("extra"), dict):
        data["extra"] = safe_extras(data["extra"])
    return event


def sanitize_breadcrumb(crumb: dict[str, Any], _hint: object = None) -> dict[str, Any]:
    """Redact addresses, URLs, request objects and task arguments from a breadcrumb.

    Returns:
        The same breadcrumb, sanitized in place.

    """
    if isinstance(crumb.get("message"), str):
        crumb["message"] = redact(crumb["message"])
    data = crumb.get("data")
    _strip_query_data(data)
    if isinstance(data, dict):
        # Logging breadcrumbs carry the record's `extra=` fields.
        crumb["data"] = safe_extras(data)
    return crumb


@dataclass(frozen=True)
class SentryOptions:
    """Deployment-specific Sentry configuration."""

    dsn: str
    environment: str
    release: str = ""
    traces_sample_rate: float = 0.0
    profiles_sample_rate: float = 0.0
    monitor_beat_tasks: bool = False


def init_sentry(options: SentryOptions) -> bool:
    """Start error and performance reporting when a DSN is configured.

    Returns:
        Whether Sentry was initialized.

    """
    if not options.dsn:
        return False
    sentry_sdk.init(
        dsn=options.dsn,
        environment=options.environment,
        release=options.release or None,
        # No IP addresses, cookies or user details unless explicitly set.
        send_default_pii=False,
        max_request_body_size="never",
        # Frame locals hold parsed request bodies (passwords, OTPs, passkeys).
        include_local_variables=False,
        before_send=sanitize_event,
        before_send_transaction=sanitize_event,
        before_breadcrumb=sanitize_breadcrumb,
        event_scrubber=EventScrubber(
            denylist=[*DEFAULT_DENYLIST, *SCRUBBED_FIELDS], recursive=True
        ),
        traces_sampler=lambda context: traces_sampler(
            context, default_rate=options.traces_sample_rate
        ),
        profiles_sample_rate=options.profiles_sample_rate,
        integrations=[
            DjangoIntegration(transaction_style="url", middleware_spans=False),
            CeleryIntegration(monitor_beat_tasks=options.monitor_beat_tasks),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
    return True
