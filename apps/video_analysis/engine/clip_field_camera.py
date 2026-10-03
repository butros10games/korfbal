"""Court positions from a tripod camera at eye level beside an outdoor field.

Such recordings show no hall and few floor markings, so the hall solver finds
nothing. What they do show is enough for a coarse ground map:

- A turning camera stretches the picture towards the side it turns to; how
  much gives the focal length.
- Standing players are about as tall as each other, so where their heads and
  feet appear gives the camera's pitch and its height in body lengths.
- A korf is 3.5 m high and the two posts stand a known distance apart, so the
  turn between them gives the camera's height in metres and its position.
  How tall the players are follows from that; it is not assumed.

The camera is a pinhole on a tripod: it turns about the vertical and tilts
about its own horizontal axis, with a fixed focal length. A tripod that stands
slightly crooked rolls the picture; that roll is measured from bodies across
the picture's width and undone. Footage that breaks those assumptions (a cut,
a zoom, motion that is not the camera's, a steep or high camera) is left
unmapped rather than mapped wrongly, and so is a camera whose place hangs on
the last few percent of the focal length or on which of several korfs is this
field's.
Positions are published as estimates, never as measurements. Lengths inside
this module are in image heights.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
import math
import operator
from typing import TYPE_CHECKING, Any

from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

POST_HEIGHT = 3.5
# Average standing height of a team, from youth to tall adult teams.
MIN_PERSON_HEIGHT, MAX_PERSON_HEIGHT = 1.35, 2.0
POST_FROM_END = 1 / 6
TRACK_WIDTH = 640
MIN_FLOW_POINTS = 12
# The camera's own motion moves most of the picture, across its width. Parts
# of the picture count equally, however much detail each one shows.
MIN_SUPPORT = 0.5
MIN_SPREAD = 0.4
FLOW_GRID = (16, 8)
CELL_POINTS = 3
MIN_CELL_POINTS = 2
MIN_CELL_CONTRAST = 2.0
# A blurred frame in a fast pan may fail; a few are bridged from the last good one.
MAX_FLOW_MISSES = 3
# A turn enlarges the picture slightly at its centre; this focal length is
# only used to tell that from a zoom.
TYPICAL_FOCAL = 1.6
MAX_ZOOM = 0.05
ZOOM_FRAMES = 12
GRAPHICS_BAND = 0.14
MIN_CONFIDENCE = 0.5
MIN_POST_CONFIDENCE = 0.25
MAX_BODY_ASPECT = 0.6
MAX_FOOT = 0.98
VIEW_SPAN = 0.2
MIN_VIEW_SAMPLES = 4
MIN_VIEW_BODIES = 30
MIN_VIEW_POSTS = 3
POST_BIN = 0.03
MAX_POST_CHOICES = 3
FIT_ROUNDS = 4
SHORT_WEIGHT = 0.6
MIN_FOCAL, MAX_FOCAL = 0.4, 5.0
# The focal length shows once the picture has turned this far, and is trusted
# after a few such turns; it is never known better than a few percent.
FOCAL_TURN = 0.25
MIN_FOCALS = 3
FOCAL_DOUBT = 0.04
# Eye level: a tripod on the ground, not a stand or a hall gallery.
MIN_CAMERA_HEIGHT, MAX_CAMERA_HEIGHT = 0.7, 2.3
MAX_PITCH = math.radians(12)
MAX_ROLL = math.radians(5)
MIN_ROLL_SPREAD = 0.5
# Pitch and body size soak up part of a roll, so it is found in steps; a
# roll that is still not settled after these is not known.
ROLL_ROUNDS = 8
ROLL_PROBE = math.radians(0.5)
MIN_ROLL_SHARE = 0.1
SAG_ROUNDS = 3
ROLL_STEP = math.radians(0.02)
# The camera must stay put within the focal length's doubt, and models this
# close together are the same camera.
MAX_CAMERA_DOUBT = 3.0
SAME_CAMERA = 2.0
PITCH_STEP = math.radians(0.5)
FINE_PITCH_STEP = math.radians(0.05)
COURT_MARGIN = 3.0
MIN_ON_COURT = 0.6
MIN_LEAD = 0.1
MAX_TIME_GAP = 0.5
CENTRE_ROW = 0.5
EPSILON = 1e-9


def tangents(
    rows: NDArray[Any], focal: float, down: NDArray[Any] | float
) -> NDArray[Any]:
    """Return how steeply each image row looks below the horizontal."""
    _, np = modules()
    return np.tan(np.arctan((rows - CENTRE_ROW) / focal) + down)


def ray(
    point: tuple[float, float], focal: float, down: float, centre: float
) -> tuple[float, float, float]:
    """Return a pixel's viewing direction as right, down and forward parts.

    The parts are in a level frame that turns with the camera's pan, so a
    pitched camera is undone here once and for all.
    """
    across, below = point[0] - centre, point[1] - CENTRE_ROW
    return (
        across,
        math.cos(down) * below + math.sin(down) * focal,
        -math.sin(down) * below + math.cos(down) * focal,
    )


def upright(
    bodies: NDArray[Any], focal: float, slope: float | None = None
) -> tuple[float, float, float]:
    """Fit the pitch at which standing bodies are equally tall.

    `bodies` holds foot row, head row and the tilt already turned since the
    view's reference, per body. For a person standing on the ground, the
    steepness of the ray to the feet and of the ray to the head differ by the
    same share of the feet's steepness: body height over camera height. Returns
    the reference pitch, that share and the remaining spread.
    """
    _, np = modules()
    feet, heads, turned = bodies[:, 0], bodies[:, 1], bodies[:, 2]

    def spread(down: float) -> tuple[float, float]:
        low = tangents(feet, focal, down - turned / focal)
        high = tangents(heads, focal, down - turned / focal)
        usable = low > EPSILON
        if usable.sum() < MIN_VIEW_BODIES // 2:
            return float("inf"), 0.0
        low, tall = low[usable], (low - high)[usable]
        share = slope
        if share is None:
            weights = np.ones(len(low))
            share = 1.0
            for _ in range(FIT_ROUNDS):
                share = float(
                    (weights * tall * low).sum() / (weights * low * low).sum()
                )
                rest = tall - share * low
                scale = float(np.median(np.abs(rest))) * 1.48 + EPSILON
                # Crouching, jumping and cut-off bodies are shorter than a
                # standing one, so shorter-than-expected samples count less.
                weights = np.where(rest > 0, 1.0, SHORT_WEIGHT) / (
                    1 + (rest / (2 * scale)) ** 2
                )
        return float(np.median(np.abs(tall - share * low))), share

    limit = MAX_PITCH + 4 * PITCH_STEP
    best = min(
        np.arange(-limit, limit + PITCH_STEP / 2, PITCH_STEP),
        key=lambda down: spread(float(down))[0],
    )
    best = min(
        np.arange(best - PITCH_STEP, best + PITCH_STEP, FINE_PITCH_STEP),
        key=lambda down: spread(float(down))[0],
    )
    rest, share = spread(float(best))
    return float(best), share, rest


def level(
    point: tuple[float, float], roll: float, centre: float
) -> tuple[float, float]:
    """Return a picture point as a camera without roll would show it."""
    sine, cosine = math.sin(roll), math.cos(roll)
    across, below = point[0] - centre, point[1] - CENTRE_ROW
    return (
        centre + cosine * across + sine * below,
        CENTRE_ROW + cosine * below - sine * across,
    )


def supported(model: dict) -> bool:
    """Whether a fitted camera is the eye-level tripod this module maps."""
    return (
        MIN_CAMERA_HEIGHT <= model["height"] <= MAX_CAMERA_HEIGHT
        and MIN_PERSON_HEIGHT <= model["person"] <= MAX_PERSON_HEIGHT
        and max(abs(p) for p in model["pitches"]) <= MAX_PITCH
    )


def apart(first: dict, second: dict) -> float:
    """Return how far two fitted cameras stand from each other, in metres."""
    return math.dist(first["camera"], second["camera"])


def sight(
    post: list,
    focal: float,
    centre: float,
    poses: tuple[NDArray[Any], NDArray[Any]],
) -> tuple[float, float]:
    """Return a korf's bearing, and its distance per metre it tops the camera."""
    _, np = modules()
    down, bearing = poses
    bearings, distances = [], []
    for x, top, frame in post:
        right, lower, forward = ray((x, top), focal, float(down[frame]), centre)
        if lower >= -EPSILON:
            continue  # The post top must be above the camera.
        bearings.append(float(bearing[frame]) + math.atan2(right, forward))
        distances.append(math.hypot(right, forward) / -lower)
    if not bearings:
        return 0.0, float("nan")
    return float(np.median(bearings)), float(np.median(distances))


def placed(solved: dict, court: tuple[float, float]) -> dict | None:
    """Turn a solved focal length into a camera position, if it has one."""
    length, width = court
    separation = length * (1 - 2 * POST_FROM_END)
    near, far = solved["near"], solved["far"]
    along = (near * near - far * far + separation * separation) / (2 * separation)
    beside = near * near - along * along
    if not math.isfinite(beside) or beside <= 0:
        return None
    camera = (
        length * POST_FROM_END + along,
        width / 2 - math.sqrt(beside),
    )
    left_post = (length * POST_FROM_END, width / 2)
    return {
        **solved,
        "camera": camera,
        "direction": math.atan2(left_post[1] - camera[1], left_post[0] - camera[0]),
    }


class Turns:
    """Follow how a camera turns, frame by frame, and what that shows of its lens."""

    def __init__(self) -> None:
        """Start before the first frame."""
        _, self.np = modules()
        self.previous: NDArray[Any] | None = None
        self.misses = 0
        self.zoom: deque = deque(maxlen=ZOOM_FRAMES)
        self.turned: NDArray[Any] = self.np.eye(3)
        # Focal lengths measured from the camera's turns, with their stretch.
        self.focals: list[tuple[int, float]] = []
        self.times: list[float] = []
        self.pans: list[float] = []
        self.tilts: list[float] = []
        # Frames share a stretch while the camera's turn was followed without
        # a break; a cut, a zoom or lost motion starts a new one. Frames whose
        # own motion was not measured have no usable pose.
        self.stretches: list[int] = []
        self.measured: list[bool] = []

    def warp(self, previous: NDArray[Any], gray: NDArray[Any]) -> NDArray[Any] | None:
        """Return the homography most of the picture, across its width, follows.

        Motion that only one part of the picture shares is something moving in
        front of the camera, not the camera.
        """
        cv, np = modules()
        columns, rows = FLOW_GRID
        wide, high = gray.shape[1], int(gray.shape[0] * (1 - GRAPHICS_BAND))
        # A detailed object would outvote a plain background, so every part
        # of the picture brings its own best points, and only a few.
        points, cells = [], []
        for cell in range(columns * rows):
            left, top = cell % columns * wide // columns, cell // columns * high // rows
            part = previous[top : top + high // rows, left : left + wide // columns]
            if part.std() < MIN_CELL_CONTRAST:
                continue
            corners = cv.goodFeaturesToTrack(part, CELL_POINTS, 0.01, 6)
            if corners is not None:
                points.append(corners.reshape(-1, 2) + np.array([left, top]))
                cells += [cell] * len(corners)
        if len(cells) < MIN_FLOW_POINTS:
            return None
        points = np.concatenate(points).astype(np.float32).reshape(-1, 1, 2)
        moved, found, _ = cv.calcOpticalFlowPyrLK(previous, gray, points, None)
        back, returned, _ = cv.calcOpticalFlowPyrLK(gray, previous, moved, None)
        drift = np.linalg.norm((back - points).reshape(-1, 2), axis=1)
        keep = (found.ravel() == 1) & (returned.ravel() == 1) & (drift < 1.0)
        if keep.sum() < MIN_FLOW_POINTS:
            return None
        spots, cells = points[keep].reshape(-1, 2), np.array(cells)[keep]
        found, support = cv.findHomography(
            spots, moved[keep].reshape(-1, 2), cv.RANSAC, 2.0
        )
        if found is None or support is None or not np.isfinite(found).all():
            return None
        agreeing = support.ravel() == 1
        filled = np.bincount(cells, minlength=columns * rows)
        shared = np.bincount(cells[agreeing], minlength=columns * rows)
        parts = filled >= MIN_CELL_POINTS
        if (
            agreeing.sum() < MIN_FLOW_POINTS
            or agreeing.sum() < MIN_SUPPORT * len(spots)
            or (shared[parts] > filled[parts] / 2).sum() < MIN_SUPPORT * parts.sum()
            or np.ptp(spots[agreeing, 0]) < MIN_SPREAD * gray.shape[1]
        ):
            return None
        return found

    def shift(self, previous: NDArray[Any], gray: NDArray[Any]) -> tuple | None:
        """Return the image centre's shift in image heights, the zoom and the warp.

        A turning camera moves the edges of the picture further than its
        centre; only the centre's shift is the focal length times the turn.
        Motion that most of the picture, across its width, does not share is
        something moving in front of the camera, not the camera.
        """
        _, np = modules()
        warp = self.warp(previous, gray)
        if warp is None:
            return None
        height, width = gray.shape
        corners = np.array([
            [width / 2, height / 2, 1.0],
            [width / 2 + 1, height / 2, 1.0],
            [width / 2, height / 2 + 1, 1.0],
        ])
        mapped = corners @ warp.T
        if (np.abs(mapped[:, 2]) < EPSILON).any():
            return None
        mapped = mapped[:, :2] / mapped[:, 2:]
        moved_by = (mapped[0] - corners[0, :2]) / height
        stretch = np.linalg.det(
            np.array([mapped[1] - mapped[0], mapped[2] - mapped[0]])
        )
        if not np.isfinite(moved_by).all() or stretch <= 0:
            return None
        # What a pure turn of this size would enlarge the centre by.
        turned = 1 + float(moved_by @ moved_by) / TYPICAL_FOCAL**2
        return moved_by, math.sqrt(stretch) / turned**0.75, warp

    def lens(self, warp: NDArray[Any], shape: tuple[int, ...], stretch: int) -> None:
        """Measure the focal length once the picture has turned far enough.

        A camera turning about its own centre maps the picture by a rotation
        seen through the lens: the picture's shift grows with the focal length
        and its perspective stretch shrinks with it, by the same turn.
        """
        np = self.np
        height, width = shape[:2]
        centred = np.array([
            [1.0, 0.0, -width / 2],
            [0.0, 1.0, -height / 2],
            [0.0, 0.0, 1.0],
        ])
        self.turned = centred @ warp @ np.linalg.inv(centred) @ self.turned
        scale = np.linalg.det(self.turned)
        if not np.isfinite(scale) or scale <= 0:
            self.turned = np.eye(3)
            return
        turn = self.turned / np.cbrt(scale)
        shift = math.hypot(turn[0, 2], turn[1, 2])
        if shift < FOCAL_TURN * height * abs(turn[2, 2]):
            return
        bend = math.hypot(turn[2, 0], turn[2, 1])
        self.turned = np.eye(3)
        if bend > EPSILON:
            focal = math.sqrt(shift / bend) / height
            if MIN_FOCAL <= focal <= MAX_FOCAL:
                self.focals.append((stretch, focal))

    def track(
        self, image: NDArray[Any], timestamp: float, *, cut: bool = False
    ) -> None:
        """Add one consecutive frame to the camera's pan and tilt path.

        Pan and tilt are kept in image heights, as the scene shift they cause.
        """
        cv, _ = modules()
        scale = TRACK_WIDTH / image.shape[1]
        gray = cv.cvtColor(
            cv.resize(image, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA),
            cv.COLOR_BGR2GRAY,
        )
        pan = self.pans[-1] if self.pans else 0.0
        tilt = self.tilts[-1] if self.tilts else 0.0
        stretch = self.stretches[-1] if self.stretches else 0
        measured, restart = True, False
        previous = self.previous
        if previous is None or cut or previous.shape != gray.shape:
            restart = previous is not None
        else:
            found = self.shift(previous, gray)
            if found is not None:
                self.zoom.append(math.log(found[1]))
                # A zoom keeps going for a while; single frames only jitter.
                if abs(sum(self.zoom)) > MAX_ZOOM:
                    restart = True  # The focal length changed: a new camera.
                else:
                    pan, tilt = pan + float(found[0][0]), tilt + float(found[0][1])
                    self.previous, self.misses = gray, 0
                    self.lens(found[2], gray.shape, stretch)
            elif self.misses < MAX_FLOW_MISSES:
                # Keep comparing with the last good frame; this one is unknown.
                self.misses += 1
                measured = False
            else:
                restart = True
        if previous is None or restart:
            stretch += int(restart)
            self.previous, self.misses = gray, 0
            self.zoom.clear()
            self.turned = self.np.eye(3)
        self.times.append(timestamp)
        self.pans.append(pan)
        self.tilts.append(tilt)
        self.stretches.append(stretch)
        self.measured.append(measured)

    def frame(self, timestamp: float) -> int | None:
        """Return the tracked frame with a measured pose nearest to a time."""
        if not self.times:
            return None
        position = bisect_left(self.times, timestamp)
        nearest = min(
            (i for i in (position - 1, position) if 0 <= i < len(self.times)),
            key=lambda i: abs(self.times[i] - timestamp),
        )
        if (
            abs(self.times[nearest] - timestamp) > MAX_TIME_GAP
            or not self.measured[nearest]
        ):
            return None
        return nearest


class FieldCamera(Turns):
    """Follow the camera through a clip, fit its geometry, then place each frame."""

    def __init__(self, court: dict) -> None:
        """Remember the court size; the posts stand a sixth in from each end."""
        super().__init__()
        self.length, self.width = float(court["length"]), float(court["width"])
        self.samples: list[dict] = []
        self.model: dict | None = None
        self.until = float("inf")
        self.roll = 0.0
        self.levelled = False
        self.sag: NDArray[Any] | None = None
        self.stances: dict[float, tuple[float, list[float]]] = {}

    def interrupt(self, timestamp: float) -> None:
        """Stop placing frames from a cut that playback found on its own."""
        self.until = min(self.until, timestamp)

    @staticmethod
    def promising(samples: list[list[dict]]) -> bool:
        """Whether sampled frames show enough people and korfs to try a fit."""
        bodies = sum(
            obj["label"] in {"player", "referee"}
            and obj.get("confidence", 0) >= MIN_CONFIDENCE
            for objects in samples
            for obj in objects
        )
        posts = sum(
            any(
                obj["label"] == "basket"
                and obj.get("confidence", 0) >= MIN_POST_CONFIDENCE
                for obj in objects
            )
            for objects in samples
        )
        return bodies >= MIN_VIEW_BODIES and posts >= 2 * MIN_VIEW_POSTS

    def sample(self, timestamp: float, objects: list[dict], aspect: float) -> None:
        """Keep one detected frame's standing bodies and korf tops."""
        frame = self.frame(timestamp)
        if frame is None:
            return
        bodies, posts = [], []
        for obj in objects:
            x, y, w, h = obj.get("observed_bbox") or obj["bbox"]
            confidence = obj.get("confidence", 0)
            if (
                obj["label"] in {"player", "referee"}
                and confidence >= MIN_CONFIDENCE
                and y + h < MAX_FOOT
                and w * aspect / max(h, EPSILON) < MAX_BODY_ASPECT
            ):
                bodies.append((y + h, y, (x + w / 2) * aspect))
            elif obj["label"] == "basket" and confidence >= MIN_POST_CONFIDENCE:
                posts.append(((x + w / 2) * aspect, y, w))
        self.samples.append({
            "frame": frame,
            "pan": self.pans[frame],
            "tilt": self.tilts[frame],
            "stretch": self.stretches[frame],
            "bodies": bodies,
            "posts": posts,
            "aspect": aspect,
        })

    def path(self) -> tuple[NDArray[Any], NDArray[Any]]:
        """Return every frame's pan and tilt as a camera without roll turns."""
        np = self.np
        pans, tilts = np.array(self.pans), np.array(self.tilts)
        sine, cosine = math.sin(self.roll), math.cos(self.roll)
        pans, tilts = cosine * pans + sine * tilts, cosine * tilts - sine * pans
        if self.sag is not None and len(self.sag) == len(tilts):
            tilts -= self.sag
        return pans, tilts

    def straighten(self, stretch: int, views: list[dict], focal: float) -> list[dict]:
        """Take out the tilt a pitched camera only seems to make while it pans.

        A camera pitched down turns about the vertical, not about its own
        upright: the point at the picture's centre drifts down a little with
        every turn, to either side, without the camera tilting. Over repeated
        pans that drift adds up to a tilt that never happened.
        """
        np = self.np
        pans, _ = self.path()
        steps = np.diff(pans, prepend=pans[0])
        steps[np.array(self.stretches) != stretch] = 0.0
        # The drift depends on the pitch, which is itself read off the path.
        for _ in range(SAG_ROUNDS):
            self.stances = {}
            _, pitches = self.stance(views, focal)
            down, _ = self.poses(stretch, views, focal, pitches)
            self.sag = np.cumsum(steps * steps * np.tan(down) / (2 * focal))
            views = self.views(stretch)
        self.stances = {}
        return views

    def views(self, stretch: int) -> list[dict]:
        """Group samples taken while the camera rested on one part of the field."""
        np = self.np
        pans, tilts = self.path()
        groups: list[list[dict]] = []
        samples = [
            {
                **s,
                "pan": float(pans[s["frame"]]),
                "tilt": float(tilts[s["frame"]]),
                "bodies": [
                    (
                        *level((x, foot), self.roll, s["aspect"] / 2)[::-1],
                        level((x, head), self.roll, s["aspect"] / 2)[1],
                    )
                    for foot, head, x in s["bodies"]
                ],
                "posts": [
                    (*level((x, top), self.roll, s["aspect"] / 2), width)
                    for x, top, width in s["posts"]
                ],
            }
            for s in self.samples
            if s["stretch"] == stretch
        ]
        for sample in sorted(samples, key=operator.itemgetter("pan")):
            if groups and sample["pan"] - groups[-1][0]["pan"] <= VIEW_SPAN:
                groups[-1].append(sample)
            else:
                groups.append([sample])
        views = []
        for members in groups:
            if len(members) < MIN_VIEW_SAMPLES:
                continue
            pan = float(np.median([s["pan"] for s in members]))
            tilt = float(np.median([s["tilt"] for s in members]))
            bodies = np.array([
                (foot, head, s["tilt"] - tilt, x)
                for s in members
                for foot, x, head in s["bodies"]
            ])
            if len(bodies) < MIN_VIEW_BODIES:
                continue
            views.append({
                "pan": pan,
                "tilt": tilt,
                "bodies": bodies,
                "posts": self.posts(members, pan, tilt),
            })
        return views

    def posts(self, members: list[dict], pan: float, tilt: float) -> list[list]:
        """Return the korfs this view may show, the likeliest first.

        This field's korf is seen most and looks largest, but neighbouring
        fields show theirs too, so the runners-up stay candidates until the
        whole geometry is known. Each korf is a list of sightings with the
        frame they were seen in.
        """
        np = self.np
        seen = [
            (x - (s["pan"] - pan), top - (s["tilt"] - tilt), width, x, top, s["frame"])
            for s in members
            for x, top, width in s["posts"]
        ]
        if len(seen) < MIN_VIEW_POSTS:
            return []
        steady = np.array([row[:3] for row in seen])
        bins = np.round(steady[:, :2] / POST_BIN).astype(int)
        _, inverse, counts = np.unique(
            bins, axis=0, return_inverse=True, return_counts=True
        )
        inverse = inverse.ravel()
        widths = np.array([
            steady[inverse == number, 2].mean() for number in range(len(counts))
        ])
        ranked = sorted(
            (
                number
                for number in range(len(counts))
                if counts[number] >= MIN_VIEW_POSTS
            ),
            key=lambda number: -counts[number] * widths[number],
        )
        return [
            [
                row[3:]
                for row, group in zip(seen, inverse, strict=True)
                if group == number
            ]
            for number in ranked[:MAX_POST_CHOICES]
        ]

    def lean(self, views: list[dict], centre: float) -> float | None:
        """Return the roll that standing bodies across the picture still show.

        In a rolled picture the horizon slants, so bodies on one side seem to
        stand too low for their height and those on the other side too high.
        Bodies in too narrow a part of the picture show no roll: None.
        """
        np = self.np
        if not views:
            return None
        rests, slants = [], []
        for view in views:
            bodies = view["bodies"]
            down, share, _ = upright(bodies, TYPICAL_FOCAL)
            turned = down - bodies[:, 2] / TYPICAL_FOCAL
            low = tangents(bodies[:, 0], TYPICAL_FOCAL, turned)
            high = tangents(bodies[:, 1], TYPICAL_FOCAL, turned)
            usable = low > EPSILON
            rests.append(((low - high) - share * low)[usable])
            slants.append(
                (-share * (bodies[:, 3] - centre) * (1 + low * low) / TYPICAL_FOCAL)[
                    usable
                ]
            )
        rest, slant = np.concatenate(rests), np.concatenate(slants)
        if (
            len(rest) < MIN_VIEW_BODIES
            or np.ptp(slant) * TYPICAL_FOCAL < MIN_ROLL_SPREAD
        ):
            return None
        weights = np.ones(len(rest))
        roll = 0.0
        for _ in range(FIT_ROUNDS):
            mean = (weights * slant).sum() / weights.sum()
            base = (weights * rest).sum() / weights.sum()
            spread = (weights * (slant - mean) ** 2).sum()
            if spread < EPSILON:
                return None
            roll = float((weights * (slant - mean) * (rest - base)).sum() / spread)
            left = rest - base - roll * (slant - mean)
            scale = float(np.median(np.abs(left))) * 1.48 + EPSILON
            weights = 1 / (1 + (left / (2 * scale)) ** 2)
        return roll

    def stance(self, views: list[dict], focal: float) -> tuple[float, list[float]]:
        """Return body height over camera height, and each view's pitch."""
        if focal not in self.stances:
            shares = [upright(view["bodies"], focal)[1] for view in views]
            share = float(self.np.median(shares))
            self.stances[focal] = (
                share,
                [upright(view["bodies"], focal, share)[0] for view in views],
            )
        return self.stances[focal]

    def poses(
        self, stretch: int, views: list[dict], focal: float, pitches: list[float]
    ) -> tuple[NDArray[Any], NDArray[Any]]:
        """Return every frame's pitch and bearing for a focal length.

        Each view anchors the pitch where it was measured from standing
        bodies; between views the anchor is interpolated, so tilt that drifted
        while the camera swept across does not carry over. A tripod turns
        about the vertical, so a pitched camera's picture shifts less per
        degree of turn.
        """
        np = self.np
        pans, tilts = self.path()
        ordered = sorted(
            zip(views, pitches, strict=True), key=lambda pair: pair[0]["pan"]
        )
        anchors = [pitch + view["tilt"] / focal for view, pitch in ordered]
        anchor = np.interp(pans, [view["pan"] for view, _ in ordered], anchors)
        down = anchor - tilts / focal
        steps = np.diff(pans, prepend=pans[0])
        steps[np.array(self.stretches) != stretch] = 0.0
        return down, -np.cumsum(steps / (focal * np.cos(down)))

    def solve(self, scene: tuple, focal: float, centre: float) -> dict | None:
        """Fit pitch, camera height and position for a focal length and two korfs.

        `scene` holds the stretch, its views and the left and right korf. Both
        korfs top the camera by the same height, so the triangle they form
        with the camera has a known shape; the distance between the posts
        gives its size, and with it the camera's height.
        """
        stretch, views, left, right = scene
        share, pitches = self.stance(views, focal)
        poses = self.poses(stretch, views, focal, pitches)
        bearing, near = sight(left, focal, centre, poses)
        other, far = sight(right, focal, centre, poses)
        between = near * near + far * far - 2 * near * far * math.cos(other - bearing)
        if not math.isfinite(between) or between <= EPSILON:
            return None
        separation = self.length * (1 - 2 * POST_FROM_END)
        above = separation / math.sqrt(between)
        height = POST_HEIGHT - above
        return placed(
            {
                "centre": centre,
                "roll": self.roll,
                "focal": focal,
                "height": height,
                "person": share * height,
                "bearing": bearing,
                "near": near * above,
                "far": far * above,
                "poses": poses,
                "pitches": pitches,
                "scene": scene,
            },
            (self.length, self.width),
        )

    def on_court(self, model: dict, stretch: int) -> float:
        """Return the share of sampled feet this camera puts on or near the court."""
        np = self.np
        inside = total = 0
        for sample in self.samples:
            if sample["stretch"] != stretch or not sample["bodies"]:
                continue
            floor = self.floor(model, sample["frame"], 1.0)
            for foot, _, x in sample["bodies"]:
                total += 1
                point = floor @ np.array([x, foot, 1.0])
                if point[2] <= EPSILON:
                    continue
                east, north = point[0] / point[2], point[1] / point[2]
                inside += (
                    -COURT_MARGIN <= east <= self.length + COURT_MARGIN
                    and -COURT_MARGIN <= north <= self.width + COURT_MARGIN
                )
        return inside / max(total, 1)

    def lean_views(self, stretch: int, centre: float) -> list[dict]:
        """Measure the picture's roll and return the stretch's levelled views."""
        self.roll, self.levelled = 0.0, False
        views = self.views(stretch)
        first = self.lean(views, centre)
        # Pitch and body size soak up part of a roll, more so when the players
        # stand close together: the roll that still shows is only a share of
        # what is left. A small trial roll shows how large a share.
        self.roll = ROLL_PROBE
        rest = self.lean(self.views(stretch), centre)
        if first is None or rest is None:
            return views
        share = (first - rest) / ROLL_PROBE
        for _ in range(ROLL_ROUNDS):
            if share < MIN_ROLL_SHARE:
                break  # Too little of a roll shows to tell how large it is.
            at, step = self.roll, rest / share
            self.roll = max(-2 * MAX_ROLL, min(2 * MAX_ROLL, at + step))
            views = self.views(stretch)
            if abs(step) < ROLL_STEP:
                self.levelled = True
                break
            again = self.lean(views, centre)
            if again is None or abs(self.roll - at) < EPSILON:
                break
            share, rest = (rest - again) / (self.roll - at), again
        return views

    def upright_views(
        self, stretch: int, centre: float, focals: list[float]
    ) -> list[dict]:
        """Settle the roll and a pitched camera's pan drift together.

        The drift misplaces the bodies the roll is read from, and the roll
        turns the path the drift is read from.
        """
        self.sag = None
        views = self.lean_views(stretch, centre)
        for _ in range(SAG_ROUNDS):
            if len(focals) < MIN_FOCALS or not views or not self.levelled:
                break
            self.straighten(stretch, views, float(self.np.median(focals)))
            views = self.lean_views(stretch, centre)
        return views

    def choose(self, found: list[dict], stretch: int) -> dict | None:
        """Return the camera that puts most feet on the court, if one stands out.

        Another korf that explains the players about as well, from another
        place, leaves the field's identity open.
        """
        scored = sorted(
            ((self.on_court(model, stretch), model) for model in found),
            key=operator.itemgetter(0),
            reverse=True,
        )
        best, model = scored[0]
        rivals = [
            score for score, other in scored[1:] if apart(model, other) > SAME_CAMERA
        ]
        if best < MIN_ON_COURT or (rivals and best - rivals[0] < MIN_LEAD):
            return None
        return model

    def settle(
        self, shot: tuple, focal: float, centre: float
    ) -> tuple[str, dict | None]:
        """Return the one camera the stretch supports, or why there is none."""
        stretch, views = shot
        views = self.straighten(stretch, views, focal)
        located = [view for view in views if view["posts"]]
        if len(located) < 2:  # noqa: PLR2004
            return "one_post", None
        # The scene shifts right when the camera turns left, so the larger pan
        # is the view further to the left.
        left = max(located, key=operator.itemgetter("pan"))
        right = min(located, key=operator.itemgetter("pan"))
        self.stances = {}
        found = []
        for near in left["posts"]:
            for far in right["posts"]:
                model = self.solve((stretch, views, near, far), focal, centre)
                if model is not None and supported(model):
                    found.append(model)
        if not found:
            return "unsupported_camera", None
        model = self.choose(found, stretch)
        if model is None:
            return "ambiguous_camera", None
        # The focal length's doubt must leave the camera in place.
        for doubt in (1 - FOCAL_DOUBT, 1 + FOCAL_DOUBT):
            other = self.solve(model["scene"], model["focal"] * doubt, centre)
            if other is None or apart(model, other) > MAX_CAMERA_DOUBT:
                return "unstable_camera", None
        return "completed", model

    def fit(self) -> dict:
        """Solve camera height, pitch, focal length and position from two posts."""
        self.model = None
        counts: dict[int, int] = {}
        for sample in self.samples:
            counts[sample["stretch"]] = counts.get(sample["stretch"], 0) + 1
        if not counts:
            return {"status": "no_views"}
        # The longest unbroken stretch is the tripod shot to calibrate.
        stretch = max(counts, key=lambda number: counts[number])
        centre = self.samples[0]["aspect"] / 2
        focals = [focal for number, focal in self.focals if number == stretch]
        views = self.upright_views(stretch, centre, focals)
        located = [view for view in views if view["posts"]]
        status = None
        if not views:
            status = "no_views"
        elif len(located) < 2:  # noqa: PLR2004
            status = "one_post"
        elif len(focals) < MIN_FOCALS:
            status = "no_focal_length"
        elif abs(self.roll) > MAX_ROLL or not self.levelled:
            status = "unsupported_camera"
        if status is not None:
            return {"status": status, "views": len(views)}
        focal = float(self.np.median(focals))
        status, model = self.settle((stretch, views), focal, centre)
        if model is None:
            return {"status": status, "views": len(views)}
        self.model = {**model, "stretch": stretch}
        return {
            "status": "completed",
            "views": len(views),
            "camera_roll_degrees": round(math.degrees(self.roll), 1),
            "camera_height_m": round(model["height"], 2),
            "player_height_m": round(model["person"], 2),
            "camera_xy_m": [round(float(v), 1) for v in model["camera"]],
            "horizontal_view_degrees": round(
                math.degrees(2 * math.atan(centre / model["focal"])), 1
            ),
            "camera_pitch_degrees": round(
                math.degrees(max(model["pitches"], key=abs)), 1
            ),
            "post_distances_m": [
                round(float(model["near"]), 1),
                round(float(model["far"]), 1),
            ],
        }

    def floor(self, model: dict, frame: int, aspect: float) -> NDArray[Any]:
        """Return one tracked frame's image-to-court mapping.

        Columns take x in units of `1 / aspect` image heights: 1.0 for points
        already in image heights, the aspect ratio for normalised coordinates.
        """
        np = self.np
        focal, height, centre = model["focal"], model["height"], model["centre"]
        down, bearing = (float(values[frame]) for values in model["poses"])
        # Turning right lowers the mathematical angle of the viewing direction.
        forward = model["direction"] - (bearing - model["bearing"])
        sine, cosine = math.sin(down), math.cos(down)
        # Rows give right, forward and the divisor for a pixel's ground point:
        # the ray through the pixel, scaled to drop the camera's height.
        ground = np.array([
            [height, 0.0, -height * centre],
            [0.0, -height * sine, height * (sine * CENTRE_ROW + cosine * focal)],
            [0.0, cosine, -cosine * CENTRE_ROW + sine * focal],
        ])
        placed = np.array([
            [math.sin(forward), math.cos(forward), model["camera"][0]],
            [-math.cos(forward), math.sin(forward), model["camera"][1]],
            [0.0, 0.0, 1.0],
        ])
        roll_sine, roll_cosine = math.sin(model["roll"]), math.cos(model["roll"])
        level = np.array([
            [
                roll_cosine,
                roll_sine,
                centre - roll_cosine * centre - roll_sine * CENTRE_ROW,
            ],
            [
                -roll_sine,
                roll_cosine,
                CENTRE_ROW - roll_cosine * CENTRE_ROW + roll_sine * centre,
            ],
            [0.0, 0.0, 1.0],
        ])
        return placed @ ground @ level @ np.diag([aspect, 1.0, 1.0])

    def locate(self, timestamp: float, aspect: float) -> tuple | None:
        """Return the frame's image-to-court mapping and its evidence."""
        np = self.np
        model, frame = self.model, self.frame(timestamp)
        # Another camera, or a turn that was lost on the way, is not this view.
        if (
            model is None
            or frame is None
            or timestamp >= self.until
            or self.stretches[frame] != model["stretch"]
        ):
            return None
        down = float(model["poses"][0][frame])
        floor = self.floor(model, frame, aspect)
        if abs(down) > MAX_PITCH or not np.isfinite(floor).all():
            return None
        return floor, {
            "status": "automatic_field",
            "estimated": True,
            "segments": [],
            "horizon": round(CENTRE_ROW - model["focal"] * math.tan(down), 4),
        }
