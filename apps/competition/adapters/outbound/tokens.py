"""Private OAuth session files with atomic refresh-token rotation."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from typing import Any


class TokenStore:
    """Keep private credentials outside Django; rotate sessions atomically."""

    def __init__(self, path: Path) -> None:
        """Read a user-owned session file, rejecting publicly readable credentials.

        Raises:
            ValueError: The session file is insecure or incomplete.
            TypeError: The session data is not an object.

        """
        if path.stat().st_mode & 0o077:
            raise ValueError("Session file permissions must be 600")
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("Session file must contain a JSON object")
        for key in ("client_id", "refresh_token", "user_agent"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Session file requires {key}")
        self.data = {
            key: data[key]
            for key in (
                "access_token",
                "refresh_token",
                "client_id",
                "user_agent",
                "secret",
                "expires_at",
            )
            if key in data
        }

    @property
    def access_token(self) -> str:
        """Return the current token without exposing the session in logs."""
        return str(self.data.get("access_token", ""))

    def needs_refresh(self) -> bool:
        """Renew shortly before known expiry; otherwise let a 401 trigger renewal."""
        expires_at = self.data.get("expires_at")
        return not self.access_token or (
            expires_at is not None and float(expires_at) <= time.time() + 60
        )

    def refresh_form(self) -> dict[str, str]:
        """Build the refresh grant verified with the KNKV app's provider."""
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.data["refresh_token"],
            "client_id": self.data["client_id"],
        }
        if self.data.get("secret"):
            form["secret"] = self.data["secret"]
        return form

    def rotate(self, response: dict[str, Any]) -> None:
        """Persist a valid renewal before using it for new API requests.

        Raises:
            ValueError: The token endpoint returned malformed credentials.

        """
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("Missing renewed access token")
        refresh = response.get("refresh_token", self.data["refresh_token"])
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("Invalid renewed refresh token")
        updated = {**self.data, "access_token": token, "refresh_token": refresh}
        updated["expires_at"] = time.time() + float(response["expires_in"])
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as f:
            temporary = Path(f.name)
            try:
                json.dump(updated, f)
                f.flush()
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        self.data = updated
