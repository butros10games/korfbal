"""Copy and verify a legacy workspace before its one-way writer cutover."""

from argparse import ArgumentParser
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import Store, validate_annotation
from apps.video_analysis.models import Workspace


def copy_media(legacy: Store, store: DatabaseStore, data: dict[str, Any]) -> None:
    """Copy registered media only and verify each payload.

    Raises:
        CommandError: A path escapes storage or its copied bytes differ.

    """
    for match in data["matches"]:
        for frame in match["frames"]:
            for key in ("proposal", "correction"):
                if frame.get(key) is not None:
                    validate_annotation(frame[key])
        paths = [f["image"] for f in match["frames"]]
        if match.get("video"):
            paths.append(match["video"])
        for relative in paths:
            original = legacy.media(relative)
            target = (store.root / relative).resolve()
            if not target.is_relative_to(store.root):
                raise CommandError("Invalid source media path")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            if digest(original) != digest(target):
                raise CommandError("Media verification failed")


def digest(path: Path) -> str:
    """Hash a file without loading full match footage into memory."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify(source: dict[str, Any], imported: dict[str, Any]) -> None:
    """Compare every annotation and history entry after normalization.

    Raises:
        CommandError: Import altered review data.

    """
    for expected, actual in zip(source["matches"], imported["matches"], strict=True):
        if {k: v for k, v in expected.items() if k != "frames"} != {
            k: v for k, v in actual.items() if k != "frames"
        }:
            raise CommandError("Recording metadata verification failed")
        for before, after in zip(expected["frames"], actual["frames"], strict=True):
            normalized = dict(
                before,
                proposal=before.get("proposal"),
                correction=before.get("correction"),
                history=before.get("history", []),
                complete=before.get("complete", False),
            )
            if normalized != after:
                raise CommandError("Annotation verification failed")


def import_data(
    legacy: Store, workspace: Workspace, data: dict[str, Any]
) -> dict[str, Any]:
    """Stage private artifacts and publish verified database records.

    Raises:
        CommandError: Artifact storage contains symlinks.

    """
    store = DatabaseStore(workspace)
    copy_media(legacy, store, data)
    artifacts = legacy.root / "vision"
    if artifacts.exists():
        if any(p.is_symlink() for p in artifacts.rglob("*")):
            raise CommandError("Artifact symlinks are not supported")
        shutil.copytree(artifacts, store.root / "vision", dirs_exist_ok=True)
        for path in artifacts.rglob("*"):
            if path.is_file() and digest(path) != digest(
                store.root / "vision" / path.relative_to(artifacts)
            ):
                raise CommandError("Artifact verification failed")
    source_revision = data["revision"]
    store._persist(data)
    Workspace.objects.filter(pk=workspace.pk).update(
        source_digest=digest(legacy.path), revision=source_revision
    )
    imported = store.read()
    verify(data, imported)
    return imported


class Command(BaseCommand):
    """Import annotations and artifacts without overwriting native work."""

    help = "Import a legacy directory; --cutover disables its old writer."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit source and existing owner."""
        parser.add_argument("source", type=Path)
        parser.add_argument("--owner", required=True)
        parser.add_argument("--slug", default="main")
        parser.add_argument("--cutover", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Hold the legacy writer lock throughout verification and commit.

        Raises:
            CommandError: The owner or existing import does not match.

        """
        source = Path(str(options["source"])).resolve()
        owner = User.objects.get(username=options["owner"])
        if not owner.is_staff or not owner.is_active:
            raise CommandError("Workspace owner must be active staff")
        legacy = Store(source)
        with legacy.transaction(), transaction.atomic():
            data = legacy.read()
            existing = Workspace.objects.filter(slug=options["slug"]).first()
            if existing:
                if existing.owner_id != owner.pk or existing.source_digest != digest(
                    legacy.path
                ):
                    raise CommandError("Different source revision; refusing overwrite")
                workspace = existing
                imported = DatabaseStore(existing).read()
            else:
                workspace = Workspace.objects.create(slug=options["slug"], owner=owner)
                imported = import_data(legacy, workspace, data)
            if options["cutover"]:
                transaction.on_commit(
                    lambda: (source / ".migrated-to-django").write_text(
                        str(workspace.pk)
                    )
                )
            counts = Counter(
                f["status"] for m in imported["matches"] for f in m["frames"]
            )
            self.stdout.write(
                json.dumps({
                    "workspace": str(workspace.pk),
                    "recordings": len(imported["matches"]),
                    "frames": sum(counts.values()),
                    "statuses": counts,
                    "cutover": options["cutover"],
                })
            )
