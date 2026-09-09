"""Passkey settings must identify the frontend's origin, including hosted previews."""

from __future__ import annotations

import runpy

import pytest

from korfbal.settings import bg_auth, runtime, security


@pytest.mark.parametrize(
    ("debug", "web_origin", "expected_origin", "expected_rp_id"),
    [
        (True, None, "https://korfbal.localhost", "korfbal.localhost"),
        (
            False,
            None,
            "https://korfbal.butrosgroot.com",
            "korfbal.butrosgroot.com",
        ),
        (
            True,
            "https://korfbal.butrosgroot.com",
            "https://korfbal.butrosgroot.com",
            "korfbal.butrosgroot.com",
        ),
        (
            True,
            "https://preview.example:8443/",
            "https://preview.example:8443",
            "preview.example",
        ),
        (True, "http://localhost:5173", "http://localhost:5173", "localhost"),
    ],
)
def test_passkey_defaults_follow_frontend_origin(
    monkeypatch: pytest.MonkeyPatch,
    debug: bool,
    web_origin: str | None,
    expected_origin: str,
    expected_rp_id: str,
) -> None:
    """Debug mode must not force a hosted frontend to register localhost passkeys."""
    monkeypatch.setattr(runtime, "DEBUG", debug)
    monkeypatch.setenv("BG_AUTH_JWT_SIGNING_KEY", "synthetic-test-key")
    monkeypatch.delenv("BG_AUTH_PASSKEY_RP_ID", raising=False)
    monkeypatch.delenv("BG_AUTH_PASSKEY_ORIGINS", raising=False)
    monkeypatch.delenv("WEB_APP_ORIGIN", raising=False)
    if web_origin is not None:
        monkeypatch.setenv("WEB_APP_ORIGIN", web_origin)
    configured_security = runpy.run_path(
        security.__file__,
        run_name="korfbal.settings._passkey_test",
    )
    monkeypatch.setattr(
        security, "WEB_APP_ORIGIN", configured_security["WEB_APP_ORIGIN"]
    )

    configured = runpy.run_path(
        bg_auth.__file__,
        run_name="korfbal.settings._passkey_test",
    )

    assert configured["BG_AUTH_PASSKEY_RP_ID"] == expected_rp_id
    assert configured["BG_AUTH_PASSKEY_ORIGINS"] == expected_origin


def test_explicit_passkey_configuration_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit settings can share a parent RP across frontend origins."""
    monkeypatch.setenv("BG_AUTH_PASSKEY_RP_ID", "example.com")
    origins = "https://app.example.com,https://other.example.com"
    monkeypatch.setenv("BG_AUTH_PASSKEY_ORIGINS", origins)
    monkeypatch.setenv("BG_AUTH_PASSKEY_RP_NAME", "Example")

    configured = runpy.run_path(
        bg_auth.__file__,
        run_name="korfbal.settings._passkey_test",
    )

    assert configured["BG_AUTH_PASSKEY_RP_ID"] == "example.com"
    assert configured["BG_AUTH_PASSKEY_ORIGINS"] == origins
    assert configured["BG_AUTH_PASSKEY_RP_NAME"] == "Example"
