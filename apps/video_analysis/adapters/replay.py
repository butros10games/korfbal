"""Publish a long recording as one replay, advancing one bounded CPU section."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
import json

from apps.video_analysis.engine.clip_contract import (
    MAX_FRAMES,
    REPLAY_PART_SECONDS,
    ClipOptions,
)
from apps.video_analysis.engine.clip_events import MAX_REPLAY_EVENTS
from apps.video_analysis.engine.clips import directory, receipt
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import artifact, digest


TOTALS = (
    "frames",
    "runtime_seconds",
    "camera_cuts",
    "active_ball_frames",
    "court_frames",
    "team_assigned",
    "player_observations",
)
METADATA = (
    "video",
    "video_sha256",
    "source_offset_seconds",
    "recording_title",
    "weights_sha256",
    "inference",
    "environment",
    "cpu_threads",
    "team_colors",
)
TERMINAL = {"completed", "cancelled", "failed", "interrupted"}


def prefix_tracks(frame: dict, part: int) -> dict:
    """Prevent independent section trackers from reusing the same visible identity."""

    def visit(value: object) -> object:
        if isinstance(value, list):
            return [visit(v) for v in value]
        if isinstance(value, dict):
            return {
                k: f"p{part}-{v}"
                if k in {"track_id", "near_player", "shot_event_id"}
                and isinstance(v, str)
                else visit(v)
                for k, v in value.items()
            }
        return value

    result = visit(frame)
    assert isinstance(result, dict)
    result["segment"] = part * MAX_FRAMES + frame["segment"]
    result["processing_section"] = part
    return result


class ReplaySection:
    """Resume a bounded section and publish progress into its single parent replay."""

    def __init__(self, store: Store, run_id: str, payload: dict) -> None:
        """Bind immutable inputs and load the last committed section boundary.

        Raises:
            ValueError: The replay ID belongs to a different recipe.

        """
        self.store, self.payload = store, payload
        self.root = directory(store, run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker = self.root / "run.json"
        options = ClipOptions.parse(payload["options"])
        recipe = {
            **payload,
            "options": {
                **asdict(options),
                "duration": payload["recording_end"] - options.start,
            },
        }
        self.record: dict = (
            json.loads(self.marker.read_text(encoding="utf-8"))
            if self.marker.exists()
            else {
                "id": run_id,
                "kind": "clip",
                "schema_version": 1,
                "recipe": recipe,
                "status": "queued",
                "review_only": True,
                "chunks": [],
                "frames": 0,
                "created_at": datetime.now(UTC).isoformat(),
                "completed_parts": 0,
                "next_start": options.start,
                "committed_totals": {},
                "message": "Preparing the first replay section",
            }
        )
        if self.record["recipe"] != recipe:
            raise ValueError("Replay ID belongs to a different request")
        self.resuming = self.record["status"] == "running"
        self.part = self.record["completed_parts"]
        self.child_id = f"{run_id}-part-{self.part:04d}"
        self.child_root = directory(store, self.child_id)
        remaining = payload["recording_end"] - self.record["next_start"]
        duration = min(REPLAY_PART_SECONDS, remaining)
        if 0 < remaining - duration < 1:
            duration = remaining
        self.child_payload: dict = {
            "match_id": payload["match_id"],
            "model": payload["model"],
            "options": {
                **asdict(options),
                "start": self.record["next_start"],
                "duration": duration,
            },
        }
        if options.team_colors is None and self.record.get("committed_team_colors"):
            self.child_payload["options"]["team_colors"] = self.record[
                "committed_team_colors"
            ]
        self.committed = self.record["committed_totals"].copy()
        self.chunks = {c["name"]: c for c in self.record["chunks"]}

    def save(self) -> None:
        """Publish the parent manifest only after its immutable chunks are available."""
        atomic_json(self.marker, self.record)
        self.store.publish_artifact(self.marker.relative_to(self.store.root).as_posix())

    def copy_chunk(self, chunk: dict) -> None:
        """Copy a new verified child chunk without duplicating already published data.

        Raises:
            ValueError: A chunk is changed, corrupt or outside the child directory.

        """
        name = f"part-{self.part:04d}-{chunk['name']}"
        known = self.chunks.get(name)
        if known:
            if known["source_sha256"] != chunk["sha256"]:
                raise ValueError("A published replay chunk changed")
            return
        source = self.child_root / chunk["name"]
        if source.parent != self.child_root or digest(source) != chunk["sha256"]:
            raise ValueError("Invalid replay chunk")
        content = json.loads(source.read_text(encoding="utf-8"))
        target = self.root / name
        atomic_json(
            target, {"frames": [prefix_tracks(f, self.part) for f in content["frames"]]}
        )
        self.store.publish_artifact(target.relative_to(self.store.root).as_posix())
        published = {
            **chunk,
            "name": name,
            "sha256": digest(target),
            "source_sha256": chunk["sha256"],
        }
        self.record["chunks"].append(published)
        self.chunks[name] = published

    def publish(self, progress: dict) -> None:
        """Aggregate current progress with committed sections, never with old progress.

        Raises:
            ValueError: Immutable source or checkpoint hashes changed between sections.

        """
        if (self.root / "cancel.json").exists():
            atomic_json(self.child_root / "cancel.json", {"requested": True})
        for key in ("weights_sha256", "video_sha256"):
            if (
                self.record.get(key)
                and progress.get(key)
                and progress[key] != self.record[key]
            ):
                raise ValueError("Replay inputs changed between sections")
        for chunk in progress.get("chunks", []):
            self.copy_chunk(chunk)
        self.record.update({k: progress[k] for k in METADATA if k in progress})
        self.record.update({
            k: self.committed.get(k, 0) + progress.get(k, 0) for k in TOTALS
        })
        if progress.get("event_detection"):
            # Replace this part's mutable candidates on every progress receipt.
            # Previously committed parts and their identities remain immutable.
            previous = [
                e
                for e in self.record.get("events", [])
                if e["processing_section"] < self.part
            ]
            current = [
                {**prefix_tracks(event, self.part), "id": f"p{self.part}-{event['id']}"}
                for event in progress.get("events", [])
            ]
            self.record["events"] = (previous + current)[:MAX_REPLAY_EVENTS]
            self.record["event_detection"] = {
                **progress["event_detection"],
                "processed_frames": self.record.get("committed_event_frames", 0)
                + progress["event_detection"].get("processed_frames", 0),
                "section_boundaries": True,
                "truncated": bool(
                    self.record.get("event_detection", {}).get("truncated")
                    or progress["event_detection"].get("truncated")
                    or len(previous) + len(current) > MAX_REPLAY_EVENTS
                ),
            }
        if progress.get("possession_detection"):
            self.record["possession_detection"] = {
                **progress["possession_detection"],
                "processed_frames": self.record.get("committed_possession_frames", 0)
                + progress["possession_detection"].get("processed_frames", 0),
                "section_boundaries": True,
                "truncated": bool(
                    self.record.get("possession_detection", {}).get("truncated")
                    or progress["possession_detection"].get("truncated")
                    or self.record.get("event_detection", {}).get("truncated")
                ),
            }
        if progress.get("identity_refinement"):
            refinement = {**progress["identity_refinement"], "section_boundaries": True}
            for field in ("links", "frame_links"):
                previous = [
                    link
                    for link in self.record.get("identity_refinement", {}).get(
                        field, []
                    )
                    if link["processing_section"] < self.part
                ]
                current = [
                    {
                        **link,
                        "from_track_id": f"p{self.part}-{link['from_track_id']}",
                        "to_track_id": f"p{self.part}-{link['to_track_id']}",
                        "processing_section": self.part,
                        **(
                            {
                                "superseded_track_id": (
                                    f"p{self.part}-" + link["superseded_track_id"]
                                )
                            }
                            if link.get("superseded_track_id")
                            else {}
                        ),
                    }
                    for link in progress["identity_refinement"].get(field, [])
                ]
                if previous or current or field == "links":
                    refinement[field] = previous + current
            self.record["identity_refinement"] = refinement
        self.record.update(
            status="running",
            message=(
                f"Analyzing section {self.part + 1}; "
                "completed frames are ready to watch"
            ),
            active_part=self.part,
        )
        self.save()

    def finish(self, result: dict) -> None:
        """Commit a completed boundary, retaining incomplete sections on failure."""
        self.publish(result)
        if (self.root / "cancel.json").exists():
            self.record.update(
                status="cancelled", message="Stopped; completed replay frames retained"
            )
        elif result["status"] == "completed":
            options = self.child_payload["options"]
            self.record.update(
                completed_parts=self.part + 1,
                next_start=options["start"] + options["duration"],
                committed_totals={k: self.record[k] for k in TOTALS},
                committed_team_colors=result.get("team_colors"),
                committed_event_frames=self.record.get("event_detection", {}).get(
                    "processed_frames", 0
                ),
                committed_possession_frames=self.record.get(
                    "possession_detection", {}
                ).get("processed_frames", 0),
            )
            done = self.record["next_start"] >= self.payload["recording_end"] - 1e-6
            self.record.update(
                status="completed" if done else "queued",
                message="Match replay ready" if done else "Next replay section queued",
            )
        else:
            self.record.update(status=result["status"], message=result["message"])

    def run(self, analyze: Callable) -> None:
        """Run one section; every failure retains progress and a terminal receipt.

        Raises:
            ValueError: The checkpoint changed between sections.

        """
        if self.record["status"] in TERMINAL:
            return
        try:
            if (self.root / "cancel.json").exists():
                self.record.update(
                    status="cancelled",
                    message="Stopped; completed replay frames retained",
                )
                return
            weights_path = (
                artifact(self.store, "runs", self.payload["model"])
                / "fit/weights/best.pt"
            )
            weights = self.store.media(
                weights_path.relative_to(self.store.root).as_posix()
            )
            if (
                self.record.get("weights_sha256")
                and digest(weights) != self.record["weights_sha256"]
            ):
                raise ValueError("The replay checkpoint changed between sections")
            if self.resuming and not (self.child_root / "run.json").is_file():
                raise ValueError(
                    "The interrupted section is unavailable; start a new replay"
                )
            self.record["status"] = "running"
            self.save()
            analyze(
                self.store, self.child_id, self.child_payload, progress=self.publish
            )
            self.finish(receipt(self.store, self.child_id))
        except Exception:
            self.record.update(
                status="failed",
                message="Replay stopped; completed sections and frames are retained",
            )
            raise
        finally:
            if self.record["status"] in TERMINAL:
                self.record["finished_at"] = datetime.now(UTC).isoformat()
            self.save()
