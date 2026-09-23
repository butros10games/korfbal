"""Private media migration must be repeatable and never replace different bytes."""

import io
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from django.core.management.base import CommandError
import pytest

from apps.player.management.commands.migrate_media_to_s3 import copy_media


def _clients(existing: bytes | None = None) -> tuple[MagicMock, MagicMock]:
    source = MagicMock()
    target = MagicMock()
    source.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "profile_pictures/example.png", "Size": 3}]}
    ]
    source.get_object.side_effect = lambda **_: {"Body": io.BytesIO(b"new")}
    stored = {"value": existing}

    def read(**_: str) -> dict[str, io.BytesIO]:
        if stored["value"] is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(stored["value"])}

    def upload(handle: io.BytesIO, bucket: str, key: str) -> None:
        stored["value"] = handle.read()

    target.get_object.side_effect = read
    target.upload_fileobj.side_effect = upload
    return source, target


def test_copy_media_verifies_and_reuses_existing_bytes() -> None:
    """Rerunning a completed transfer reads and checks without another upload."""
    source, target = _clients()
    assert copy_media(source, target, "old", "new") == (1, 3)
    assert copy_media(source, target, "old", "new") == (1, 3)
    target.upload_fileobj.assert_called_once()


def test_copy_media_refuses_to_replace_different_target() -> None:
    """A preexisting key with different bytes requires manual investigation."""
    source, target = _clients(b"old")
    with pytest.raises(CommandError, match="Target differs"):
        copy_media(source, target, "old", "new")
    target.upload_fileobj.assert_not_called()
