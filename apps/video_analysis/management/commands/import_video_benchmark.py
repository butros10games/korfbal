"""Import a frozen paired evaluation for manual audit without changing labels."""

import json
from pathlib import Path
from typing import cast

from django.core.management.base import BaseCommand, CommandParser

from apps.video_analysis.composition import worker_store
from apps.video_analysis.engine import temporal_fusion, vision
from apps.video_analysis.engine.curation import fingerprint
from apps.video_analysis.engine.store import atomic_json, validate_annotation
from apps.video_analysis.models import Workspace


class Command(BaseCommand):
    """Register baseline or v2 temporal boxes from a captured evaluation report."""

    help = "Import captured benchmark predictions into Audit & training examples"

    def add_arguments(self, parser: CommandParser) -> None:
        """Require explicit report, immutable ID and prediction variant."""
        parser.add_argument("report", type=Path)
        parser.add_argument("--name", required=True)
        parser.add_argument("--workspace", default="main")
        parser.add_argument(
            "--variant", choices=["single", "temporal"], default="single"
        )

    def handle(self, *args: object, **options: object) -> None:
        """Validate all inputs before publishing an immutable audit artifact.

        Raises:
            ValueError: A frame is invalid or the benchmark ID already exists.

        """
        workspace = Workspace.objects.get(slug=options["workspace"])
        store = worker_store(workspace, None)
        name = vision.identifier(cast(str, options["name"]))
        report = json.loads(cast(Path, options["report"]).read_text())
        current = {
            (m["id"], f["id"]) for m in store.read()["matches"] for f in m["frames"]
        }
        records = []
        seen = set()
        for item in report["frames"]:
            identity = (item["match_id"], item["frame_id"])
            if identity not in current or identity in seen:
                raise ValueError(
                    "Benchmark contains missing or duplicate workspace frames"
                )
            seen.add(identity)
            prediction = validate_annotation(item["baseline"])
            if options["variant"] == "temporal" and item.get("mode") == "temporal":
                tracks = [
                    {int(k): v for k, v in step.items()} for step in item["tracks"]
                ]
                prediction, _ = temporal_fusion.fuse(
                    prediction, tracks, report["confidence"], len(tracks) // 2
                )
            records.append({
                "match_id": identity[0],
                "frame_id": identity[1],
                "prediction": prediction,
                "reference": validate_annotation(item["reference"]),
                "group": item["group"],
            })
        with store.transaction():
            target = store.root / "vision/audits" / f"{name}.json"
            if target.exists():
                raise ValueError("Benchmark ID already exists")
            atomic_json(
                target,
                {
                    "schema_version": 1,
                    "variant": options["variant"],
                    "report_sha256": fingerprint(report),
                    "weights_sha256": report["weights_sha256"],
                    "frames": records,
                },
            )
            store.sync_artifacts()
        self.stdout.write(
            f"Imported {len(records)} audit frames as {name}; no reviews changed"
        )
