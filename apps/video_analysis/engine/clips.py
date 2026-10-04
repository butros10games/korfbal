"""Stream a bounded clip into private, inspectable chunks and a terminal receipt."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from copy import deepcopy
import ctypes
from dataclasses import asdict
from datetime import UTC, datetime
import importlib
from itertools import starmap
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from .clip_appearance import (
    Appearance,
    beside_broadcast,
    beside_cache,
    describe_players,
)
from .clip_closed_set import (
    RECIPE as CLOSED_SET_RECIPE,
    Confidence,
    Evidence,
    Player,
    ReviewSession,
    Scope,
    Settings as ClosedSetSettings,
    merge_additions,
)
from .clip_closed_set_calibration import calibrated
from .clip_contract import (
    CHUNK_FRAMES,
    MAX_RUNTIME_SECONDS,
    MIN_CLIP_SECONDS,
    SECTION_BUDGET_FRACTION,
    ClipOptions,
)
from .clip_identity import IdentityMemory, court_reference
from .clip_identity_review import ReviewCache
from .clip_inference import CPU_THREADS, clip_detector
from .clip_linking import Background, Linker, torso_colour
from .clip_live_play import Placements
from .clip_match_events import MatchEvents
from .clip_match_evidence import (
    compose_tracklets,
    fit_fragments,
    fragments,
    pure_fragments,
    resolved,
)
from .clip_match_gallery import RecordingGallery
from .clip_match_identity import Settings, associate, compose
from .clip_match_input import anchors, calibration, readings, validate
from .clip_match_wide import save as save_match_evidence
from .clip_models import MODEL_ERROR, failure_message, supports_clips
from .clip_number_anchors import (
    KitRead,
    NumberPolicy,
    NumberReads,
    confident as confident_numbers,
    kit_reads,
    named as named_numbers,
    oriented,
    tracklet_reads,
)
from .clip_overlap import OverlapFrame, OverlapRecovery
from .clip_positions import attach_post_distances
from .clip_recovery import PlayerRecovery, RecoveryFrame
from .clip_references import suggestion
from .clip_refinement import IdentityRefiner
from .clip_replay import top_down
from .clip_section_identity import (
    GALLERY_FILE,
    GalleryLink,
    added_players,
    carried,
    fingerprint as section_fingerprint,
    open_gallery,
    recorded,
    remember,
)
from .clip_signals import Camera, Teams, modules
from .clip_team_opening import OpeningTeams
from .clip_tracking import Balls, People
from .detect import ProjectionStore
from .keypoints import post_feet
from .number_artifact import load as load_numbers
from .numbers import ShirtNumbers
from .store import Store, atomic_json
from .training import environment, weights_record
from .vision import digest, identifier


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_appearance_giant import GiantAppearance


PROGRESS_SECONDS = 5
REFERENCE_TIME_TOLERANCE = 0.1
# Appearance changes slowly; a sparser sample keeps its cost bounded at any rate.
APPEARANCE_INTERVAL = 0.15


def directory(store: Store, run_id: str) -> Path:
    """Resolve a clip artifact inside its owner's workspace."""
    return store.root / "vision" / "clips" / identifier(run_id)


def receipt(store: Store, run_id: str) -> dict:
    """Read the latest atomically published progress or terminal result."""
    return json.loads((directory(store, run_id) / "run.json").read_text())


class ClipRun:
    """Bound memory, preserve partial results, and make every attempt inspectable."""

    def __init__(
        self,
        root: Path,
        match: dict,
        options: ClipOptions,
        section: dict | None = None,
    ) -> None:
        """Bind immutable inputs and establish a single wall-clock deadline.

        ``section`` is server-owned replay context (part number and the parent's
        gallery path). A section ends its decoding at a budget boundary instead
        of running into the deadline, and shares one roster identity with the
        recording's other sections.
        """
        self.root, self.match, self.options = root, match, options
        self.section = section
        self.started = time.monotonic()
        self.last_publish = self.started
        self.buffer: list[dict] = []
        self.timings = {"decode": 0.0, "inference": 0.0, "tracking": 0.0}
        self.record: dict[str, Any] = {
            "id": root.name,
            "schema_version": 1,
            "kind": "clip",
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
            "recipe": {"match_id": match["id"], "options": asdict(options)},
            "recording_title": match.get("title", match["id"]),
            "video": match["video"],
            "source_offset_seconds": match.get("source_offset_seconds", 0),
            "frames": 0,
            "chunks": [],
            "camera_cuts": 0,
            "active_ball_frames": 0,
            "court_frames": 0,
            "team_assigned": 0,
            "player_observations": 0,
            "review_only": True,
            "message": "Loading recording and model",
        }
        self.camera = Camera(options.court)
        self.people = People(options)
        self.balls = Balls()
        self.teams = Teams(options.team_colors)
        self.opening_teams = OpeningTeams()
        self.events = MatchEvents(options.court)
        self.recovery = PlayerRecovery(
            options, crops=os.environ.get("KORFBAL_CLIP_RECOVERY_CROPS", "0") == "1"
        )
        self.overlap = OverlapRecovery()
        self.refiner = IdentityRefiner()
        self.numbers: ShirtNumbers | None = None
        self.number_reads = NumberReads()
        self.placements = Placements()
        self.linker = Linker()
        self.background = Background()
        self.described_at = -math.inf
        self.appearance: Appearance | None = None
        self.broadcast_appearance: Appearance | GiantAppearance | None = None
        self.record["recipe"]["player_recovery"] = {
            "version": 1,
            "crop_search_enabled": self.recovery.crops,
        }

    def publish(self) -> None:
        """Commit immutable frame chunks before publishing their manifest entries."""
        if self.buffer and not (
            self.record["status"] == "running"
            and self.opening_teams.waiting(self.buffer)
        ):
            self.record["team_assigned"] += self.opening_teams.finish(
                self.buffer,
                self.teams,
                self.options.court,
                confirm=self.record["status"] in {"running", "completed"},
            )
            name = f"chunk-{len(self.record['chunks']):05d}.json"
            atomic_json(self.root / name, {"frames": self.buffer})
            self.record["chunks"].append({
                "name": name,
                "start": self.buffer[0]["time_seconds"],
                "end": self.buffer[-1]["time_seconds"],
                "frames": len(self.buffer),
                "sha256": digest(self.root / name),
            })
            self.buffer.clear()
        self.record["runtime_seconds"] = round(time.monotonic() - self.started, 2)
        self.record["timings_seconds"] = {
            key: round(value, 3) for key, value in self.timings.items()
        }
        self.record["processed_fps"] = round(
            self.record["frames"] / max(0.001, time.monotonic() - self.started), 2
        )
        self.record.update(self.events.snapshot())
        self.record["player_recovery"] = self.recovery.snapshot()
        self.record["overlap_recovery"] = self.overlap.snapshot()
        self.record["team_resolution"] = self.teams.spans.snapshot()
        if self.numbers is not None:
            self.record["shirt_numbers"] = {
                **self.record.get("shirt_numbers", {}),
                "status": "enabled",
                **self.numbers.snapshot(),
            }
        atomic_json(self.root / "run.json", self.record)
        self.last_publish = time.monotonic()

    def stopped(self) -> bool:
        """Check cancellation and the same deadline during decoding and inference."""
        if (self.root / "cancel.json").exists():
            self.record.update(
                status="cancelled", message="Stopped; partial results retained"
            )
        elif time.monotonic() - self.started > MAX_RUNTIME_SECONDS:
            self.record.update(
                status="interrupted",
                message="Runtime limit reached; partial results retained",
            )
        return self.record["status"] != "running"

    def budgeted(
        self, frames: Iterator[tuple[float, NDArray[Any]]]
    ) -> Iterator[tuple[float, NDArray[Any]]]:
        """Close a replay section at its budget; the replay resumes at that frame.

        Yields:
            Decoded frames until the section's share of the deadline is used.

        """
        end = self.options.start + self.options.duration
        for timestamp, image in frames:
            # Never close within the last second: the remainder would be too
            # short to analyse as its own section.
            if self.budget_spent() and end - timestamp >= MIN_CLIP_SECONDS:
                # Linking and identity then run within the remaining budget.
                self.record["section_end_seconds"] = round(timestamp, 6)
                return
            yield timestamp, image

    def budget_spent(self) -> bool:
        """Report whether a replay section has used its share of the deadline."""
        return (
            self.section is not None
            and self.record["frames"] > 0
            and time.monotonic() - self.started
            > SECTION_BUDGET_FRACTION * MAX_RUNTIME_SECONDS
        )

    def finish(self) -> None:
        """Publish a terminal receipt even when the last chunk cannot be encoded."""
        self.record["finished_at"] = datetime.now(UTC).isoformat()
        self.events.finish(self.record["status"])
        try:
            if self.record["status"] == "completed":
                started = time.monotonic()
                self.record["status"] = "running"
                self.record["identity_refinement"] = self.linked_identities(
                    self.refiner.finish(stopped=self.stopped)
                )
                if self.stopped():
                    self.record.pop("identity_refinement", None)
                else:
                    self.record["status"] = "completed"
                self.timings["identity_refinement"] = time.monotonic() - started
            self.publish()
        except Exception:
            self.record.pop("identity_refinement", None)
            self.record.update(
                status="failed",
                message="Could not publish final frames; earlier chunks retained",
                unpublished_frames=len(self.buffer),
            )
            atomic_json(self.root / "run.json", self.record)
            raise

    def frames(self, video: Path | str) -> Iterator[tuple[float, NDArray[Any]]]:
        """Decode sequentially, recording actual timestamps rather than guessed times.

        Yields:
            An actual source timestamp and one sampled BGR image.

        Raises:
            ValueError: The recording cannot supply the requested interval or cadence.

        """
        cv, _ = modules()
        capture = cv.VideoCapture(
            str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
        )
        try:
            fps = capture.get(cv.CAP_PROP_FPS)
            if (
                not capture.isOpened()
                or not math.isfinite(fps)
                or fps < self.options.fps
            ):
                raise ValueError("Recording cannot supply the requested frame rate")
            capture.set(cv.CAP_PROP_POS_MSEC, self.options.start * 1000)
            next_sample, last_timestamp = self.options.start, -1.0
            end = self.options.start + self.options.duration
            while next_sample < end - 1e-6 and not self.stopped():
                started = time.monotonic()
                ok = capture.grab()
                self.timings["decode"] += time.monotonic() - started
                if not ok:
                    if next_sample < end - 1 / fps - 1e-6:
                        raise ValueError(
                            "Recording ended before the requested interval"
                        )
                    break
                timestamp = capture.get(cv.CAP_PROP_POS_MSEC) / 1000
                if not math.isfinite(timestamp) or timestamp <= last_timestamp:
                    raise ValueError("Decoder timestamps must increase")
                last_timestamp = timestamp
                if timestamp + 1e-6 < next_sample:
                    continue
                if timestamp >= end:
                    break
                started = time.monotonic()
                ok, image = capture.retrieve()
                self.timings["decode"] += time.monotonic() - started
                if not ok:
                    raise ValueError("Could not retrieve the sampled frame")
                yield timestamp, image
                # Avoid duplicating frames when decoding a variable-rate source.
                next_sample = max(next_sample + 1 / self.options.fps, timestamp + 1e-6)
        finally:
            capture.release()

    def step(
        self,
        image: NDArray[Any],
        timestamp: float,
        result: object,
        detector: object | None = None,
    ) -> None:
        """Combine identities with independent colour, ball and floor signals."""
        raw = cast("Any", result)
        raw = self.overlap.refine(
            raw,
            image,
            self.teams,
            OverlapFrame(
                detector,
                self.timings["inference"],
                self.recovery.seconds,
                self.stopped,
                timestamp,
            ),
        )
        raw = cast("Any", raw)
        result = raw
        boxes = [
            [x1, y1, x2 - x1, y2 - y1] for x1, y1, x2, y2 in raw.boxes.xyxyn.tolist()
        ]
        observations = static_objects(
            raw, 0.1, labels=("ball", "basket", "player", "referee")
        )
        camera = self.camera.update(image, timestamp, boxes, observations)
        camera["calibration"]["suggested_points"] = suggestion(
            camera["floor"], self.options.court, camera["calibration"], boxes
        )
        if camera["cut"]:
            self.people.reset(camera["segment"])
            self.balls.reset(camera["segment"])
            self.teams.reset()
            self.record["camera_cuts"] += 1
        # A referee the detector calls a player would be tracked, recovered
        # and given a team as one: correct the detections for everything below.
        self.people.referee.correct(result, image, timestamp, camera["motion"])

        def recover(memory: IdentityMemory, observed: list[dict]) -> list[dict]:
            recovered = self.recovery.recover(
                memory,
                observed,
                RecoveryFrame(
                    result,
                    image,
                    timestamp,
                    camera,
                    detector,
                    self.timings["inference"],
                    self.stopped,
                    other_seconds=self.overlap.seconds,
                ),
            )
            # A crop search runs the detector again, outside the correction.
            return [
                obj
                for obj in recovered
                if not self.people.referee.covers(
                    obj.get("observed_bbox") or obj["bbox"], image, timestamp
                )
            ]

        self.people.identities.shirts.centers = self.teams.centers
        persons = self.people.update(
            result,
            image,
            timestamp,
            camera,
            recovery=recover if detector is not None else None,
        )
        self.teams.update(image, persons, timestamp)
        self.opening_teams.observe(self.teams, persons, timestamp, camera["segment"])
        if self.numbers is not None:
            self.numbers.attach(image, persons, timestamp, camera["segment"])
            self.number_reads.observe(persons, timestamp)
        self.placements.observe(persons, timestamp, self.options.court)
        self.refiner.observe(image, persons, timestamp, camera, shirts=self.teams)
        if self.appearance is not None:
            self.observe_identities(image, persons, timestamp, camera)
        detected = static_objects(result, self.options.confidence)
        balls, active = self.balls.update(
            [o for o in detected if o["label"] == "ball"],
            persons,
            timestamp,
            camera["motion"],
            court_key=court_reference(camera),
        )
        attach_post_distances(persons, self.options.court)
        objects = persons + balls + [o for o in detected if o["label"] == "basket"]
        possession = self.events.update(objects, active, timestamp, camera)
        self.buffer.append({
            "time_seconds": round(timestamp, 6),
            "segment": camera["segment"],
            "camera_cut": camera["cut"],
            "court_available": camera["floor"] is not None,
            "calibration": camera["calibration"],
            "active_ball": active,
            "possession": possession,
            "objects": objects,
            "top_down": top_down(
                objects, active, self.options.court, camera["calibration"]
            ),
        })
        self.record["frames"] += 1
        self.record["active_ball_frames"] += int(active["status"] == "observed")
        self.record["court_frames"] += int(camera["floor"] is not None)
        self.record["team_assigned"] += sum(
            o.get("team") in {"team_a", "team_b"} for o in persons
        )
        self.record["player_observations"] += sum(
            o["label"] == "player" for o in persons
        )
        self.record["team_colors"] = self.teams.colors()
        self.record["referee_correction"] = self.people.referee.snapshot()
        if (
            len(self.buffer) >= CHUNK_FRAMES
            or time.monotonic() - self.last_publish >= PROGRESS_SECONDS
        ):
            self.publish()

    def observe_identities(
        self, image: NDArray[Any], persons: list[dict], timestamp: float, camera: dict
    ) -> None:
        """Retain observed players, with appearance on sampled frames, for linking."""
        players = [
            # A copy: the colour is evidence for linking, not part of the frame.
            dict(
                obj,
                shirt_colour=torso_colour(
                    image, obj.get("observed_bbox") or obj["bbox"]
                ),
            )
            for obj in persons
            # Referees too: someone the detector calls a player in one frame
            # and a referee in another is one person to follow.
            if obj["label"] in {"player", "referee"} and not obj.get("estimated")
        ]
        descriptors = None
        if (
            self.appearance
            and timestamp - self.described_at >= APPEARANCE_INTERVAL - 1e-6
        ):
            self.described_at = timestamp
            descriptors = describe_players(self.appearance, image, players)
            if self.broadcast_appearance is not None:
                # Both descriptors of every box; the linker picks one per clip.
                alternates = describe_players(self.broadcast_appearance, image, players)
                descriptors = {
                    index: (value, alternates[index])
                    for index, value in descriptors.items()
                    if index in alternates
                }
            self.timings["appearance"] = self.appearance.seconds + (
                self.broadcast_appearance.seconds if self.broadcast_appearance else 0
            )
        height, width = image.shape[:2]
        boxes = [obj.get("observed_bbox") or obj["bbox"] for obj in persons]
        motion = self.background.update(image, boxes, cut=bool(camera["cut"]))
        self.linker.observe(
            players,
            round(timestamp, 6),
            {"segment": camera["segment"], "cut": camera["cut"], "motion": motion},
            aspect=width / height,
            descriptors=descriptors,
        )

    def linked_identities(self, refined: dict) -> dict:
        """Keep shot linking separate from the optional constrained match layer.

        Exactly one pass writes match-wide names. With a match roster the
        closed-set classifier does, and shirt numbers reach it as roster
        anchors; without one the legacy number pass joins numbered pieces and
        the constrained overlay only applies verified anchors.
        """
        linked = refined
        if self.appearance is not None and refined.get("status") == "completed":
            linked = self.linker.finish(self.stopped) or refined
        if linked.get("status") != "completed" or self.stopped():
            return linked
        payload = validate(self.options.match_identity)
        if payload.get("closed_set"):
            return self.closed_set_identities(linked, payload)
        # Legacy calibrated number splitting remains an upstream overlay. This
        # module consumes only its flattened links, just like within-shot links.
        linked = self.refiner.roster.finish(linked, self.stopped) or refined
        pieces = fragments(
            self.linker, linked, replay_shots=set(payload["replay_shots"])
        )
        numbers = readings(payload)
        for piece in pieces:
            piece.numbers = numbers.get(piece.identity, [])
        result = associate(
            pieces,
            anchors=anchors(payload),
            calibration=calibration(payload),
            settings=Settings(
                self.match["id"],
                payload.get("roster_size", 8),
                section_id=self.root.name,
            ),
            stopped=self.stopped,
        )
        return compose(linked, result)

    def number_evidence(
        self,
        pieces: list,
        owners: dict[tuple[float, str], str],
        closed: dict,
    ) -> tuple[list[KitRead], dict]:
        """Return confident shirt-number reads in kit tags, and a receipt.

        The review session decides which roster side wears which kit and turns
        reads into anchors only once that is known.
        """
        policy = NumberPolicy.from_receipt(self.record.get("shirt_numbers"))
        receipt: dict[str, Any] = {"version": 2, **self.number_reads.snapshot()}
        if policy is None or closed.get("numbers", "automatic") != "automatic":
            receipt["status"] = "disabled" if policy else "not_approved"
            return [], receipt
        reads = kit_reads(
            confident_numbers(pieces, tracklet_reads(owners, self.number_reads), policy)
        )
        receipt.update(
            status="enabled",
            policy={
                "threshold": policy.threshold,
                "min_support": policy.min_support,
                "provenance": policy.provenance,
            },
        )
        return reads, receipt

    def closed_set_identities(self, linked: dict, payload: dict) -> dict:
        """Expose roster questions and confirmations through the existing overlay."""
        closed = payload["closed_set"]
        ownership = None
        if closed.get("input", "tracklets") == "tracklets":
            pieces, ownership = pure_fragments(
                self.linker, linked, replay_shots=set(payload["replay_shots"])
            )
            owners = {
                (round(self.linker.rows[i].time, 6), self.linker.rows[i].track_id): f
                for i, f in ownership.items()
            }
        else:
            pieces = fragments(
                self.linker,
                linked,
                replay_shots=set(payload["replay_shots"]),
                dimensions=128,
            )
            owners = {
                (round(self.linker.rows[i].time, 6), self.linker.rows[i].track_id): f
                for i, (f, _) in resolved(self.linker, linked).items()
            }
        numbers = readings(payload)
        placed = self.placements.counts(owners)
        for piece in pieces:
            piece.numbers = numbers.get(piece.identity, [])
            piece.placement = placed.get(piece.identity, (0, 0))
        linking = self.record.get("identity_linking", {})
        # The roster classifier uses the descriptor the linker chose for this clip.
        checksum = (
            linking.get("broadcast_appearance", {}).get("sha256", "unavailable")
            if self.linker.broadcast
            else linking.get("sha256", "unavailable")
        )
        recipe = f"{checksum}:{CLOSED_SET_RECIPE}:{closed.get('input', 'tracklets')}"
        if self.section is not None and ownership is not None and not self.stopped():
            self.save_match_evidence(linked, payload, pieces, owners, checksum)
        raw_calibration = closed.get("calibration")
        # Trusted options win; otherwise the descriptor's measured calibration
        # (development clips) decides which appearance names are published.
        confidence = (
            Confidence(
                raw_calibration["provenance"],
                raw_calibration["descriptor_recipe"],
                tuple(tuple(b) for b in raw_calibration["bins"]),
            )
            if raw_calibration
            else calibrated(
                checksum,
                closed.get("input", "tracklets"),
                fixed_camera=self.linker.fixed,
            )
        )
        roster = [Player(**p) for p in closed["roster"]]
        gallery = self.gallery(checksum)
        remembered: list = []
        remembered_anchors: list = []
        ledger: list = []
        section = self.gallery_section()["id"] if self.section is not None else ""
        if gallery is not None:
            # Players earlier sections' reviewers added are on the roster too.
            roster, _, _ = merge_additions(
                roster, list(starmap(Player, added_players(gallery)))
            )
            # Earlier sections' named players: pieces in the kit they wore,
            # anchors on their roster side; and every other section's
            # kit-orientation sightings, so the recording decides as one.
            remembered, remembered_anchors = carried(gallery, roster)
            ledger = recorded(gallery, section)
        reads, receipt = self.number_evidence(pieces, owners, closed)
        current = list(pieces)
        if remembered:
            pieces = deepcopy([*pieces, *remembered])
            fit_fragments(pieces, dimensions=128)
            current = pieces[: len(current)]
        session = ReviewSession(
            Scope(self.match["id"], self.root.name),
            pieces,
            roster,
            ClosedSetSettings(recipe, confidence),
            Evidence(
                (*anchors(payload), *remembered_anchors),
                tuple(reads),
                kits=closed.get("orientation", "automatic") == "kits",
                ledger=tuple(ledger),
                section=section,
            ),
        )
        kits = session.result.get("orientation", {})
        receipt.update(
            orientation=kits,
            **named_numbers(pieces, reads, roster, kits.get("swapped"))[1],
        )
        result = {
            **session.result,
            "review": session.snapshot(),
            "number_anchors": receipt,
        }
        if self.stopped() or result["status"] != "completed":
            return linked
        if gallery is not None and self.section is not None:
            # Confirmed views always; views named by number only exist under a
            # decided orientation and record it. The section's sightings join
            # the recording's ledger either way.
            result["recording_gallery"] = {
                "carried_players": len(remembered),
                **remember(
                    gallery,
                    self.gallery_section(),
                    current,
                    result,
                    oriented(roster, swapped=session.swapped),
                    session.sightings(),
                ),
            }
        tagged = {
            **linked,
            **{
                key: [
                    {**link, "fragment_identity": link["to_track_id"]}
                    for link in linked.get(key, [])
                ]
                for key in ("links", "frame_links")
            },
        }
        refinement = (
            compose_tracklets(
                self.linker,
                linked,
                result,
                ownership,
                propagate=bool(closed.get("propagate", False)),
            )
            if ownership is not None
            else compose(tagged, result)
        )
        # A section's review corrects the gallery views it committed and revises
        # the ones it carried when another section's reviewer corrects them.
        link = (
            GalleryLink(
                Path(str((self.section or {})["gallery_path"])).parent.name,
                self.gallery_section()["id"],
                checksum,
            )
            if gallery is not None and self.section is not None
            else None
        )
        ReviewCache(
            self.root / "identity-review.sqlite",
            Scope(session.namespace, session.fingerprint),
        ).create(session, refinement=refinement, gallery=link)
        return refinement

    def save_match_evidence(
        self, linked: dict, payload: dict, pieces: list, owners: dict, checksum: str
    ) -> None:
        """Write this section's evidence for the recording's match pass.

        The evidence always uses the strongest descriptor of the run: a section
        whose linker judged it a fixed camera (a studio shot at half-time, say)
        still describes its tracklets like the other sections, so one match
        appearance space covers them all.
        """
        linking = self.record.get("identity_linking", {})
        strong = linking.get("broadcast_appearance", {}).get("sha256")
        if strong and not self.linker.broadcast and self.linker.alternates:
            self.linker.broadcast = True
            try:
                pieces, ownership = pure_fragments(
                    self.linker, linked, replay_shots=set(payload["replay_shots"])
                )
            finally:
                self.linker.broadcast = False
            owners = {
                (round(self.linker.rows[i].time, 6), self.linker.rows[i].track_id): f
                for i, f in ownership.items()
            }
            placed = self.placements.counts(owners)
            for piece in pieces:
                piece.placement = placed.get(piece.identity, (0, 0))
            checksum = strong
        self.record["match_evidence"] = save_match_evidence(
            self.root,
            self.gallery_section(),
            pieces,
            tracklet_reads(owners, self.number_reads),
            {
                "descriptor_sha256": checksum,
                "numbers": self.record.get("shirt_numbers"),
                "team_colors": self.teams.colors(),
                "fixed_camera": self.linker.fixed,
            },
        )

    def gallery(self, recipe: str) -> RecordingGallery | None:
        """Open the parent replay's gallery for this recording and descriptor."""
        path = (self.section or {}).get("gallery_path")
        if not path:
            return None
        return open_gallery(Path(path), str(self.match["id"]), recipe)

    def gallery_section(self) -> dict:
        """Describe the owned source range and immutable fingerprint."""
        start = self.options.start
        end = self.record.get("section_end_seconds") or start + self.options.duration
        return {
            "id": f"part-{int((self.section or {}).get('part', 0)):04d}",
            "start": start,
            "end": end,
            "fingerprint": section_fingerprint({
                "run": self.root.name,
                "video": self.record.get("video_sha256"),
                "weights": self.record.get("weights_sha256"),
                "linking": self.record.get("identity_linking", {}).get("sha256"),
                "start": start,
                "end": end,
            }),
        }

    def load_appearance(self, cache: Path) -> None:
        """Load the descriptor model and, if configured, the broadcast one."""
        self.appearance, self.record["identity_linking"] = beside_cache(cache)
        self.broadcast_appearance, broadcast = beside_broadcast(cache)
        if broadcast is not None and self.appearance is not None:
            self.record["identity_linking"]["broadcast_appearance"] = broadcast
        else:
            self.broadcast_appearance = None

    def execute(self, store: Store, weights: str) -> None:
        """Load one model, stream inference, and always publish a terminal receipt.

        Raises:
            ValueError: The model is incompatible or no frames were decoded.

        """
        atomic_json(self.root / "run.json", self.record)
        try:
            with store.video_source(self.match["video"]) as video:
                if self.stopped():
                    return
                cv, _ = modules()
                cv.setNumThreads(CPU_THREADS)
                torch = importlib.import_module("torch")
                torch.set_num_threads(CPU_THREADS)
                self.record["video_sha256"] = (
                    self.match["video_sha256"]
                    if video.startswith("http://127.0.0.1:")
                    else digest(Path(video))
                )
                model = cast(
                    "Any", clip_detector(weights, store.root / "vision" / "cpu-cache")
                )
                self.record.update(weights_record(weights, model))
                expected = self.record["recipe"].get("weights_sha256")
                if expected and self.record["weights_sha256"] != expected:
                    raise ValueError("Checkpoint changed while the clip was starting")
                self.record["environment"] = environment()
                self.numbers, self.record["shirt_numbers"] = load_numbers(weights)
                self.load_appearance(store.root / "vision" / "cpu-cache")
                if not supports_clips(list(model.names.values())):
                    self.record["failure_code"] = "incompatible_model"
                    raise ValueError(MODEL_ERROR)
                self.prepare_court(video, model)
                if self.stopped():
                    return
                self.record["message"] = "Analyzing clip"
                self.publish()
                for timestamp, image in self.budgeted(self.frames(video)):
                    started = time.monotonic()
                    result = model.predict(
                        image,
                        device="cpu",
                        imgsz=self.options.imgsz,
                        conf=0.1,
                        max_det=80,
                        verbose=False,
                    )[0]
                    self.timings["inference"] += time.monotonic() - started
                    self.record["inference"] = getattr(
                        model,
                        "inference_info",
                        {"backend": "torch", "precision": "fp32"},
                    )
                    # Ultralytics CPU setup can reset the pool after device changes.
                    torch.set_num_threads(CPU_THREADS)
                    self.record["cpu_threads"] = torch.get_num_threads()
                    if self.stopped():
                        break
                    started = time.monotonic()
                    recovery_before = self.recovery.seconds + self.overlap.seconds
                    self.step(image, timestamp, result, model)
                    self.timings["tracking"] += (
                        time.monotonic()
                        - started
                        - (
                            self.recovery.seconds
                            + self.overlap.seconds
                            - recovery_before
                        )
                    )
                    self.timings["recovery"] = (
                        self.recovery.seconds + self.overlap.seconds
                    )
                if self.record["status"] == "running":
                    if not self.record["frames"]:
                        raise ValueError("No frames were decoded")
                    self.record.update(
                        status="completed", message="Clip ready for inspection"
                    )
        except Exception as error:
            self.record.update(
                status="failed",
                message=failure_message(
                    self.record,
                    f"Clip failed ({type(error).__name__}); partial results kept",
                ),
            )
            raise
        finally:
            self.finish()

    def prepare_court(self, video: Path | str, model: object) -> None:
        """Decode exact reference frames within the same inference deadline.

        Raises:
            ValueError: A reference frame cannot be decoded accurately.

        """
        if self.camera.automatic:
            importlib.import_module(f"{__package__}.clip_auto_prepare").prepare(
                self, video, model
            )
            return
        if not self.camera.mapping:
            return
        cv, _ = modules()
        torch = importlib.import_module("torch")
        capture = cv.VideoCapture(
            str(video), cv.CAP_FFMPEG, [cv.CAP_PROP_N_THREADS, CPU_THREADS]
        )
        prepared = []
        try:
            for anchor in self.camera.mapping.court["anchors"]:
                if self.stopped():
                    return
                capture.set(cv.CAP_PROP_POS_MSEC, anchor["time"] * 1000)
                ok, image = capture.read()
                if (
                    not ok
                    or abs(capture.get(cv.CAP_PROP_POS_MSEC) / 1000 - anchor["time"])
                    > REFERENCE_TIME_TOLERANCE
                ):
                    raise ValueError("Could not decode the requested reference frame")
                result = cast("Any", model).predict(
                    image,
                    device="cpu",
                    imgsz=self.options.imgsz,
                    conf=0.1,
                    max_det=80,
                    verbose=False,
                )[0]
                torch.set_num_threads(CPU_THREADS)
                boxes = [
                    [a, b, c - a, d - b] for a, b, c, d in result.boxes.xyxyn.tolist()
                ]
                prepared.append(self.camera.mapping.add_reference(image, anchor, boxes))
                self.record["court_references"] = prepared
                self.record["message"] = (
                    f"Prepared {len(prepared)} court reference frames"
                )
                self.publish()
        finally:
            capture.release()


def static_objects(
    raw: object, confidence: float, *, labels: tuple[str, ...] = ("ball", "basket")
) -> list[dict]:
    """Preserve basket and ball observations independently of people association."""
    result = cast("Any", raw)
    detected = []
    boxes = result.boxes.cpu().numpy()
    corners = boxes.xyxyn.tolist()
    for box, cls, score, foot in zip(
        corners,
        boxes.cls.tolist(),
        boxes.conf.tolist(),
        post_feet(result, len(corners)),
        strict=True,
    ):
        label = result.names[int(cls)]
        if label not in labels or score < confidence:
            continue
        x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in box]
        if x2 > x1 and y2 > y1:
            detected.append({
                "label": label,
                "bbox": [x1, y1, x2 - x1, y2 - y1],
                "confidence": float(score),
                **({"post_foot": foot} if label == "basket" and foot else {}),
            })
    return detected


def analyze(
    store: Store, run_id: str, match: dict, weights: str, options: ClipOptions
) -> dict:
    """Run once; never save human labels or silently repeat an interrupted attempt."""
    return launch(
        store, run_id, {"match": match, "weights": weights, "options": options}
    )


def launch(store: Store, run_id: str, request: dict) -> dict:
    """Run a request: match, weights, parsed options and optional replay section.

    The section is server-owned context from ``adapters/replay.py``; its gallery
    path is resolved inside this store, never taken from a client.

    Raises:
        ValueError: The identifier already belongs to another or interrupted run.

    """
    match, weights, options = request["match"], request["weights"], request["options"]
    section = request.get("section")
    options.for_recording(match)
    root = directory(store, run_id)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "run.json"
    recipe = {
        "match_id": match["id"],
        "options": asdict(options),
        "weights_sha256": digest(Path(weights)),
    }
    if marker.exists():
        previous = json.loads(marker.read_text())
        if previous.get("recipe") != recipe:
            raise ValueError("Clip ID belongs to a different request")
        if previous["status"] == "completed":
            return previous
        raise ValueError("This attempt already started; create a new run to retry")
    if section is not None:
        section = {
            "part": int(section.get("part", 0)),
            "gallery_path": str(directory(store, str(section["parent"])) / GALLERY_FILE)
            if section.get("parent")
            else None,
        }
    run = ClipRun(root, match, options, section)
    run.record["recipe"] = recipe
    run.execute(store, weights)
    return run.record


WORKER_PID = "KORFBAL_WORKER_PID"
PR_SET_PDEATHSIG = 1


def bind_to_worker() -> None:
    """Die with the worker that started this run, never outlive it.

    A replay section whose worker died is restarted in its own directory by a
    redelivered task, so an orphaned run must not keep writing there. Linux
    kills this process when its parent dies; a parent that died before that
    was set is caught by the parent check.

    Raises:
        SystemExit: The worker that started this run is already gone.

    """
    expected = os.environ.get(WORKER_PID)
    if not expected or sys.platform != "linux":
        return
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != int(expected):
        raise SystemExit("The worker that started this run is gone")


def main() -> None:
    """Run a fresh database projection in the isolated detector environment."""
    bind_to_worker()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    launch(
        ProjectionStore(args.root, args.input),
        payload["run_id"],
        {
            "match": payload["match"],
            "weights": payload["weights"],
            "options": ClipOptions.parse(payload["options"]),
            "section": payload.get("section"),
        },
    )


if __name__ == "__main__":
    main()
