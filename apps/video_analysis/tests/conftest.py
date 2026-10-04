"""Isolated native review workspace fixtures."""

import io
from pathlib import Path
from typing import BinaryIO
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.core.management import call_command
import pytest
from pytest_django.fixtures import Settings

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.media import create_demo
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Workspace


@pytest.fixture
def imported(tmp_path: Path, settings: Settings) -> tuple[User, DatabaseStore, Store]:
    """Import a disposable synthetic recording through the real migration command."""
    settings.VIDEO_ANALYSIS_ROOT = tmp_path / "native"
    owner = User.objects.create_user(username="reviewer", is_staff=True)
    source = tmp_path / "legacy"
    legacy = Store(source)
    create_demo(legacy)
    call_command("import_video_reviews", str(source), owner=owner.username)
    workspace = Workspace.objects.get(slug="main")
    return owner, DatabaseStore(workspace, owner), legacy


@pytest.fixture
def s3() -> MagicMock:
    """Model immutable S3 bytes without hiding corruption behind local cache reads."""
    client = MagicMock()
    objects: dict[tuple[str, str], bytes] = {}

    def upload(handle: BinaryIO, bucket: str, key: str, **kwargs: object) -> None:
        objects[bucket, key] = handle.read()

    def get(**kwargs: str) -> dict[str, object]:
        content = objects[kwargs["Bucket"], kwargs["Key"]]
        if kwargs.get("Range"):
            start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
            content = content[start : end + 1]
        return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    def download(bucket: str, key: str, handle: BinaryIO) -> None:
        handle.write(objects[bucket, key])

    client.upload_fileobj.side_effect = upload
    client.get_object.side_effect = get
    client.download_fileobj.side_effect = download
    return client
