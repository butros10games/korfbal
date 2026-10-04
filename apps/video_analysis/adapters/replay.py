"""Publish a long recording as one replay, advancing one bounded CPU section.

Committed sections are immutable. A section that failed (an explicit retry) or
whose worker died (resumed automatically, a bounded number of times) runs again
as a new attempt of the same section: the unfinished attempt's private files
and gallery commit are discarded, its partial progress leaves the replay, and
chunks it already published keep their names and bytes while the new attempt
publishes under attempt-scoped names.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
import json
import subprocess

from apps.video_analysis.engine.clip_contract import (
    MAX_FRAMES,
    MIN_CLIP_SECONDS,
    REPLAY_PART_SECONDS,
    ClipOptions,
)
from apps.video_analysis.engine.clip_events import MAX_REPLAY_EVENTS
from apps.video_analysis.engine.clip_match_identity import scope_link
from apps.video_analysis.engine.clip_match_wide import RESULT, republish, wide_receipt
from apps.video_analysis.engine.clip_section_identity import GALLERY_FILE, withdraw
from apps.video_analysis.engine.clips import directory, receipt
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.engine.vision import artifact, digest

from .match_identity import MatchInputsError


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
RETRYABLE = {"failed", "interrupted"}
# Automatic restarts of a section whose worker died, before a person decides.
MAX_SECTION_ATTEMPTS = 3


def publish_match_wide(store: Store, record: dict, match_pass: Callable) -> dict:
    """Run a replay's match pass and rename its published links with the result.

    The links are rebuilt from every section's own receipt (``republish``), so
    a repeated pass after a reviewer's answer starts from the section names,
    never from an earlier pass's names. A failure keeps the published names
    and records why; it never fails the replay.

    Returns:
        The replay's updated receipt (not yet saved).

    """
    run_id = record["id"]
    try:
        match_pass(store, run_id)
        result = json.loads(
            (directory(store, run_id) / RESULT).read_text(encoding="utf-8")
        )
        children = [
            receipt(store, f"{run_id}-part-{part:04d}")
            for part in range(int(record.get("completed_parts", 0)))
        ]
    except MatchInputsError as error:
        # Never solve on part of the recording or without stored answers.
        wide = {
            "status": "failed",
            "code": "inputs_unavailable",
            "message": str(error)[:300],
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        wide = {
            "status": "failed",
            "code": "match_pass_failed",
            "message": "The match pass failed; the published names are kept",
        }
    else:
        record["identity_refinement"] = republish(record, children, result)
        wide = wide_receipt(result)
    return {
        **record,
        "match_identity_wide": {
            **wide,
            "finished_at": datetime.now(UTC).isoformat(),
        },
    }


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
        if 0 < remaining - duration < MIN_CLIP_SECONDS:
            duration = remaining
        self.child_payload: dict = {
            "match_id": payload["match_id"],
            "model": payload["model"],
            "options": {
                **asdict(options),
                "start": self.record["next_start"],
                "duration": duration,
            },
            # Server-owned: the child closes at its budget and shares this
            # replay's roster gallery (one match-wide identity).
            "section": {"part": self.part, "parent": run_id},
        }
        if options.team_colors is None and self.record.get("committed_team_colors"):
            self.child_payload["options"]["team_colors"] = self.record[
                "committed_team_colors"
            ]
        self.committed = self.record["committed_totals"].copy()
        self.match_pass: Callable | None = None
        self.chunks = {c["name"]: c for c in self.record["chunks"]}
        self.attempt = int(
            self.record.get("section_attempts", {}).get(str(self.part), 0)
        )

    def save(self) -> None:
        """Publish the parent manifest only after its immutable chunks are available."""
        atomic_json(self.marker, self.record)
        self.store.publish_artifact(self.marker.relative_to(self.store.root).as_posix())

    def copy_chunk(self, chunk: dict) -> None:
        """Copy a new verified child chunk without duplicating already published data.

        Raises:
            ValueError: A chunk is changed, corrupt or outside the child directory.

        """
        # A new attempt of a section never reuses a name an earlier one published.
        attempt = f"r{self.attempt}-" if self.attempt else ""
        name = f"part-{self.part:04d}-{attempt}{chunk['name']}"
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
            refinement: dict = {
                **progress["identity_refinement"],
                "section_boundaries": True,
            }
            for field in ("links", "frame_links"):
                previous = [
                    link
                    for link in self.record.get("identity_refinement", {}).get(
                        field, []
                    )
                    if link["processing_section"] < self.part
                ]
                current = [
                    scope_link(link, self.part, refinement.get("match_identity"))
                    for link in progress["identity_refinement"].get(field, [])
                ]
                if previous or current or field == "links":
                    refinement[field] = previous + current
            self.record["identity_refinement"] = refinement
        self.merge_team_resolution(progress.get("team_resolution"))
        self.record.update(
            status="running",
            message=(
                f"Analyzing section {self.part + 1}; "
                "completed frames are ready to watch"
            ),
            active_part=self.part,
        )
        self.save()

    def merge_team_resolution(self, progress: dict | None) -> None:
        """Prefix section team intervals; committed sections stay immutable."""
        if not progress:
            return
        resolution = self.record.get("team_resolution", {})
        previous = [
            span
            for span in resolution.get("spans", [])
            if span["processing_section"] < self.part
        ]
        current = [
            {
                **span,
                "track_id": f"p{self.part}-{span['track_id']}",
                "processing_section": self.part,
            }
            for span in progress.get("spans", [])
        ]
        self.record["team_resolution"] = {
            **progress,
            "spans": previous + current,
            "section_boundaries": True,
            "truncated": bool(resolution.get("truncated") or progress.get("truncated")),
        }

    def finish(self, result: dict) -> None:
        """Commit a completed boundary, retaining incomplete sections on failure."""
        self.publish(result)
        if (self.root / "cancel.json").exists():
            self.record.update(
                status="cancelled", message="Stopped; completed replay frames retained"
            )
        elif result["status"] == "completed":
            options = self.child_payload["options"]
            end = options["start"] + options["duration"]
            boundary = result.get("section_end_seconds")
            if isinstance(boundary, (int, float)) and options["start"] < boundary < end:
                if self.payload["recording_end"] - boundary < MIN_CLIP_SECONDS:
                    # Too short to analyse as a section of its own (the engine
                    # no longer stops there): finish, and say what was skipped.
                    self.record["unprocessed_tail_seconds"] = round(
                        self.payload["recording_end"] - boundary, 6
                    )
                else:
                    # The child used its runtime budget before the planned end:
                    # continue from its first unprocessed frame, not past it.
                    end = float(boundary)
                    self.record["budget_boundaries"] = (
                        self.record.get("budget_boundaries", 0) + 1
                    )
            self.record.update(
                completed_parts=self.part + 1,
                next_start=end,
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
            if done:
                self.match_wide()
            self.record.update(
                status="completed" if done else "queued",
                message="Match replay ready" if done else "Next replay section queued",
            )
        else:
            self.record.update(status=result["status"], message=result["message"])

    def match_wide(self) -> None:
        """Name players across all sections once the last section is done.

        Runs only for a replay with a match roster. The match pass reads every
        section's identity evidence (``clip_match_wide``); a failure keeps each
        section's own names and is recorded, it never fails the replay.
        """
        closed = (self.payload["options"].get("match_identity") or {}).get("closed_set")
        if not closed or self.match_pass is None:
            return
        # The match pass reads the committed section count from the manifest.
        self.record["message"] = "Naming players across the whole recording"
        self.save()
        self.record = publish_match_wide(self.store, self.record, self.match_pass)

    def restart(self, *, automatic: bool) -> None:
        """Make the unfinished section a new attempt, keeping committed sections.

        A child that completed (only its commit into this replay was lost) is
        committed as it is. Otherwise its private files are discarded, here and
        in object storage, so a stale review cache or evidence is never reused;
        its gallery commit is withdrawn; and its partial progress leaves the
        replay. Its published chunks stay as they are, unlisted.

        Raises:
            ValueError: An interrupted section used up its automatic restarts.

        """
        marker = self.child_root / "run.json"
        if marker.is_file() and (
            json.loads(marker.read_text(encoding="utf-8")).get("status") == "completed"
        ):
            return
        attempts = self.record.setdefault("section_attempts", {})
        if automatic and int(attempts.get(str(self.part), 0)) >= MAX_SECTION_ATTEMPTS:
            raise ValueError(
                f"Section {self.part + 1} was interrupted {MAX_SECTION_ATTEMPTS} "
                "times; retry it once the worker is healthy"
            )
        self.attempt = int(attempts.get(str(self.part), 0)) + 1
        attempts[str(self.part)] = self.attempt
        self.store.discard_section(self.record["id"], self.part)
        withdraw(self.root / GALLERY_FILE, f"part-{self.part:04d}")
        own = f"part-{self.part:04d}-"
        self.record["chunks"] = [
            c for c in self.record["chunks"] if not c["name"].startswith(own)
        ]
        self.chunks = {c["name"]: c for c in self.record["chunks"]}
        self.record.update({k: self.committed.get(k, 0) for k in TOTALS})
        self.record.pop("finished_at", None)
        if "events" in self.record:
            self.record["events"] = [
                e for e in self.record["events"] if e["processing_section"] < self.part
            ]
        for field in ("links", "frame_links"):
            links = self.record.get("identity_refinement", {}).get(field)
            if links is not None:
                self.record["identity_refinement"][field] = [
                    link for link in links if link["processing_section"] < self.part
                ]
        spans = self.record.get("team_resolution", {}).get("spans")
        if spans is not None:
            self.record["team_resolution"]["spans"] = [
                span for span in spans if span["processing_section"] < self.part
            ]

    def run(
        self, analyze: Callable, match: Callable | None = None, *, retry: bool = False
    ) -> None:
        """Run one section; every failure retains progress and a terminal receipt.

        ``match`` runs the recording's match pass after the last section.
        ``retry`` is the server-owned retry of a failed replay: it resumes at
        the unfinished section. A redelivery of a failed replay runs nothing.

        Raises:
            ValueError: The checkpoint changed between sections.

        """
        self.match_pass = match
        restart = self.resuming
        if self.record["status"] in TERMINAL:
            if not (retry and self.record["status"] in RETRYABLE):
                return
            restart = True
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
            if restart:
                # A worker died mid-section (restarted automatically, bounded)
                # or a person asked to retry the failed section.
                self.restart(automatic=not retry)
            self.record.update(
                status="running",
                message=f"Analyzing section {self.part + 1}"
                + (f" (attempt {self.attempt + 1})" if self.attempt else ""),
            )
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
