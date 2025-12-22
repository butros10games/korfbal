"""Generate VAPID keys for web push notifications.

This command generates an ES256 (P-256) keypair and prints it in a format that
can be pasted into a .env file.

References:
    - VAPID for Web Push (RFC 8292)

Output keys are base64url-encoded (no padding), as commonly used by web-push
clients.

Security:
    - Treat the PRIVATE key as a secret. Never commit it.
    - The PUBLIC key is safe to expose (frontend uses it for PushManager.subscribe).

"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.core.management.base import BaseCommand


DEFAULT_VAPID_SUBJECT = "mailto:admin@example.com"


def _b64url_no_padding(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> tuple[str, str]:
    """Generate a VAPID (ES256 / P-256) keypair.

    Returns:
        (public_key_b64url, private_key_b64url)

    Notes:
        - Public key is the uncompressed EC point (65 bytes, starts with 0x04).
        - Private key is the 32-byte private scalar.

    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.private_numbers()

    private_value_bytes = numbers.private_value.to_bytes(32, "big")

    x_bytes = numbers.public_numbers.x.to_bytes(32, "big")
    y_bytes = numbers.public_numbers.y.to_bytes(32, "big")
    public_value_bytes = b"\x04" + x_bytes + y_bytes

    return (
        _b64url_no_padding(public_value_bytes),
        _b64url_no_padding(private_value_bytes),
    )


class Command(BaseCommand):
    """Generate and print VAPID keys for the Korfbal web push configuration."""

    help = "Generate WEBPUSH_VAPID_PUBLIC_KEY / WEBPUSH_VAPID_PRIVATE_KEY"

    def add_arguments(self, parser: Any) -> None:  # noqa: ANN401
        """Add CLI arguments."""
        parser.add_argument(
            "--subject",
            default=str(
                getattr(settings, "WEBPUSH_VAPID_SUBJECT", DEFAULT_VAPID_SUBJECT)
                or DEFAULT_VAPID_SUBJECT
            ),
            help=(
                "Value for WEBPUSH_VAPID_SUBJECT (e.g. mailto:admin@example.com "
                "or https://example.com)"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Generate keys and print them to stdout."""
        subject = str(options.get("subject") or DEFAULT_VAPID_SUBJECT).strip()

        public_key, private_key = generate_vapid_keypair()

        self.stdout.write("Generated VAPID keys (base64url, no padding):")
        self.stdout.write("")
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={public_key}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={private_key}")
        self.stdout.write(f"WEBPUSH_VAPID_SUBJECT={subject}")
        self.stdout.write("WEBPUSH_TTL_SECONDS=3600")
        self.stdout.write("")
        self.stdout.write("Frontend (Vite) also needs the public key:")
        self.stdout.write("VITE_VAPID_PUBLIC_KEY=<same as WEBPUSH_VAPID_PUBLIC_KEY>")
