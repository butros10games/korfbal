"""Fixed-position camera calibration: one hall pose, pan/tilt/zoom per frame.

A broadcast camera rotates and zooms about a fixed optical centre. Its whole
image, including stands, walls and banners, then moves by a rotation homography,
so a view without visible court markings can still be registered. One solve over
every sampled frame shares each court observation with the whole camera: earlier
and later frames are calibrated together, not only after the first observation.

Views that cannot be explained by a rotation of the same camera form separate
groups. A group without its own court observation stays uncalibrated.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from . import clip_geometry as geometry
from .clip_hall_geometry import centring, features, floor_matrix, track, without
from .clip_hall_solver import MAX_KEYFRAMES, Solver
from .clip_signals import modules


if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

PROMOTE_INLIERS = 150
PROMOTE_INTERVAL = 1.0
SEARCH_INTERVAL = 0.5
MAX_SEARCH = 8
TRACKED_CANDIDATES = 2
MAX_CHAIN_SECONDS = 20.0
MAX_CONTINUITY_GAP = 0.5
MAX_STEP_DEGREES = 12.0
MAX_ZOOM_STEP = 1.35


class Hall:
    """Locate every frame of a calibrated fixed camera; abstain on other cameras."""

    def __init__(self, court: dict) -> None:
        """Start without calibration; `prepare` supplies the joint solve."""
        self.court = court
        self.solver = Solver(court)
        self.keyframes: list[dict] = []
        self.previous: dict | None = None
        self.last_search = -math.inf
        self.last_promotion = -math.inf
        self.groups: list[dict] = []
        self.summary: dict = {"groups": []}

    def calibrate(self, stopped: Callable[[], bool] | None = None) -> dict:
        """Solve all sampled views together and keep posed keyframes."""
        groups = self.solver.solve(stopped)
        self.keyframes = []
        for number, group in enumerate(groups):
            state = group["state"]
            for k in group["members"]:
                self.keyframes.append({
                    "time": self.solver.keyframes[k]["time"],
                    "features": self.solver.keyframes[k]["features"],
                    "rotation": state["rotation"][k],
                    "focal": state["focal"][k],
                    "group": number,
                })
        self.groups = groups
        self.summary = {
            "keyframes": len(self.solver.keyframes),
            "matched_pairs": len(self.solver.edges),
            "failures": self.solver.diagnostics,
            "groups": [
                {
                    "keyframes": len(g["members"]),
                    "basket_observations": g["observations"],
                    "rotation_error_pixels": g["rotation_error_pixels"],
                    "basket_error_pixels": g["observation_error_pixels"],
                    "camera_height_m": round(abs(float(g["state"]["centre"][2])), 2),
                    "reference_time": g["reference_time"],
                }
                for g in groups
            ],
        }
        return self.summary

    def candidates(self, timestamp: float, cut: bool) -> list[dict]:
        """Prefer views near the previous pose; otherwise search by time."""
        _, np = modules()
        previous = self.previous
        if (
            previous is not None
            and not cut
            and timestamp - previous["time"] <= MAX_CONTINUITY_GAP
        ):
            axis = previous["rotation"][2]
            same = [k for k in self.keyframes if k["group"] == previous["group"]]
            return sorted(
                same,
                key=lambda k: (
                    float(np.arccos(np.clip(k["rotation"][2] @ axis, -1, 1)))
                    + abs(math.log(k["focal"] / previous["focal"]))
                ),
            )[:TRACKED_CANDIDATES]
        if timestamp - self.last_search < SEARCH_INTERVAL and not cut:
            return []
        self.last_search = timestamp
        return sorted(self.keyframes, key=lambda k: abs(k["time"] - timestamp))[
            :MAX_SEARCH
        ]

    def register(self, current: dict, timestamp: float, cut: bool) -> tuple | None:
        """Match a posed keyframe, else chain briefly from the previous frame."""
        for keyframe in self.candidates(timestamp, cut):
            found = track(keyframe, current)
            if found:
                return found, keyframe, False
        previous = self.previous
        if (
            previous is not None
            and timestamp - previous["time"] <= MAX_CONTINUITY_GAP
            and timestamp - previous["anchored"] <= MAX_CHAIN_SECONDS
        ):
            found = track(previous, current)
            if found:
                return found, previous, True
        return None

    def locate(
        self, image: NDArray[Any], timestamp: float, objects: list, cut: bool
    ) -> tuple | None:
        """Return a floor and evidence, or None when this view is not calibrated."""
        if not self.keyframes:
            return None
        _, np = modules()
        current = without(
            features(image, [o["bbox"] for o in objects]), self.solver.overlay
        )
        previous = self.previous
        registered = self.register(current, timestamp, cut)
        if registered is None:
            self.previous = None
            return None
        ((rotation, focal, error), inliers), source, chained = registered
        group = self.groups[source["group"]]
        centre = group["state"]["centre"]
        floor = floor_matrix(rotation, focal, centre, current["aspect"])
        try:
            geometry.footprint(floor, self.court)
        except ValueError:
            self.previous = None
            return None
        continuous = False
        motion = None
        if (
            previous is not None
            and previous["group"] == source["group"]
            and timestamp - previous["time"] <= MAX_CONTINUITY_GAP
        ):
            step = previous["rotation"].T @ rotation
            angle = math.degrees(
                math.acos(max(-1.0, min(1.0, (np.trace(step) - 1) / 2)))
            )
            zoom = max(focal, previous["focal"]) / min(focal, previous["focal"])
            continuous = angle <= MAX_STEP_DEGREES and zoom <= MAX_ZOOM_STEP
            if continuous:
                a = np.linalg.inv(centring(current["aspect"]))
                motion = (
                    a
                    @ np.diag([focal, focal, 1.0])
                    @ rotation
                    @ previous["rotation"].T
                    @ np.diag([1 / previous["focal"], 1 / previous["focal"], 1.0])
                    @ np.linalg.inv(a)
                )
        anchored = previous["anchored"] if chained and previous else timestamp
        self.previous = {
            "time": timestamp,
            "anchored": anchored,
            "features": current,
            "rotation": rotation,
            "focal": focal,
            "group": source["group"],
        }
        if (
            (chained or inliers < PROMOTE_INLIERS)
            and len(self.keyframes) < MAX_KEYFRAMES
            and timestamp - self.last_promotion >= PROMOTE_INTERVAL
        ):
            self.keyframes.append({**self.previous, "promoted": True})
            self.last_promotion = timestamp
        return floor, {
            "status": "automatic_camera",
            "estimated": True,
            "reference_time": group["reference_time"],
            "camera_group": source["group"],
            "keyframe_time": round(float(source["time"]), 3),
            "chained": chained,
            "inliers": int(inliers),
            "error_pixels": round(error, 3),
            "focal": round(focal, 4),
            "court_observations": group["observations"],
            "segments": [],
            "continuous": continuous,
            "camera_motion": motion,
        }
