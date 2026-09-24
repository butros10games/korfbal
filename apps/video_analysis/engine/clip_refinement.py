"""Use both ends of a short gap to propose conservative replay identity links.

The original frame chunks remain immutable. These links rename observed people
in replay only; they neither invent boxes nor change event/training evidence.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
import math
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from . import (
    clip_refinement_backward as backward,
    clip_refinement_occlusion as occlusion,
    clip_refinement_ownership as ownership,
)
from .clip_identity import IdentityMemory, court_reference
from .clip_refinement_spans import ObservationSpans
from .clip_segment_reconciliation import reconcile
from .clip_signals import Teams, modules, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray


MAX_GAP = 1.2
MAX_TRACKS = 3000
ENDPOINT_SAMPLES = 5
MIN_SAMPLES = 3
MAX_APPEARANCE = 0.32
MAX_COST = 0.72
MIN_MARGIN = 0.18
MIN_PATCH_WIDTH = 6
MIN_PATCH_HEIGHT = 24
SAMPLE_INTERVAL = 0.075
ENDPOINT_TOLERANCE = 0.24
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55
MIN_HISTORY_SECONDS = 0.15
MAX_SPEED_METRES = 9


def appearance(image: NDArray[Any], box: list) -> list | None:
    """Describe clothing in four body bands; no face or biometric recognition.

    Small HSV histograms preserve more clothing detail than mean shirt colour.
    They are deliberately not called a learned person re-identification model.
    """
    cv, np = modules()
    h, w = image.shape[:2]
    x, y, width, height = box
    if x <= 0 or y <= 0 or x + width >= 1 or y + height >= 1:
        return None
    left, right = round((x + width * 0.2) * w), round((x + width * 0.8) * w)
    top, bottom = round((y + height * 0.18) * h), round((y + height * 0.96) * h)
    if right - left < MIN_PATCH_WIDTH or bottom - top < MIN_PATCH_HEIGHT:
        return None
    patch = cv.resize(image[top:bottom, left:right], (16, 48))
    hsv = cv.cvtColor(patch, cv.COLOR_BGR2HSV)
    descriptor = []
    for band in np.array_split(hsv, 4):
        chroma = cv.calcHist([band], [0, 1], None, [8, 4], [0, 180, 0, 256])
        light = cv.calcHist([band], [2], None, [8], [0, 256])
        descriptor.append(
            np.concatenate((chroma.ravel() * 0.75, light.ravel() * 0.25))
            / band.shape[0]
            / band.shape[1]
        )
    return np.asarray(descriptor).tolist()


def appearance_distance(a: list, b: list) -> float:
    """Average Hellinger distance across corresponding clothing bands."""
    _, np = modules()
    return float(np.sqrt(np.square(np.sqrt(a) - np.sqrt(b)).sum(axis=1) / 2).mean())


class IdentityRefiner:
    """Collect bounded endpoint and observation evidence without retaining images."""

    def __init__(self) -> None:
        """Start one independent section with an explicit camera coordinate epoch."""
        _, self.np = modules()
        self.tracks: dict[str, dict] = {}
        self.epoch = 0
        self.warp = self.np.eye(3)
        self.truncated = False
        self.confirmed: dict[str, dict] = {}
        self.spans = ObservationSpans()

    def advance(self, camera: dict) -> NDArray[Any] | None:
        """Use a new epoch whenever camera compensation becomes unavailable."""
        motion = camera.get("motion")
        if camera.get("cut") or motion is None:
            self.epoch += 1
            self.warp = self.np.eye(3)
        else:
            self.warp = motion @ self.warp
        try:
            inverse = self.np.linalg.inv(self.warp)
        except self.np.linalg.LinAlgError:
            self.epoch += 1
            self.warp = self.np.eye(3)
            return None
        return inverse

    def observe(
        self,
        image: NDArray[Any],
        objects: list[dict],
        time: float,
        camera: dict,
        *,
        shirts: Teams | None = None,
    ) -> None:
        """Retain clean clothing samples and positions in a shared camera space."""
        inverse = self.advance(camera)
        if inverse is None:
            return
        key = court_reference(camera)
        trusted = key is not None and not camera.get("calibration", {}).get("estimated")
        objects = [obj for obj in objects if obj["label"] in {"player", "referee"}]
        for obj in (obj for obj in objects if obj["label"] == "player"):
            identity = obj["track_id"]
            box = obj["observed_bbox"]
            clear = IdentityMemory.clear_torso(obj, objects)
            descriptor = appearance(image, box) if clear else None
            x, y, w, h = box
            body = transform([x + w / 2, y + h * 0.45], inverse)
            feet = transform([x + w / 2, y + h], inverse)
            head = transform([x + w / 2, y], inverse)
            if body is None or feet is None or head is None:
                continue
            shirt = shirts.vote(shirts.observations.get(identity)) if shirts else None
            sample = {
                "time": time,
                "image": body,
                "height": math.dist(feet, head),
                "epoch": self.epoch,
                "court": obj.get("court_xy_m") if trusted else None,
                "court_key": key if trusted else None,
                "appearance": descriptor,
                "team": obj.get("team", "unknown"),
                "shirt_team": f"team_{'ab'[shirt[0]]}"
                if shirt
                else "unknown"
                if shirts
                else obj.get("team", "unknown"),
            }
            self.spans.observe({
                "id": identity,
                "display_id": obj.get("display_id"),
                "time": time,
                "segment": camera.get("segment", 0),
                "epoch": self.epoch,
                "image": body,
                "height": sample["height"],
                "uncertain": bool(obj.get("identity_uncertain")),
                "box": list(box),
                "confidence": obj.get("confidence", 0),
                "sample": sample if descriptor is not None else None,
            })
            if obj.get("identity_uncertain"):
                continue
            if not self.ensure_track(obj, camera, time):
                continue
            track = self.tracks[identity]
            self.confirm(obj, track, time)
            track["end"] = time
            if obj.get("team") in {"team_a", "team_b"}:
                track["teams"].add(obj["team"])
            occlusion.observe(track, obj, objects, time, occluded=not clear)
            if descriptor is None:
                continue
            if track["last"] and time - track["last"][-1]["time"] < SAMPLE_INTERVAL:
                continue
            if len(track["first"]) < ENDPOINT_SAMPLES:
                track["first"].append(sample)
            track["last"] = (track["last"] + [sample])[-ENDPOINT_SAMPLES:]

    def ensure_track(self, obj: dict, camera: dict, time: float) -> bool:
        """Bound endpoint history to established identities."""
        identity = obj["track_id"]
        if identity not in self.tracks:
            if len(self.tracks) >= MAX_TRACKS:
                self.truncated = True
                return False
            self.tracks[identity] = {
                "id": identity,
                "display_id": obj.get("display_id"),
                "segment": camera.get("segment", 0),
                "start": time,
                "first": [],
                "last": [],
                "teams": set(),
            }
        return True

    def confirm(self, obj: dict, track: dict, time: float) -> None:
        """Backfill a provisional track only after online multi-frame confirmation."""
        confirmation = obj.get("identity_confirmation")
        if not confirmation:
            return
        source = self.tracks.get(confirmation["from_track_id"])
        if (
            source
            and source["end"] < time
            and source["segment"] == track["segment"]
            and confirmation["to_track_id"] == obj["track_id"]
        ):
            self.confirmed[source["id"]] = dict(confirmation)

    def cost(self, before: dict, after: dict, *, use_appearance: bool) -> float:
        """Require forward and backward motion, clear endpoints and compatible kit."""
        gap = after["start"] - before["end"]
        if (
            before["id"] == after["id"]
            or not 0 < gap <= MAX_GAP
            or before["segment"] != after["segment"]
            or len(before["teams"] | after["teams"]) > 1
        ):
            return math.inf
        a, b = before["last"], after["first"]
        if min(len(a), len(b)) < MIN_SAMPLES:
            return math.inf
        # No extrapolation from stale/occluded samples or barely established IDs.
        if (
            before["end"] - a[-1]["time"] > ENDPOINT_TOLERANCE
            or b[0]["time"] - after["start"] > ENDPOINT_TOLERANCE
        ):
            return math.inf
        motion = self.motion_error(a, b)
        clothing = self.clothing_error(a, b) if use_appearance else 0.0
        if motion > 1 or clothing > MAX_APPEARANCE:
            return math.inf
        return 0.55 * motion + 0.45 * clothing / MAX_APPEARANCE

    def motion_error(self, a: list, b: list) -> float:
        """Measure both extrapolations in metres or camera-compensated body units."""
        metric = a[-1]["court_key"] is not None and all(
            s["court_key"] == a[-1]["court_key"] and s["court"] is not None
            for s in a + b
        )
        if not metric and len({s["epoch"] for s in a + b}) != 1:
            return math.inf
        axis = "court" if metric else "image"
        height = (a[-1]["height"] + b[0]["height"]) / 2
        ratio = a[-1]["height"] / max(1e-9, b[0]["height"])
        if not MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO:
            return math.inf
        dt = b[0]["time"] - a[-1]["time"]
        radius = (0.5 + 2 * dt) if metric else height * (0.3 + 0.8 * dt)
        errors = []
        for samples, anchor, target, sign in (
            (a, a[-1], b[0], 1),
            (b, b[0], a[-1], -1),
        ):
            elapsed = samples[-1]["time"] - samples[0]["time"]
            if elapsed < MIN_HISTORY_SECONDS:
                return math.inf
            velocity = (self.np.array(samples[-1][axis]) - samples[0][axis]) / elapsed
            if metric and self.np.linalg.norm(velocity) > MAX_SPEED_METRES:
                return math.inf
            expected = self.np.array(anchor[axis]) + velocity * dt * sign
            errors.append(float(self.np.linalg.norm(expected - target[axis])) / radius)
        return max(errors)

    def clothing_error(self, a: list, b: list) -> float:
        """Aggregate endpoint clothing samples, keeping normalized histograms."""
        first = self.np.median([s["appearance"] for s in a], axis=0)
        second = self.np.median([s["appearance"] for s in b], axis=0)
        first /= self.np.maximum(first.sum(axis=1, keepdims=True), 1e-9)
        second /= self.np.maximum(second.sum(axis=1, keepdims=True), 1e-9)
        return appearance_distance(first, second)

    def candidates(
        self, *, use_appearance: bool, stopped: Callable[[], bool] | None
    ) -> list[tuple[float, str, str]] | None:
        """Limit comparisons to neighbouring time windows within the job deadline."""
        ordered = sorted(self.tracks.values(), key=itemgetter("start"))
        starts = [track["start"] for track in ordered]
        candidates: list[tuple[float, str, str]] = []
        for before in ordered:
            if stopped and stopped():
                return None
            for after in ordered[bisect_right(starts, before["end"]) :]:
                if after["start"] > before["end"] + occlusion.MAX_OCCLUSION_GAP:
                    break
                cost = self.cost(before, after, use_appearance=use_appearance)
                if use_appearance:
                    cost = min(cost, occlusion.cost(self, before, after))
                if math.isfinite(cost):
                    candidates.append((cost, before["id"], after["id"]))
        return candidates

    def finish(
        self, *, use_appearance: bool = True, stopped: Callable[[], bool] | None = None
    ) -> dict:
        """Return mutually unique links; never join simultaneous identities."""
        candidates = self.candidates(use_appearance=use_appearance, stopped=stopped)
        interrupted = {
            "version": 1,
            "status": "interrupted",
            "links": [],
            "review_only": True,
        }
        if candidates is None:
            return interrupted
        links: list[dict] = list(self.confirmed.values())
        aliases: dict[str, str] = {
            link["from_track_id"]: link["to_track_id"] for link in links
        }
        confirmed_ids = set(aliases) | set(aliases.values())
        rejected: Counter = Counter()
        for cost, source, target in sorted(candidates):
            if stopped and stopped():
                return interrupted
            if cost >= MAX_COST or source in confirmed_ids or target in confirmed_ids:
                continue
            if any(
                (other_source == source or other_target == target)
                and (other_source, other_target) != (source, target)
                and other_cost - cost < MIN_MARGIN
                for other_cost, other_source, other_target in candidates
            ):
                rejected["ambiguous"] += 1
                continue
            aliases[target] = source
            links.append({
                "from_track_id": target,
                "to_track_id": source,
                "display_id": self.tracks[source]["display_id"],
                "gap_seconds": round(
                    self.tracks[target]["start"] - self.tracks[source]["end"], 3
                ),
                "evidence_score": round(1 - cost, 3),
            })
        # Flatten chains so every consumer resolves identities identically.
        for link in links:
            canonical = link["to_track_id"]
            while canonical in aliases:
                canonical = aliases[canonical]
            link["to_track_id"] = canonical
            link["display_id"] = self.tracks[canonical]["display_id"]
        frame_links = self.spans.finish(self, links, stopped) if use_appearance else []
        if frame_links is None:
            return interrupted
        frame_links = (
            self.reconcile_frames(links, frame_links, stopped) if use_appearance else []
        )
        if frame_links is None:
            return interrupted
        return {
            "version": 1,
            "status": "completed",
            "appearance": "clothing_histograms" if use_appearance else "disabled",
            "tracks": len(self.tracks),
            "truncated": self.truncated or self.spans.truncated,
            "rejected": dict(rejected),
            "links": links,
            "frame_links": frame_links,
            "review_only": True,
        }

    def reconcile_frames(
        self, links: list, frame_links: list, stopped: Callable[[], bool] | None
    ) -> list | None:
        """Resolve body swaps before two bounded passes over original clean views."""
        recovered = backward.recover(self, links, frame_links, stopped)
        if recovered is None:
            return None
        replaced = {(c["time_seconds"], c["from_track_id"]) for c in recovered}
        frame_links = [
            c
            for c in frame_links
            if (c["time_seconds"], c["from_track_id"]) not in replaced
        ] + recovered
        for _ in range(2):
            segment_links = reconcile(self, links, frame_links, stopped)
            if segment_links is None:
                return None
            known = {(c["time_seconds"], c["from_track_id"]) for c in frame_links}
            frame_links.extend(
                c
                for c in segment_links
                if (c["time_seconds"], c["from_track_id"]) not in known
            )
        return frame_links


def refined_frames(frames: list[dict], report: dict) -> list[dict]:
    """Apply replay aliases to observed boxes for independent benchmark scoring."""
    if report.get("status") != "completed":
        return frames
    aliases = {link["from_track_id"]: link for link in report["links"]}
    scoped = {
        (link["time_seconds"], link["from_track_id"]): link
        for link in report.get("frame_links", [])
    }
    output = []
    for frame in frames:
        frame_links = {
            identity: link
            for (time, identity), link in scoped.items()
            if time == frame.get("time_seconds")
        }
        superseded = ownership.superseded_ids(frame["objects"], frame_links, aliases)
        objects = []
        for obj in frame["objects"]:
            if obj["track_id"] in superseded:
                continue
            link = scoped.get(
                (frame.get("time_seconds"), obj["track_id"]),
                aliases.get(obj["track_id"]),
            )
            resolved = obj
            if link:
                resolved = dict(
                    obj,
                    track_id=link["to_track_id"],
                )
                if link.get("display_id") is not None:
                    resolved["display_id"] = link["display_id"]
                if link.get("team") in {"team_a", "team_b"}:
                    resolved["team"] = link["team"]
            objects.append(resolved)
        ids = [obj["track_id"] for obj in objects]
        output.append(
            dict(frame, objects=objects) if len(ids) == len(set(ids)) else frame
        )
    return output
