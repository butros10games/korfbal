"""Isolated native review workspace fixtures."""

from pathlib import Path

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
