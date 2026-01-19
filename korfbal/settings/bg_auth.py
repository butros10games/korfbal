"""bg_auth app configuration exposed via settings."""

from __future__ import annotations

from .env import env


SITE = "Korfbal Web Tool"
LOGIN_FOTO: str = "images/logo/KWT_logo.png"
LOGIN_TITLE: str = "Welkom terug!"
LOGIN_DESCRIPTION: str = "login voor KWT"
REGISTER_TITLE: str = "Registratie"
REGISTER_HEADING_MOBILE: str = "Welkom!"
REGISTER_HEADING_DESKTOP: str = "Welkom!"
REGISTER_DESCRIPTION: str = "Maak je account aan"
BG_AUTH_SUPPORT_EMAIL: str = "butrosgroot@gmail.com"
BG_AUTH_EMAIL_CODE_VALIDITY_SECONDS: int = 15 * 60
BG_AUTH_RESEND_CONFIRMATION_MAX_AGE: int = 24 * 60 * 60

BG_AUTH_JWT_SIGNING_KEY = env("BG_AUTH_JWT_SIGNING_KEY", "")

LOGIN_REDIRECT_URL = "index"
