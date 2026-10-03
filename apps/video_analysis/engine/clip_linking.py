"""Whole-shot player identities from short, unambiguous tracklets.

Frame-to-frame association is only trusted where it is unambiguous, which
leaves short tracklets that each show one person. Tracklets are then joined by
exclusive assignments that combine camera-compensated motion, shirt team and
an appearance space fitted to the clip itself: its tracklets are the classes,
so the space keeps what distinguishes these players (hair, shoes, numbers) and
drops what varies within one person (pose, blur, background).

Measured on labelled benchmark clips this removes most identity switches and
wrong-person joins of the frame-by-frame tracker; see
`scripts/python/research_data/korfbal_tracklet_linking.md`.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import suppress
import importlib
from operator import itemgetter
from typing import TYPE_CHECKING, Any, NamedTuple

from .clip_signals import modules


if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

VERSION = 1
MIN_CONFIDENCE = 0.3
DUPLICATE_IOU = 0.7
SAME_BODY_IOU = 0.5
MIN_IOU = 0.4
MIN_IOU_MARGIN = 0.15
# Tracklets shorter than this carry too little evidence to be linked; they
# only fill gaps inside an identity built from longer ones.
SOLID_SECONDS = 0.4
GAPS = (0.4, 1.5, 3.0)
LONG_GAP = 30.0
PASSES = 2
MAX_OVERLAP = 0.09
MIN_STEP = 0.04
VELOCITY_WINDOW = 0.4
VELOCITY_LEAD = 0.5
MIN_VELOCITY_SPAN = 0.15
MIN_VELOCITY_SAMPLES = 3
REACH = 0.35
REACH_PER_SECOND = 1.3
MAX_REACH_RATIO = 2.5
MIN_SIZE_RATIO = 0.6
MAX_SIZE_RATIO = 1.6
MIN_LONG_SIZE_RATIO = 0.4
MAX_LONG_SIZE_RATIO = 2.5
LONG_REACH = 0.5
LONG_SPEED = 4.0
MAX_LONG_APPEARANCE = 0.3
# Logistic link score fitted on labelled candidate pairs: a link is accepted
# when appearance and motion together are more likely the same person.
BIAS = 4.0
APPEARANCE_WEIGHT = 7.6
MOTION_WEIGHT = 1.15
LONG_PENALTY = 1.0
ONLINE_BONUS = 2.0
UNKNOWN_TEAM_PENALTY = 1.0
UNKNOWN_APPEARANCE = 0.45
MAX_BLIND_REACH = 0.5
MAX_BLIND_GAP = 0.3
DIMENSIONS = 32
SHRINKAGE = 0.3
MIN_CLASS_SAMPLES = 6
MIN_CLASSES = 4
TEAM_SHARE = 0.7
# Torso region of a player box and how clearly a shirt colour must lean to one team.
TORSO = (0.3, 0.7, 0.2, 0.45)
LIGHTNESS_WEIGHT = 0.3
MIN_COLOUR_SAMPLES = 8
MIN_COLOUR_CONFIDENCE = 0.5
MIN_COLOUR_TRACKLETS = 4
COLOUR_MARGIN = 0.5
MIN_COLOUR_GAP = 20.0
MIN_COLOUR_AGREEMENT = 0.8
MIN_COLOUR_ANCHORS = 2
COLOUR_ROUNDS = 20
# An identity the detector called a referee this often is the referee throughout.
MIN_REFEREE_ROWS = 5
MIN_REFEREE_SHARE = 0.3
MIN_TEAM_VOTES = 3
FILL_GAP = 3.0
FLOW_POINTS = 600
MIN_FLOW_POINTS = 12
GRAPHICS_BAND = 0.14
FILL_DISTANCE = 0.5
BODY_CENTRE = 0.45
EPSILON = 1e-9
UNREACHABLE = 1e6
TEAMS = ("team_a", "team_b")


class Row(NamedTuple):
    """One retained player observation in isotropic image units."""

    frame: int
    time: float
    box: Any
    confidence: float
    team: int | None
    track_id: str
    descriptor: int
    colour: Any = None
    referee: bool = False


def overlaps(first: NDArray[Any], second: NDArray[Any]) -> NDArray[Any]:
    """Return pairwise IoU for two sets of corner boxes."""
    _, np = modules()
    if not len(first) or not len(second):
        return np.zeros((len(first), len(second)))
    left = np.maximum(first[:, None, 0], second[None, :, 0])
    top = np.maximum(first[:, None, 1], second[None, :, 1])
    right = np.minimum(first[:, None, 2], second[None, :, 2])
    bottom = np.minimum(first[:, None, 3], second[None, :, 3])
    shared = np.clip(right - left, 0, None) * np.clip(bottom - top, 0, None)
    areas = (first[:, 2] - first[:, 0]) * (first[:, 3] - first[:, 1])
    others = (second[:, 2] - second[:, 0]) * (second[:, 3] - second[:, 1])
    return shared / np.maximum(areas[:, None] + others[None, :] - shared, EPSILON)


def moved(box: NDArray[Any], motion: NDArray[Any] | None) -> NDArray[Any]:
    """Carry a box into the next frame's coordinates."""
    if motion is None:
        return box
    _, np = modules()
    corners = np.array([[box[0], box[1], 1.0], [box[2], box[3], 1.0]]) @ motion.T
    scale = np.where(np.abs(corners[:, 2]) < EPSILON, 1.0, corners[:, 2])
    corners = corners[:, :2] / scale[:, None]
    return np.array([corners[0, 0], corners[0, 1], corners[1, 0], corners[1, 1]])


def assign(costs: NDArray[Any], limit: float) -> dict[int, int]:
    """Solve one exclusive assignment in which leaving a row unlinked costs `limit`."""
    _, np = modules()
    count = len(costs)
    if not count:
        return {}
    optimize = importlib.import_module("scipy.optimize")
    padded = np.hstack([
        np.where(np.isfinite(costs), costs, UNREACHABLE),
        np.full((count, count), limit),
    ])
    rows, columns = optimize.linear_sum_assignment(padded)
    return {
        int(row): int(column)
        for row, column in zip(rows, columns, strict=True)
        if column < costs.shape[1] and costs[row, column] <= limit
    }


def torso_colour(image: NDArray[Any], box: list[float]) -> NDArray[Any] | None:
    """Return the median Lab colour of a normalised box's torso region."""
    cv, np = modules()
    height, width = image.shape[:2]
    x, y, w, h = box
    left, right = int((x + TORSO[0] * w) * width), int((x + TORSO[1] * w) * width)
    top, bottom = int((y + TORSO[2] * h) * height), int((y + TORSO[3] * h) * height)
    patch = image[
        max(0, top) : max(top + 1, bottom), max(0, left) : max(left + 1, right)
    ]
    if not patch.size:
        return None
    lab = cv.cvtColor(np.ascontiguousarray(patch), cv.COLOR_BGR2LAB)
    return np.median(lab.reshape(-1, 3), axis=0)


def colour_sides(colours: NDArray[Any]) -> list[int | None]:
    """Split tracklet shirt colours into two groups; unclear ones stay undecided."""
    _, np = modules()
    values = colours * np.array([LIGHTNESS_WEIGHT, 1.0, 1.0])
    # Start from two colours far apart without comparing every pair: the one
    # furthest from the average, then the one furthest from that.
    first = int(np.linalg.norm(values - values.mean(axis=0), axis=1).argmax())
    second = int(np.linalg.norm(values - values[first], axis=1).argmax())
    centres = values[[first, second]].astype(float)
    for _ in range(COLOUR_ROUNDS):
        nearest = np.linalg.norm(values[:, None] - centres[None], axis=2).argmin(axis=1)
        centres = np.array([
            values[nearest == side].mean(axis=0)
            if (nearest == side).any()
            else centres[side]
            for side in (0, 1)
        ])
    reach = np.linalg.norm(values[:, None] - centres[None], axis=2)
    apart = float(np.linalg.norm(centres[0] - centres[1]))
    if apart < MIN_COLOUR_GAP:
        # One shirt colour in two shades is one team, not two.
        return [None] * len(values)
    margin = COLOUR_MARGIN * apart
    return [
        int(row.argmin()) if abs(row[0] - row[1]) > margin else None for row in reach
    ]


class Background:
    """Camera motion between consecutive frames from features outside player boxes.

    The court calibration's motion is tuned for the floor map; association
    needs the frame-to-frame image motion, which sparse flow measures directly.
    """

    def __init__(self) -> None:
        """Start without a previous frame."""
        self.previous: NDArray[Any] | None = None

    def update(
        self, image: NDArray[Any], boxes: list[list[float]], *, cut: bool = False
    ) -> NDArray[Any] | None:
        """Return previous-to-current motion in normalised image coordinates."""
        cv, np = modules()
        gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
        previous, self.previous = self.previous, gray
        if previous is None or cut or previous.shape != gray.shape:
            return None
        height, width = gray.shape
        mask = np.full(gray.shape, 255, np.uint8)
        for x, y, w, h in boxes:
            left, top = max(0, int(x * width)), max(0, int(y * height))
            mask[top : int((y + h) * height) + 1, left : int((x + w) * width) + 1] = 0
        # Broadcast graphics do not move with the camera.
        mask[int(height * (1 - GRAPHICS_BAND)) :] = 0
        points = cv.goodFeaturesToTrack(previous, FLOW_POINTS, 0.01, 8, mask=mask)
        if points is None or len(points) < MIN_FLOW_POINTS:
            return None
        window = {"winSize": (21, 21), "maxLevel": 3}
        forward, found, _ = cv.calcOpticalFlowPyrLK(
            previous, gray, points, None, **window
        )
        backward, returned, _ = cv.calcOpticalFlowPyrLK(
            gray, previous, forward, None, **window
        )
        drift = np.linalg.norm((backward - points).reshape(-1, 2), axis=1)
        keep = (found.ravel() == 1) & (returned.ravel() == 1) & (drift < 1.0)
        if keep.sum() < MIN_FLOW_POINTS:
            return None
        affine, _ = cv.estimateAffinePartial2D(
            points[keep], forward[keep], method=cv.RANSAC, ransacReprojThreshold=2.0
        )
        if affine is None:
            return None
        scale = np.diag([float(width), float(height), 1.0])
        return np.linalg.inv(scale) @ np.vstack([affine, [0.0, 0.0, 1.0]]) @ scale


def follows(before: dict, after: dict) -> bool:
    """Whether one identity can continue as another: later, and not a rival shirt."""
    if after["start"] <= before["start"] or after["end"] <= before["end"]:
        return False
    # Sharing exactly one frame is two people; a tracker hand-over overlaps less.
    if after["start"] == before["end"]:
        return False
    teams = {before["team"], after["team"]} - {None}
    return len(teams) <= 1


def displacement(before: dict, after: dict, gap: float) -> tuple[float, float]:
    """Return predicted and plain displacement in body heights."""
    _, np = modules()
    height = (before["last_height"] + after["first_height"]) / 2
    lead = min(gap, VELOCITY_LEAD)
    forward = np.linalg.norm(
        before["last"] + before["last_velocity"] * lead - after["first"]
    )
    backward = np.linalg.norm(
        after["first"] - after["first_velocity"] * lead - before["last"]
    )
    plain = float(np.linalg.norm(before["last"] - after["first"]))
    predicted = min(plain, float(forward + backward) / 2)
    return predicted / max(height, EPSILON), plain / max(height, EPSILON)


def link_cost(
    before: dict,
    after: dict,
    appearance: float | None,
    *,
    long: bool,
) -> float:
    """Score one candidate link; below zero means more likely than not."""
    gap = max(after["start"] - before["end"], MIN_STEP)
    predicted, plain = displacement(before, after, gap)
    ratio = before["last_height"] / max(after["first_height"], EPSILON)
    # Without a team on both sides nothing rules out an opponent.
    unknown = (
        UNKNOWN_TEAM_PENALTY if before["team"] is None or after["team"] is None else 0.0
    )
    if long:
        if (
            appearance is None
            or appearance > MAX_LONG_APPEARANCE
            or plain > LONG_REACH + LONG_SPEED * gap
            or not MIN_LONG_SIZE_RATIO < ratio < MAX_LONG_SIZE_RATIO
        ):
            return float("inf")
        return APPEARANCE_WEIGHT * appearance - BIAS + LONG_PENALTY + unknown
    # The online tracker followed one box across this gap: weak evidence
    # alone, but worth a share when appearance does not contradict it.
    online = ONLINE_BONUS if before["last_id"] == after["first_id"] else 0.0
    reach = predicted / (REACH + REACH_PER_SECOND * gap)
    if reach > MAX_REACH_RATIO or not MIN_SIZE_RATIO < ratio < MAX_SIZE_RATIO:
        return float("inf")
    if appearance is None:
        if reach > MAX_BLIND_REACH or gap > MAX_BLIND_GAP:
            return float("inf")
        appearance = UNKNOWN_APPEARANCE
    return (
        APPEARANCE_WEIGHT * appearance + MOTION_WEIGHT * reach - BIAS - online + unknown
    )


def seconded(rows: list[Row]) -> set[int]:
    """Find player boxes that a referee box of the same frame lies on.

    The detector can report one body under both classes at once. Whichever box
    ends up in an identity, the referee detection is evidence about that body.
    """
    _, np = modules()
    frames: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        frames[row.frame].append(index)
    found: set[int] = set()
    for members in frames.values():
        referees = [i for i in members if rows[i].referee]
        players = [i for i in members if not rows[i].referee]
        if not referees or not players:
            continue
        scores = overlaps(
            np.array([rows[i].box for i in players]),
            np.array([rows[i].box for i in referees]),
        )
        # Only a box on the very same body counts: a player standing partly
        # in front of the referee is someone else.
        found.update(
            index
            for position, index in enumerate(players)
            if scores[position].max() >= DUPLICATE_IOU
        )
    return found


def official(rows: list[Row], chain: list[int], doubled: set[int]) -> bool:
    """Whether an identity is the referee, from every frame it was seen in."""
    votes = sum(rows[index].referee or index in doubled for index in chain)
    return votes >= MIN_REFEREE_ROWS and votes >= MIN_REFEREE_SHARE * len(chain)


def shadows(rows: list[Row], owner: dict[int, int], officials: set[int]) -> dict:
    """Find loose player boxes that lie on a referee identity's own box.

    The detector can report one body as a referee and as a player in the same
    frame. The referee box is linked; its player copy would otherwise stay a
    team player on the court map.
    """
    _, np = modules()
    frames: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        frames[row.frame].append(index)
    found: dict[int, int] = {}
    for members in frames.values():
        linked = [i for i in members if owner.get(i) in officials]
        loose = [i for i in members if i not in owner and not rows[i].referee]
        if not linked or not loose:
            continue
        scores = overlaps(
            np.array([rows[i].box for i in loose]),
            np.array([rows[i].box for i in linked]),
        )
        for position, index in enumerate(loose):
            best = int(scores[position].argmax())
            if scores[position, best] >= DUPLICATE_IOU:
                found[index] = owner[linked[best]]
    return found


class Linker:
    """Collect one clip's player observations and resolve them into identities."""

    def __init__(self) -> None:
        """Start without frames; descriptors are optional per observation."""
        _, self.np = modules()
        self.rows: list[Row] = []
        self.segments: list[int] = []
        self.motions: list[NDArray[Any] | None] = []
        self.descriptors: list[NDArray[Any]] = []
        self.to_first: list[NDArray[Any]] = []
        self.sides: dict[int, int] = {}

    def observe(
        self,
        players: list[dict],
        time: float,
        camera: dict,
        *,
        aspect: float,
        descriptors: dict[int, NDArray[Any]] | None = None,
    ) -> None:
        """Retain one frame's observed player boxes, teams and appearance."""
        np = self.np
        frame = len(self.segments)
        self.segments.append(int(camera.get("segment", 0)))
        scale = np.diag([aspect, 1.0, 1.0])
        motion = camera.get("motion")
        self.motions.append(
            None
            if motion is None or camera.get("cut")
            else scale @ np.asarray(motion, dtype=float) @ np.linalg.inv(scale)
        )
        for index, player in enumerate(players):
            x, y, w, h = player.get("observed_bbox") or player["bbox"]
            if w <= 0 or h <= 0:
                continue
            descriptor = -1
            if descriptors and index in descriptors:
                descriptor = len(self.descriptors)
                self.descriptors.append(
                    np.asarray(descriptors[index], dtype=np.float16)
                )
            # Only a shirt seen in this frame counts; a team inherited through
            # the online identity would repeat that identity's mistakes.
            referee = player.get("label") == "referee"
            seen = player.get("team_source", "shirt") == "shirt" and not referee
            team = player.get("team") if seen else None
            self.rows.append(
                Row(
                    frame,
                    time,
                    np.array([x * aspect, y, (x + w) * aspect, y + h]),
                    float(player.get("confidence", 1.0)),
                    TEAMS.index(team) if team in TEAMS else None,
                    player["track_id"],
                    descriptor,
                    player.get("shirt_colour"),
                    referee,
                )
            )

    def tracklets(self) -> list[list[int]]:
        """Follow boxes only while exactly one continuation is plausible."""
        np = self.np
        frames: dict[int, list[int]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            if row.confidence >= MIN_CONFIDENCE:
                frames[row.frame].append(index)
        finished: list[list[int]] = []
        active: list[list[int]] = []
        previous_segment: int | None = None
        for frame, segment in enumerate(self.segments):
            if segment != previous_segment:
                finished.extend(active)
                active, previous_segment = [], segment
            indices = self.distinct(frames.get(frame, []))
            boxes = np.array([self.rows[i].box for i in indices]).reshape(-1, 4)
            scores = overlaps(
                np.array([self.predicted(track, frame) for track in active]).reshape(
                    -1, 4
                ),
                boxes,
            )
            continued: list[list[int]] = []
            used: set[int] = set()
            for row, column in assign(-scores, -MIN_IOU).items():
                best = scores[row, column]
                rivals = max(
                    np.delete(scores[row], column).max(initial=0.0),
                    np.delete(scores[:, column], row).max(initial=0.0),
                )
                if best - rivals < MIN_IOU_MARGIN:
                    continue
                active[row].append(indices[column])
                continued.append(active[row])
                used.add(column)
            kept = {id(track) for track in continued}
            finished.extend(track for track in active if id(track) not in kept)
            continued.extend(
                [index] for column, index in enumerate(indices) if column not in used
            )
            active = continued
        finished.extend(active)
        return finished

    def distinct(self, indices: list[int]) -> list[int]:
        """Drop the weaker of two boxes that describe the same body."""
        np = self.np
        ordered = sorted(indices, key=lambda i: -self.rows[i].confidence)
        if not ordered:
            return []
        scores = overlaps(*[np.array([self.rows[i].box for i in ordered])] * 2)
        kept: list[int] = []
        for position in range(len(ordered)):
            if all(scores[position, other] < DUPLICATE_IOU for other in kept):
                kept.append(position)
        return [ordered[position] for position in kept]

    def predicted(self, track: list[int], frame: int) -> NDArray[Any]:
        """Extrapolate the last box by its camera-compensated displacement."""
        last = self.rows[track[-1]]
        box = moved(last.box, self.motions[frame])
        if len(track) > 1 and last.frame == frame - 1:
            before = self.rows[track[-2]]
            if before.frame == last.frame - 1:
                shift = last.box - moved(before.box, self.motions[last.frame])
                centre = [(shift[0] + shift[2]) / 2, (shift[1] + shift[3]) / 2]
                box += self.np.array(centre * 2)
        return box

    def stabilise(self) -> None:
        """Map every frame into the coordinates of its shot's first frame."""
        np = self.np
        self.to_first = []
        current, previous = np.eye(3), None
        for segment, motion in zip(self.segments, self.motions, strict=True):
            if segment != previous:
                current = np.eye(3)
            elif motion is not None:
                # An unusable motion keeps the last stable mapping.
                with suppress(np.linalg.LinAlgError):
                    current @= np.linalg.inv(motion)
            self.to_first.append(current.copy())
            previous = segment

    def placed(self, index: int) -> tuple[NDArray[Any], float]:
        """Return a body point and body height in the shot's stable coordinates."""
        np = self.np
        row = self.rows[index]
        matrix = self.to_first[row.frame]
        x1, y1, x2, y2 = row.box
        point = matrix @ np.array([
            (x1 + x2) / 2,
            y1 + (y2 - y1) * BODY_CENTRE,
            1.0,
        ])
        point = point[:2] / (point[2] if abs(point[2]) > EPSILON else 1.0)
        scale = float(np.sqrt(abs(np.linalg.det(matrix[:2, :2]))))
        return point, (y2 - y1) * scale

    def describe(self, chain: list[int]) -> dict:
        """Summarise both ends of an identity for motion comparisons."""
        np = self.np
        times = np.array([self.rows[i].time for i in chain])
        placed = [self.placed(i) for i in chain]
        points = np.array([point for point, _ in placed])
        heights = np.array([height for _, height in placed])

        def velocity(selected: NDArray[Any]) -> NDArray[Any]:
            span = times[selected[-1]] - times[selected[0]]
            if len(selected) < MIN_VELOCITY_SAMPLES or span < MIN_VELOCITY_SPAN:
                return np.zeros(2)
            design = np.column_stack([
                np.ones(len(selected)),
                times[selected] - times[selected[0]],
            ])
            return np.linalg.lstsq(design, points[selected], rcond=None)[0][1]

        first = np.flatnonzero(times - times[0] <= VELOCITY_WINDOW)
        last = np.flatnonzero(times[-1] - times <= VELOCITY_WINDOW)
        teams = Counter(
            self.sides.get(i, self.rows[i].team)
            for i in chain
            if self.sides.get(i, self.rows[i].team) is not None
        )
        team = None
        if teams:
            side, votes = teams.most_common(1)[0]
            if votes >= MIN_TEAM_VOTES and votes >= TEAM_SHARE * sum(teams.values()):
                team = side
        return {
            "start": float(times[0]),
            "end": float(times[-1]),
            "first": points[0],
            "last": points[-1],
            "first_height": float(np.median(heights[first])),
            "last_height": float(np.median(heights[last])),
            "first_velocity": velocity(first),
            "last_velocity": velocity(last),
            "team": team,
            "first_id": self.rows[chain[0]].track_id,
            "last_id": self.rows[chain[-1]].track_id,
        }

    def space(self) -> tuple[NDArray[Any], NDArray[Any]] | None:
        """Gather the sampled descriptors and the observations they describe."""
        np = self.np
        owners = np.array([
            index for index, row in enumerate(self.rows) if row.descriptor >= 0
        ])
        if len(owners) < MIN_CLASS_SAMPLES * MIN_CLASSES:
            return None
        values = np.stack([
            self.descriptors[self.rows[i].descriptor] for i in owners
        ]).astype(np.float64)
        return owners, values - values.mean(axis=0)

    def appearance(
        self,
        chains: list[list[int]],
        space: tuple[NDArray[Any], NDArray[Any]] | None,
    ) -> dict[int, NDArray[Any]]:
        """Fit the identity space on the current chains and place each in it."""
        np = self.np
        if space is None:
            return {}
        owners, values = space
        owner_chain = np.full(len(self.rows), -1)
        for number, chain in enumerate(chains):
            owner_chain[chain] = number
        labels = owner_chain[owners]
        classes = [
            label
            for label, count in Counter(labels[labels >= 0].tolist()).items()
            if count >= MIN_CLASS_SAMPLES
        ]
        if len(classes) < MIN_CLASSES:
            return {}
        within = np.zeros((values.shape[1],) * 2)
        centres = []
        for label in classes:
            members = values[labels == label]
            centre = members.mean(axis=0)
            centres.append(centre)
            within += (members - centre).T @ (members - centre)
        within /= sum(int((labels == label).sum()) for label in classes)
        within = (1 - SHRINKAGE) * within + SHRINKAGE * np.trace(within) / len(
            within
        ) * np.eye(len(within))
        weights, vectors = np.linalg.eigh(within)
        whiten = vectors @ np.diag(np.maximum(weights, EPSILON) ** -0.5) @ vectors.T
        between = np.array(centres) @ whiten
        _, _, axes = np.linalg.svd(between - between.mean(axis=0), full_matrices=False)
        projected = values @ whiten @ axes[:DIMENSIONS].T
        projected /= np.maximum(
            np.linalg.norm(projected, axis=1, keepdims=True), EPSILON
        )
        means = {}
        for label in set(labels[labels >= 0].tolist()):
            mean = projected[labels == label].mean(axis=0)
            means[label] = mean / max(float(np.linalg.norm(mean)), EPSILON)
        return means

    def link(
        self,
        chains: list[list[int]],
        space: tuple[NDArray[Any], NDArray[Any]] | None,
        gap: float,
        *,
        long: bool,
    ) -> list[list[int]]:
        """Join chains by one exclusive assignment per camera shot."""
        np = self.np
        chains = sorted(chains, key=lambda chain: self.rows[chain[0]].time)
        means = self.appearance(chains, space)
        described = [self.describe(chain) for chain in chains]
        by_segment: dict[int, list[int]] = defaultdict(list)
        for number, chain in enumerate(chains):
            by_segment[self.segments[self.rows[chain[0]].frame]].append(number)
        following: dict[int, int] = {}
        for members in by_segment.values():
            starts = [described[number]["start"] for number in members]
            costs = np.full((len(members), len(members)), np.inf)
            for row, number in enumerate(members):
                before = described[number]
                low = bisect_left(starts, before["end"] - MAX_OVERLAP)
                high = bisect_right(starts, before["end"] + gap)
                for column in range(low, high):
                    other = members[column]
                    after = described[other]
                    if not follows(before, after):
                        continue
                    distance = (
                        1 - float(means[number] @ means[other])
                        if number in means and other in means
                        else None
                    )
                    costs[row, column] = link_cost(before, after, distance, long=long)
            following.update({
                members[row]: members[column]
                for row, column in assign(costs, 0.0).items()
            })
        followers = set(following.values())
        merged = []
        for first, chain in enumerate(chains):
            if first in followers:
                continue
            combined, number = list(chain), first
            while number in following:
                number = following[number]
                combined.extend(chains[number])
            merged.append(sorted(set(combined), key=self.order))
        return merged

    def order(self, index: int) -> tuple[int, float]:
        """Order observations by frame, strongest first within a frame."""
        return self.rows[index].frame, -self.rows[index].confidence

    def single(self, chain: list[int]) -> list[int]:
        """Keep one observation per frame after tracklets overlapped by a frame."""
        kept: list[int] = []
        for index in sorted(chain, key=self.order):
            if not kept or self.rows[kept[-1]].frame != self.rows[index].frame:
                kept.append(index)
        return kept

    def gap(
        self, frames: list[int], members: list[int], index: int, placed: dict
    ) -> tuple[int, NDArray[Any], float] | None:
        """Where an identity misses this observation's frame, and what it expects."""
        row = self.rows[index]
        position = bisect_left(frames, row.frame)
        if position in {0, len(frames)} or frames[position] == row.frame:
            return None
        before, after = members[position - 1], members[position]
        start, end = self.rows[before].time, self.rows[after].time
        if end - start > FILL_GAP:
            return None
        weight = (row.time - start) / max(end - start, EPSILON)
        expected = placed[before][0] * (1 - weight) + placed[after][0] * weight
        return position, expected, (placed[before][1] + placed[after][1]) / 2

    def fill(self, chains: list[list[int]]) -> list[list[int]]:
        """Give loose observations to the identity whose gap they fit, per frame."""
        np = self.np
        owned = {index for chain in chains for index in chain}
        loose: dict[int, list[int]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            if index not in owned:
                loose[row.frame].append(index)
        placed = {index: self.placed(index) for index in owned}
        members = [list(chain) for chain in chains]
        frames = [[self.rows[index].frame for index in chain] for chain in chains]
        segments = [self.segments[self.rows[chain[0]].frame] for chain in chains]
        for frame in sorted(loose):
            found = [self.placed(index) for index in loose[frame]]
            gaps = [
                (number, gap)
                for number in range(len(chains))
                if segments[number] == self.segments[frame]
                and (
                    gap := self.gap(
                        frames[number], members[number], loose[frame][0], placed
                    )
                )
            ]
            costs = np.full((len(found), len(gaps)), np.inf)
            for row, (point, height) in enumerate(found):
                for column, (_, (_, expected, size)) in enumerate(gaps):
                    if MIN_SIZE_RATIO < height / max(size, EPSILON) < MAX_SIZE_RATIO:
                        distance = float(np.linalg.norm(point - expected))
                        costs[row, column] = distance / max(size, EPSILON)
            for row, column in assign(costs, FILL_DISTANCE).items():
                number, (position, _, _) = gaps[column]
                index = loose[frame][row]
                frames[number].insert(position, frame)
                members[number].insert(position, index)
                placed[index] = found[row]
        return members

    def shirt_sides(self, tracklets: list[list[int]]) -> dict[int, int]:
        """Give tracklets without a seen shirt team the side their colour shows.

        Tracklets show one person, so their median torso colour is steadier
        than a frame's. The two colour groups are named by the shirt teams
        already seen; without enough agreement the colours are not used.
        """
        np = self.np
        described = []
        for track in tracklets:
            # A referee's kit is not a team colour, whatever it resembles.
            colours = [
                self.rows[i].colour
                for i in track
                if self.rows[i].colour is not None
                and not self.rows[i].referee
                and self.rows[i].confidence >= MIN_COLOUR_CONFIDENCE
            ]
            if len(colours) >= MIN_COLOUR_SAMPLES:
                described.append((track, np.median(np.array(colours), axis=0)))
        if len(described) < MIN_COLOUR_TRACKLETS:
            return {}
        groups = colour_sides(np.array([colour for _, colour in described]))
        seen: Counter[tuple[int, int]] = Counter()
        for (track, _), group in zip(described, groups, strict=True):
            votes = Counter(
                team for i in track if (team := self.rows[i].team) is not None
            )
            if group is not None and votes:
                seen[group, votes.most_common(1)[0][0]] += 1
        straight, crossed = seen[0, 0] + seen[1, 1], seen[0, 1] + seen[1, 0]
        # Without seen shirt teams the two groups have no names: calling them
        # team A and B could contradict a team seen on a short tracklet.
        if straight + crossed < MIN_COLOUR_ANCHORS or max(
            straight, crossed
        ) < MIN_COLOUR_AGREEMENT * (straight + crossed):
            return {}
        flip = crossed > straight
        sides: dict[int, int] = {}
        for (track, _), group in zip(described, groups, strict=True):
            if group is None:
                continue
            # A team seen on this tracklet stands; colour only fills
            # tracklets on which no shirt team was seen often enough.
            seen_here = sum(self.rows[i].team is not None for i in track)
            if seen_here >= MIN_TEAM_VOTES:
                continue
            side = 1 - group if flip else group
            sides.update({
                i: side
                for i in track
                if self.rows[i].team is None and not self.rows[i].referee
            })
        return sides

    def identities(
        self, stopped: Callable[[], bool] | None = None
    ) -> list[list[int]] | None:
        """Resolve observations into identities, or stop at the job deadline."""
        self.stabilise()
        tracklets = self.tracklets()
        self.sides = self.shirt_sides(tracklets)

        def solid(track: list[int]) -> bool:
            span = self.rows[track[-1]].time - self.rows[track[0]].time
            return span >= SOLID_SECONDS

        chains = [track for track in tracklets if solid(track)]
        space = self.space()
        for _ in range(PASSES):
            for gap in GAPS:
                if stopped and stopped():
                    return None
                chains = self.link(chains, space, gap, long=False)
            if stopped and stopped():
                return None
            chains = self.link(chains, space, LONG_GAP, long=True)
        return self.fill([self.single(chain) for chain in chains])

    def duplicates(self, owner: dict[int, int], main: dict[str, int]) -> dict[int, str]:
        """Find loose second boxes of a linked body, keyed by the linked observation.

        The replay drops such a box only when its online track belongs to the
        same identity, so other loose boxes stay visible as their own fragment.
        """
        np = self.np
        frames: dict[int, list[int]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            frames[row.frame].append(index)
        found: dict[int, str] = {}
        for members in frames.values():
            linked = [index for index in members if index in owner]
            loose = [index for index in members if index not in owner]
            if not linked or not loose:
                continue
            scores = overlaps(
                np.array([self.rows[index].box for index in loose]),
                np.array([self.rows[index].box for index in linked]),
            )
            for row, index in enumerate(loose):
                best = int(scores[row].argmax())
                kept = linked[best]
                if (
                    scores[row, best] >= SAME_BODY_IOU
                    and kept not in found
                    and main.get(self.rows[index].track_id) == owner[kept]
                ):
                    found[kept] = self.rows[index].track_id
        return found

    def frame_links(
        self,
        owner: dict[int, int],
        main: dict[str, int],
        names: list[str],
        teams: list[int | None],
        officials: set[int],
    ) -> list[dict[str, Any]]:
        """Rename the observations an online track's main alias does not cover."""
        duplicates = self.duplicates(owner, main)
        copies = shadows(self.rows, owner, officials)
        frame_links = []
        for index, row in sorted(
            enumerate(self.rows), key=lambda item: itemgetter(0, 5)(item[1])
        ):
            if row.referee:
                continue  # Already a referee in the frame; nothing to correct.
            number = owner.get(index, copies.get(index))
            if number in officials:
                # Seen as a player here, but the same person is the referee
                # elsewhere in the shot, before or after this frame.
                # A loose copy shares its frame with the identity's own box,
                # so it needs a name of its own.
                target = names[number]
                if index in copies:
                    target = f"{target}~{row.track_id}"
                frame_links.append({
                    "time_seconds": round(row.time, 6),
                    "from_track_id": row.track_id,
                    "to_track_id": target,
                    "display_id": None,
                    "label": "referee",
                })
                continue
            if number == main.get(row.track_id) and index not in duplicates:
                continue
            link: dict[str, Any] = {
                "time_seconds": round(row.time, 6),
                "from_track_id": row.track_id,
                "to_track_id": f"{row.track_id}~unlinked",
                "display_id": None,
            }
            if number is not None:
                link.update(to_track_id=names[number], display_id=number + 1)
                team = teams[number]
                if team is not None:
                    link["team"] = TEAMS[team]
            if index in duplicates:
                link["superseded_track_id"] = duplicates[index]
            frame_links.append(link)
        return frame_links

    def finish(self, stopped: Callable[[], bool] | None = None) -> dict | None:
        """Describe the identities as replay links over the retained observations."""
        if not self.rows:
            return None
        chains = self.identities(stopped)
        if chains is None:
            return None
        chains.sort(key=lambda chain: self.rows[chain[0]].time)
        owner: dict[int, int] = {}
        for number, chain in enumerate(chains):
            owner.update(dict.fromkeys(chain, number))
        names = [
            f"s{self.segments[self.rows[chain[0]].frame]}-linked-{number + 1}"
            for number, chain in enumerate(chains)
        ]
        teams = [self.describe(chain)["team"] for chain in chains]
        doubled = seconded(self.rows)
        officials = {
            number
            for number, chain in enumerate(chains)
            if official(self.rows, chain, doubled)
        }
        targets: dict[str, Counter[int]] = defaultdict(Counter)
        for index, row in enumerate(self.rows):
            if index in owner and not row.referee and owner[index] not in officials:
                targets[row.track_id][owner[index]] += 1
        # One alias covers an online track's main identity; only the
        # observations that belong elsewhere need their own entry.
        main = {
            track_id: found.most_common(1)[0][0] for track_id, found in targets.items()
        }
        links = [
            {
                "from_track_id": track_id,
                "to_track_id": names[number],
                "display_id": number + 1,
                "source": "tracklet_linking",
            }
            for track_id, number in sorted(main.items())
        ]
        frame_links = self.frame_links(owner, main, names, teams, officials)
        return {
            "version": 1,
            "status": "completed",
            "appearance": "learned_descriptors" if self.descriptors else "unavailable",
            "linking": {
                "version": VERSION,
                "identities": len(chains) - len(officials),
                "referee_identities": len(officials),
                "referee_corrections": sum(
                    link.get("label") == "referee" for link in frame_links
                ),
                "observations": len(self.rows),
                "linked_observations": len(owner),
            },
            "tracks": len({row.track_id for row in self.rows}),
            "truncated": False,
            "rejected": {},
            "links": links,
            "frame_links": frame_links,
            "review_only": True,
        }
