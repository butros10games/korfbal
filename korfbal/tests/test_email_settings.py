"""Checks for the production email sender configuration."""

from importlib import import_module, reload

import pytest


def test_sender_defaults_to_smtp_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the SMTP account as sender unless an explicit sender is configured."""
    email_settings = import_module("korfbal.settings.email")
    try:
        with monkeypatch.context() as env:
            env.setenv("EMAIL_USER", "sender@example.com")
            env.delenv("DEFAULT_FROM_EMAIL", raising=False)
            reload(email_settings)
            assert email_settings.DEFAULT_FROM_EMAIL == "sender@example.com"

            env.setenv("DEFAULT_FROM_EMAIL", "no-reply@example.com")
            reload(email_settings)
            assert email_settings.DEFAULT_FROM_EMAIL == "no-reply@example.com"
    finally:
        reload(email_settings)
