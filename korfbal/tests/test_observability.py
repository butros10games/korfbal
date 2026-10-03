"""Structured logs, request correlation and Sentry privacy defaults."""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
import io
import json
import logging
from pathlib import Path
from typing import Any
from unittest import mock
import uuid
from wsgiref.util import setup_testing_defaults

from bg_auth.tasks import send_2fa_email_task, send_confirmation_email_task
from django.core.mail import EmailMultiAlternatives
from django.core.wsgi import get_wsgi_application
from django.http import HttpRequest, HttpResponse
from django.test import Client, override_settings
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
import yaml

from korfbal import (
    celery as celery_module,
    observability,
)


GENERATED_REQUEST_ID_LENGTH = 32
# Defined far from the failing view so Sentry's source context cannot show them.
SUBMITTED_SECRET = "hunter2-" + "secret"
SUBMITTED_CODE = "918" + "273"
ELAPSED_MS = 12


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.getLogger("korfbal.test").makeRecord(
        "korfbal.test", logging.WARNING, __file__, 1, message, (), None, extra=extra
    )
    observability.ContextFilter().filter(record)
    return record


def test_json_lines_carry_request_context_and_extra_fields() -> None:
    """A collector can join any line to its request without parsing free text."""
    with observability.bind_request_id("req-12345678"):
        line = observability.JsonFormatter().format(_record(match="m1", ms=ELAPSED_MS))
    payload = json.loads(line)
    assert payload["request_id"] == "req-12345678"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "korfbal.test"
    assert payload["match"] == "m1"
    assert payload["ms"] == ELAPSED_MS
    assert "task_id" not in payload


def test_json_lines_include_exceptions() -> None:
    """Tracebacks stay inside the single JSON line."""
    error = RuntimeError("boom")
    record = logging.getLogger("korfbal.test").makeRecord(
        "korfbal.test",
        logging.ERROR,
        __file__,
        1,
        "failed",
        (),
        (RuntimeError, error, None),
    )
    payload = json.loads(observability.JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["exc"]


def test_celery_signals_attach_and_release_task_context() -> None:
    """Worker logs name the task that produced them, and only while it runs."""
    task = mock.Mock()
    task.name = "apps.competition.tasks.sync"
    celery_module._bind_task_log_context(task_id="t-1", task=task)
    record = _record()
    assert (record.task_id, record.task_name) == ("t-1", task.name)
    celery_module._unbind_task_log_context(task_id="t-1")
    assert _record().task_id is None


@pytest.mark.django_db
@override_settings(KORFBAL_ACCESS_LOG=True)
def test_access_log_records_route_pattern_not_raw_path(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    """Path values (IDs, activation tokens) never reach the access log."""
    club_id = str(uuid.uuid4())
    with caplog.at_level(logging.INFO, logger="korfbal.access"):
        response = client.get(f"/api/club/clubs/{club_id}/")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == GENERATED_REQUEST_ID_LENGTH
    [entry] = [r for r in caplog.records if r.name == "korfbal.access"]
    assert entry.request_id == request_id
    assert entry.status == response.status_code
    assert entry.route.startswith("api/club/")
    assert club_id not in entry.getMessage()
    assert club_id not in json.dumps(vars(entry), default=str)


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [
        ("edge-0123456789abcdef", True),
        ("short", False),
        ("bad id\nforged-line", False),
        ("x" * 65, False),
    ],
)
@pytest.mark.django_db
def test_incoming_request_ids_are_kept_only_when_safe(
    client: Client, incoming: str, kept: bool
) -> None:
    """An edge-supplied ID correlates; anything else cannot forge log content."""
    response = client.get("/api/unknown-route/", headers={"X-Request-ID": incoming})
    assert (response.headers["X-Request-ID"] == incoming) is kept


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"parent_sampled": True}, 1.0),
        ({"parent_sampled": False}, 0.0),
        ({"asgi_scope": {"path": "/api/live/events/"}}, 0.0),
        ({"wsgi_environ": {"PATH_INFO": "/metrics"}}, 0.0),
        ({"wsgi_environ": {"PATH_INFO": "/api/match/1/"}}, 0.25),
    ],
)
def test_traces_follow_client_decision_and_skip_streams(
    context: dict[str, Any], expected: float
) -> None:
    """Browser traces stay joined to the API; long-lived streams are not traced."""
    assert observability.traces_sampler(context, default_rate=0.25) == expected


def test_sentry_is_off_without_dsn() -> None:
    """Development, tests and self-hosters report nothing by default."""
    with mock.patch.object(observability.sentry_sdk, "init") as init:
        assert not observability.init_sentry(
            observability.SentryOptions(dsn="", environment="production")
        )
    init.assert_not_called()


def test_sentry_sends_no_personal_data_or_request_bodies() -> None:
    """Auth payloads (OTPs, passkey assertions, tokens) never leave the server."""
    with mock.patch.object(observability.sentry_sdk, "init") as init:
        assert observability.init_sentry(
            observability.SentryOptions(
                dsn="https://key@o1.ingest.de.sentry.io/1",
                environment="production",
                release="abcdef123456",
            )
        )
    options = init.call_args.kwargs
    assert options["send_default_pii"] is False
    assert options["max_request_body_size"] == "never"
    assert options["release"] == "abcdef123456"
    event = {"extra": {"payload": {"otp": "123456", "assertion": "x", "club": "Ons"}}}
    scrubbed = options["event_scrubber"]
    scrubbed.scrub_event(event)
    payload = event["extra"]["payload"]
    assert payload["club"] == "Ons"
    assert payload["otp"] != "123456"
    assert payload["assertion"] != "x"


def test_production_containers_rotate_their_logs() -> None:
    """Docker's default json-file driver never deletes logs and fills the disk."""
    project = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((project / "docker-compose.prod.yaml").read_text())
    for name, service in compose["services"].items():
        options = service.get("logging", {}).get("options", {})
        assert options.get("max-size"), name
        assert options.get("max-file"), name


@pytest.mark.django_db
def test_browser_trace_headers_pass_cors_preflight(client: Client) -> None:
    """Sentry's browser tracing adds headers that a strict preflight would reject."""
    response = client.options(
        "/api/matches/finished/",
        headers={
            "Origin": "https://korfbal.butrosgroot.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "sentry-trace,baggage",
        },
    )
    allowed = response.headers["Access-Control-Allow-Headers"].lower()
    assert "sentry-trace" in allowed
    assert "baggage" in allowed


@pytest.mark.django_db
def test_request_id_is_readable_cross_origin(client: Client) -> None:
    """The web app can quote the API request ID in its own error reports."""
    response = client.get(
        "/api/matches/finished/?limit=1",
        headers={"Origin": "https://korfbal.butrosgroot.com"},
    )
    assert "x-request-id" in response.headers["Access-Control-Expose-Headers"].lower()


# --- Captured Sentry envelopes -----------------------------------------------

PATH_VALUE = "c0ffee-activation-value-4f1e"
RECIPIENT = "speler@example.nl"


def _failing_view(request: HttpRequest, token: str) -> HttpResponse:
    body = json.dumps({"password": SUBMITTED_SECRET, "otp": SUBMITTED_CODE})
    raise RuntimeError(f"failed for {len(body)} bytes")


def _ok_view(request: HttpRequest, token: str) -> HttpResponse:
    return HttpResponse("ok")


def _form_view(request: HttpRequest, token: str) -> HttpResponse:
    return HttpResponse(str(len(request.POST)))


_upload_view = csrf_exempt(_form_view)


urlpatterns = [
    path("api/fail/<str:token>/", _failing_view),
    path("api/ok/<str:token>/", _ok_view),
    path("api/form/<str:token>/", _form_view),
    path("api/upload/<str:token>/", _upload_view),
]


class _CapturingTransport(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def dump(self) -> str:
        return "\n".join(
            json.dumps(item.payload.json, default=str)
            for envelope in self.envelopes
            for item in envelope.items
            if item.payload.json is not None
        )


@pytest.fixture
def sentry_transport() -> Iterator[_CapturingTransport]:
    """Run the real SDK with production options, capturing what it would send.

    Yields:
        The transport holding every envelope the SDK sent.

    """
    transport = _CapturingTransport()
    real_init = sentry_sdk.init

    def init(**options: object) -> None:
        real_init(**options, transport=transport)

    with mock.patch.object(observability.sentry_sdk, "init", side_effect=init):
        observability.init_sentry(
            observability.SentryOptions(
                dsn="https://key@o1.ingest.de.sentry.io/1",
                environment="test",
                traces_sample_rate=1.0,
            )
        )
    try:
        yield transport
    finally:
        sentry_sdk.flush()
        real_init()


def _wsgi_get(path_and_query: str, *, method: str = "GET", body: bytes = b"") -> int:
    """Request through the real WSGI handler, which Sentry instruments.

    The test client bypasses it, so its events have no request or transaction.

    Returns:
        The response status code.

    """
    route, _, query = path_and_query.partition("?")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": route,
        "QUERY_STRING": query,
        "HTTP_HOST": "testserver",
        "HTTP_REFERER": f"https://elders.example/{PATH_VALUE}",
        "HTTP_X_FORWARDED_FOR": "203.0.113.9",
        "REMOTE_ADDR": "203.0.113.9",
    }
    if body:
        environ.update({
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        })
    setup_testing_defaults(environ)
    statuses: list[str] = []

    def start_response(status: str, *_args: object) -> None:
        statuses.append(status)

    with override_settings(DEBUG_PROPAGATE_EXCEPTIONS=False):
        b"".join(get_wsgi_application()(environ, start_response))
    return int(statuses[0].split()[0])


def _assert_private(dump: str) -> None:
    for secret in (
        PATH_VALUE,
        RECIPIENT,
        SUBMITTED_SECRET,
        SUBMITTED_CODE,
        "geheim",
        "203.0.113.9",
    ):
        assert secret not in dump
    assert "https://elders.example/" not in dump


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_sentry_errors_omit_path_tokens_queries_headers_and_locals(
    sentry_transport: _CapturingTransport,
) -> None:
    """Activation tokens, query strings, Referers and parsed bodies stay private."""
    status = _wsgi_get(f"/api/fail/{PATH_VALUE}/?q=geheim")
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    sentry_sdk.flush()
    dump = sentry_transport.dump()
    assert "RuntimeError" in dump
    assert "http://testserver/api/fail/{token}/" in dump
    _assert_private(dump)


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_sentry_transactions_omit_path_tokens_and_queries(
    sentry_transport: _CapturingTransport,
) -> None:
    """Successful requests are traced by route pattern only."""
    assert _wsgi_get(f"/api/ok/{PATH_VALUE}/?q=geheim") == HTTPStatus.OK
    assert _wsgi_get(f"/api/unrouted/{PATH_VALUE}/?q=geheim") == HTTPStatus.NOT_FOUND
    sentry_sdk.flush()
    dump = sentry_transport.dump()
    assert '"transaction": "/api/ok/{token}/"' in dump
    assert '"transaction": "<unmatched>"' in dump
    _assert_private(dump)


def test_sentry_log_events_and_breadcrumbs_omit_email_addresses(
    sentry_transport: _CapturingTransport,
) -> None:
    """Mail tasks log recipients; neither the event nor its breadcrumbs keep them."""
    task_logger = logging.getLogger("bg_auth.tasks.task_send_email")
    task_logger.info("Sending 2FA email to %s.", RECIPIENT)
    error = RuntimeError(f"SMTP refused {RECIPIENT}")
    task_logger.error(
        "Error sending 2FA email to %s: %s",
        RECIPIENT,
        error,
        exc_info=(RuntimeError, error, None),
    )
    sentry_sdk.flush()
    dump = sentry_transport.dump()
    assert "Error sending 2FA email" in dump
    _assert_private(dump)


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_json_error_logs_omit_request_paths_and_queries() -> None:
    """Django's 500 log carries the request object and raw path; neither is kept."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability.JsonFormatter())
    django_logger = logging.getLogger("django.request")
    django_logger.addHandler(handler)
    try:
        Client(raise_request_exception=False).get(f"/api/fail/{PATH_VALUE}/?q=geheim")
    finally:
        django_logger.removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines
    assert lines[0]["message"] == "Internal Server Error: api/fail/<str:token>/"
    assert "request" not in lines[0]
    _assert_private(stream.getvalue())


def test_json_logs_redact_email_addresses() -> None:
    """Shipped logs never contain the recipients that mail tasks interpolate."""
    record = _record(f"Sending 2FA email to {RECIPIENT}.")
    assert RECIPIENT not in observability.JsonFormatter().format(record)


def _json_logs(logger_name: str = "") -> tuple[io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability.JsonFormatter())
    logging.getLogger(logger_name).addHandler(handler)
    return stream, handler


def test_failed_mail_tasks_ship_no_recipients_codes_or_links(
    sentry_transport: _CapturingTransport,
) -> None:
    """Celery logs a failed task with its arguments; neither sink keeps them."""
    stream, handler = _json_logs()
    activation = f"https://korfbal.example/activate/MTIz/{PATH_VALUE}/"
    try:
        with mock.patch.object(
            EmailMultiAlternatives, "send", side_effect=RuntimeError("SMTP down")
        ):
            send_2fa_email_task.apply(args=(RECIPIENT, SUBMITTED_CODE))
            send_confirmation_email_task.apply(args=(RECIPIENT, activation))
    finally:
        logging.getLogger().removeHandler(handler)
    sentry_sdk.flush()
    logs = stream.getvalue()
    assert "raised unexpected" in logs
    _assert_private(logs)
    _assert_private(sentry_transport.dump())


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__, DATA_UPLOAD_MAX_MEMORY_SIZE=10)
def test_security_errors_ship_no_request_objects(
    sentry_transport: _CapturingTransport,
) -> None:
    """`django.security.*` logs attach the request; its repr is the full URL."""
    stream, handler = _json_logs()
    try:
        status = _wsgi_get(
            f"/api/upload/{PATH_VALUE}/?q=geheim", method="POST", body=b"a=" + b"x" * 64
        )
    finally:
        logging.getLogger().removeHandler(handler)
    assert status == HTTPStatus.BAD_REQUEST
    sentry_sdk.flush()
    _assert_private(stream.getvalue())
    _assert_private(sentry_transport.dump())


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_csrf_failures_log_routes_not_paths_or_referers(
    sentry_transport: _CapturingTransport,
) -> None:
    """A rejected POST quotes its path and the full Referer; only routes remain."""
    stream, handler = _json_logs()
    try:
        response = Client(enforce_csrf_checks=True).post(
            f"/api/form/{PATH_VALUE}/?q=geheim",
            secure=True,
            headers={"Referer": f"https://elders.example/{PATH_VALUE}?q=geheim"},
        )
    finally:
        logging.getLogger().removeHandler(handler)
    assert response.status_code == HTTPStatus.FORBIDDEN
    sentry_sdk.flush()
    logs = stream.getvalue()
    assert "api/form/<str:token>/" in logs
    _assert_private(logs)
    _assert_private(sentry_transport.dump())


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_malformed_origins_keep_csrf_rejections_at_403() -> None:
    """Redacting a header URL that cannot be parsed must not break the response."""
    stream, handler = _json_logs()
    try:
        response = Client(enforce_csrf_checks=True).post(
            f"/api/form/{PATH_VALUE}/",
            secure=True,
            headers={"Origin": "https://[invalid"},
        )
    finally:
        logging.getLogger().removeHandler(handler)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "[url]" in stream.getvalue()


def test_sentry_events_with_malformed_urls_are_still_sent() -> None:
    """An unparsable request URL is dropped rather than failing the event."""
    event = observability.sanitize_event({"request": {"url": "https://[invalid/x"}})
    assert event["request"] == {}
