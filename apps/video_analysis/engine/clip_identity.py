"""Recover player identities after brief occlusions interrupt native tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .clip_signals import Teams, center, distance, modules, transform


if TYPE_CHECKING:
    from numpy.typing import NDArray

IDENTITY_GAP = 3.0
MAX_SHIRT_DISTANCE = 25
MIN_SIZE_RATIO = 0.65
MAX_SIZE_RATIO = 1.55
MIN_RECOVERY_MARGIN = 0.3
MAX_RECOVERY_COST = 1.0


class IdentityMemory:
    """Keep a public identity through a uniquely supported short tracker restart.

    Shirt colour is only a rejection signal: nearby teammates must still have a
    separated spatial match. This is not biometric or jersey-number recognition.
    """

    def __init__(self) -> None:
        """Create isolated history for one camera segment."""
        self.tracks: dict[str, dict] = {}
        self.next_id = 0
        self.shirts = Teams()
        _, self.np = modules()

    def reset(self) -> None:
        """Never transfer identities across a camera cut."""
        self.tracks.clear()
        self.next_id = 0

    def advance(self, timestamp: float, motion: NDArray[Any] | None) -> None:
        """Carry the last observed position through every intermediate camera warp."""
        self.tracks = {
            k: v
            for k, v in self.tracks.items()
            if timestamp - v["time"] <= IDENTITY_GAP
        }
        if motion is not None:
            for prior in self.tracks.values():
                prior["point"] = transform(prior["point"], motion) or prior["point"]

    def cost(
        self, prior: dict, obj: dict, color: NDArray[Any] | None, time: float
    ) -> float:
        """Require compatible size, shirt and local motion before reconnecting."""
        if prior["label"] != obj["label"] or color is None or prior["color"] is None:
            return float("inf")
        ratio = obj["observed_bbox"][3] / max(1e-6, prior["height"])
        shirt = float(self.np.linalg.norm(prior["color"] - color))
        if not MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO or shirt > MAX_SHIRT_DISTANCE:
            return float("inf")
        dt = time - prior["time"]
        expected = [
            p + v * min(dt, 0.5)
            for p, v in zip(prior["point"], prior["velocity"], strict=True)
        ]
        radius = max(0.025, min(0.1, prior["height"] * 0.45))
        return distance(expected, center(obj["observed_bbox"])) / radius + shirt / 100

    def recover(
        self, objects: list[dict], colors: list, timestamp: float
    ) -> dict[int, str]:
        """Use mutual best matches with a margin, reserving all native continuations."""
        native = {v["native"]: k for k, v in self.tracks.items()}
        matched = {
            i: native[o["track_id"]]
            for i, o in enumerate(objects)
            if o["track_id"] in native
        }
        available = [k for k in self.tracks if k not in matched.values()]
        missing = [
            i for i, o in enumerate(objects) if i not in matched and not o.get("issue")
        ]
        if not available or not missing:
            return matched
        costs = self.np.array([
            [
                self.cost(self.tracks[k], objects[i], colors[i], timestamp)
                for i in missing
            ]
            for k in available
        ])
        for row, identity in enumerate(available):
            column = int(self.np.argmin(costs[row]))
            best = costs[row, column]
            others = [
                *self.np.delete(costs[row], column),
                *self.np.delete(costs[:, column], row),
            ]
            if best < MAX_RECOVERY_COST and all(
                v - best >= MIN_RECOVERY_MARGIN for v in others
            ):
                matched[missing[column]] = identity
        return matched

    def update(
        self,
        objects: list[dict],
        image: NDArray[Any],
        timestamp: float,
        motion: NDArray[Any] | None,
    ) -> list[dict]:
        """Reconcile observed people only; never draw a person while they are hidden."""
        self.advance(timestamp, motion)
        colors = [self.shirts.observe(image, o["observed_bbox"]) for o in objects]
        matched = self.recover(objects, colors, timestamp)
        for index, obj in enumerate(objects):
            native = obj["track_id"]
            identity = matched.get(index)
            prior = self.tracks.get(identity) if identity is not None else None
            if identity is None:
                self.next_id += 1
                identity = (
                    native if native not in self.tracks else f"{native}~{self.next_id}"
                )
            display_id = prior["display_id"] if prior else self.next_id
            point = center(obj["observed_bbox"])
            velocity = [0.0, 0.0]
            if prior and timestamp > prior["time"]:
                velocity = [
                    (a - b) / (timestamp - prior["time"])
                    for a, b in zip(point, prior["point"], strict=True)
                ]
            obj.update(
                track_id=identity,
                display_id=display_id,
                identity_source="reconnected" if identity != native else "tracker",
            )
            self.tracks[identity] = {
                "native": native,
                "display_id": display_id,
                "label": obj["label"],
                "point": point,
                "height": obj["observed_bbox"][3],
                "color": colors[index]
                if colors[index] is not None
                else (prior["color"] if prior else None),
                "velocity": velocity,
                "time": timestamp,
            }
        return objects
