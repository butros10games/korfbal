"""Edge-case tests for slow-request helper behavior."""

from __future__ import annotations

from typing import cast

from django.http import HttpResponse, StreamingHttpResponse
from pytest_django.fixtures import SettingsWrapper

from apps.kwt_common.middleware import request_timing
from apps.kwt_common.utils.slow_requests import slow_request_buffer_ttl_s


def test_response_size_prefers_content_length_and_rejects_negative_values() -> None:
    """The explicit header is authoritative but cannot produce negative metrics."""
    expected_size = 4
    response = HttpResponse("larger body")
    response["Content-Length"] = "4"
    assert request_timing._response_size_bytes(response) == expected_size

    response["Content-Length"] = "-9"
    assert request_timing._response_size_bytes(response) == 0


def test_response_size_falls_back_for_malformed_header_without_stream_consumption() -> (
    None
):
    """Malformed lengths fall back to bytes without consuming stream content."""
    expected_size = 4
    response = HttpResponse("body")
    response["Content-Length"] = "unknown"
    assert request_timing._response_size_bytes(response) == expected_size

    streaming = StreamingHttpResponse(iter([b"private", b"content"]))
    assert request_timing._response_size_bytes(cast(HttpResponse, streaming)) is None


def test_slow_request_ttl_has_a_safe_minimum(settings: SettingsWrapper) -> None:
    """Invalidly small configuration cannot request backend-specific zero TTL."""
    safe_minimum_seconds = 60
    settings.KORFBAL_SLOW_REQUEST_BUFFER_TTL_S = -1

    assert slow_request_buffer_ttl_s() == safe_minimum_seconds
