"""Keep the referee a referee when the detector calls them a player.

On club footage the detector often reports the referee as a player, sometimes
as both in one frame. The referee then gets a team colour, is tracked and
linked as a player and appears on the court map. A referee is one person in
one kit: once seen as a referee, the box that continues them in that kit stays
a referee, and a second player box on the same body is dropped.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, cast

from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

MIN_REFEREE_CONFIDENCE = 0.3
MIN_PLAYER_CONFIDENCE = 0.1
SAME_BODY_IOU = 0.5
CONTINUES_IOU = 0.3
MAX_GAP_SECONDS = 1.0
MIN_KIT_SAMPLES = 5
KIT_SAMPLES = 50
MAX_KIT_DISTANCE = 30.0
# Without a known position the kit alone decides, so it must be unlike the players'.
CLOSE_KIT_DISTANCE = 18.0
PLAYER_SAMPLES = 400
MIN_PLAYER_SAMPLES = 40
MAX_PLAYERS_IN_KIT = 0.03
LIGHTNESS_WEIGHT = 0.5
TORSO = (0.3, 0.7, 0.2, 0.45)
EPSILON = 1e-9


def overlap(first: NDArray[Any], second: NDArray[Any]) -> float:
    """Return the IoU of two corner boxes."""
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    if width <= 0 or height <= 0:
        return 0.0
    shared = width * height
    areas = (first[2] - first[0]) * (first[3] - first[1]) + (second[2] - second[0]) * (
        second[3] - second[1]
    )
    return float(shared / max(areas - shared, EPSILON))


def kit(image: NDArray[Any], box: NDArray[Any]) -> NDArray[Any] | None:
    """Return the median Lab colour of a pixel box's torso region."""
    cv, np = modules()
    x1, y1, x2, y2 = box[:4]
    width, height = x2 - x1, y2 - y1
    patch = image[
        max(0, int(y1 + TORSO[2] * height)) : max(0, int(y1 + TORSO[3] * height)) + 1,
        max(0, int(x1 + TORSO[0] * width)) : max(0, int(x1 + TORSO[1] * width)) + 1,
    ]
    if not patch.size:
        return None
    lab = cv.cvtColor(np.ascontiguousarray(patch), cv.COLOR_BGR2LAB)
    return np.median(lab.reshape(-1, 3), axis=0)


def alike(first: NDArray[Any] | None, second: NDArray[Any] | None) -> bool:
    """Whether two torso colours could be one kit; an unseen torso could be."""
    _, np = modules()
    if first is None or second is None:
        return True
    weights = np.array([LIGHTNESS_WEIGHT, 1.0, 1.0])
    return float(np.linalg.norm((first - second) * weights)) <= MAX_KIT_DISTANCE


def outvoted(
    referee: NDArray[Any], worn: NDArray[Any] | None, players: list[tuple]
) -> bool:
    """Whether a surer player box in a like kit shares a referee box's body.

    The detector sometimes adds a weak referee label to a player. A surer
    player in another kit is someone else standing in front. `players` holds
    each player's box and torso colour.
    """
    return any(
        overlap(box, referee) >= SAME_BODY_IOU
        and box[4] > referee[4]
        and alike(colour, worn)
        for box, colour in players
    )


class RefereeMemory:
    """Remember where the referee was and what they wear, within one camera shot."""

    def __init__(self) -> None:
        """Start without a referee."""
        _, self.np = modules()
        self.colours: deque = deque(maxlen=KIT_SAMPLES)
        self.players: deque = deque(maxlen=PLAYER_SAMPLES)
        self.box: NDArray[Any] | None = None
        self.time = -float("inf")
        self.relabelled = 0
        self.dropped = 0

    def preload(self, kits: list, players: list) -> None:
        """Start with the kit colours sampled across the whole clip.

        The detector may first call the referee a referee late in a shot; the
        samples already hold those later frames.
        """
        self.colours.extend(colour for colour in kits if colour is not None)
        self.players.extend(colour for colour in players if colour is not None)

    def reset(self) -> None:
        """Forget the position at a camera cut; the kit stays the same."""
        self.box = None
        self.time = -float("inf")

    def distance(self, colour: NDArray[Any] | None) -> float:
        """Return how far a torso colour is from the referee's usual kit."""
        np = self.np
        if colour is None or len(self.colours) < MIN_KIT_SAMPLES:
            return float("inf")
        weights = np.array([LIGHTNESS_WEIGHT, 1.0, 1.0])
        usual = np.median(np.array(self.colours), axis=0)
        return float(np.linalg.norm((colour - usual) * weights))

    def wears(self, colour: NDArray[Any] | None) -> bool:
        """Whether a torso colour matches the kit seen on referee detections."""
        return self.distance(colour) <= MAX_KIT_DISTANCE

    def close(self, colour: NDArray[Any] | None) -> bool:
        """Whether a torso colour is the referee's kit closely enough to stand alone."""
        return self.distance(colour) <= CLOSE_KIT_DISTANCE

    def distinctive(self) -> bool:
        """Whether hardly any player wears something like the referee's kit."""
        if len(self.players) < MIN_PLAYER_SAMPLES:
            return False
        close = sum(
            self.distance(colour) <= CLOSE_KIT_DISTANCE for colour in self.players
        )
        return close <= MAX_PLAYERS_IN_KIT * len(self.players)

    def correct(
        self,
        raw: object,
        image: NDArray[Any],
        timestamp: float,
        motion: NDArray[Any] | None = None,
    ) -> None:
        """Correct a detector result in place, for every later reader of it.

        Tracking, recovery and team colours all read the same result; row
        order is kept, so indices into it stay valid.
        """
        result = cast("Any", raw)
        kinds = (
            {k for k, v in result.names.items() if v in {"player", "person"}},
            {k for k, v in result.names.items() if v == "referee"},
        )
        boxes = result.boxes
        data = boxes.data
        rows = data.cpu().numpy() if hasattr(data, "cpu") else self.np.asarray(data)
        fixed = self.apply(rows, kinds, image, timestamp, motion)
        if fixed is rows:
            return
        replacement = data.new_tensor(fixed) if hasattr(data, "new_tensor") else fixed
        result.boxes = type(boxes)(replacement, boxes.orig_shape)

    def apply(
        self,
        detections: NDArray[Any],
        classes: tuple[set[int], set[int]],
        image: NDArray[Any],
        timestamp: float,
        motion: NDArray[Any] | None = None,
    ) -> NDArray[Any]:
        """Return detection rows with the referee's player boxes corrected.

        Rows are `[x1, y1, x2, y2, confidence, class]` in pixels; row order is
        kept. `classes` holds the player and the referee class numbers.
        """
        players, referees = classes
        # Carry the remembered box along with the camera on every frame, also
        # the ones without detections, so it stays in the current image.
        self.follow(image, motion)
        if not referees or not len(detections):
            return detections
        rows = detections.copy()
        referee = next(iter(referees))
        found = [
            index
            for index, row in enumerate(rows)
            if int(row[5]) in referees and row[4] >= MIN_REFEREE_CONFIDENCE
        ]
        candidates = [
            index
            for index, row in enumerate(rows)
            if int(row[5]) in players and row[4] >= MIN_PLAYER_CONFIDENCE
        ]
        colours = {index: kit(image, rows[index]) for index in candidates}
        found = self.trusted(rows, found, colours, image)
        if found:
            best = max(found, key=lambda index: rows[index, 4])
            colour = kit(image, rows[best])
            if colour is not None:
                self.colours.append(colour)
            # A second referee has player duplicates too. A player crossing
            # in front of a referee overlaps as much but wears another kit.
            taken = set()
            for index in candidates:
                if any(
                    overlap(rows[index], rows[other]) >= SAME_BODY_IOU
                    and alike(colours[index], kit(image, rows[other]))
                    for other in found
                ):
                    rows[index, 4] = 0.0
                    self.dropped += 1
                    taken.add(index)
            self.box, self.time = rows[best, :4].copy(), timestamp
            self.remember(colours, taken)
            return rows
        if self.box is None or timestamp - self.time > MAX_GAP_SECONDS:
            return self.by_kit(rows, colours, referee, timestamp)
        expected = self.box
        scored = [
            (overlap(rows[index], expected), index)
            for index in candidates
            if overlap(rows[index], expected) >= CONTINUES_IOU
            and self.wears(colours[index])
        ]
        if not scored:
            return self.by_kit(rows, colours, referee, timestamp)
        _, index = max(scored)
        self.remember(colours, self.claim(rows, colours, index, referee, timestamp))
        return rows

    def trusted(
        self, rows: NDArray[Any], found: list[int], colours: dict, image: NDArray[Any]
    ) -> list[int]:
        """Return the referee boxes to believe, silencing the others.

        The detector sometimes adds a weak referee label to a player. Where a
        surer player box shares the body, the referee label only stands when
        the body wears the referee's known kit.
        """
        kept = []
        for index in found:
            worn = kit(image, rows[index])
            players = [(rows[other], colour) for other, colour in colours.items()]
            if outvoted(rows[index], worn, players) and not self.wears(worn):
                rows[index, 4] = 0.0
            else:
                kept.append(index)
        return kept

    def claim(
        self,
        rows: NDArray[Any],
        colours: dict,
        index: int,
        referee: int,
        timestamp: float,
    ) -> set[int]:
        """Make one player box the referee and silence its copies in that kit.

        The detector may put two player boxes on the referee; the second one
        would stay a player on the referee's body. Returns the rows that were
        the referee, so their colours are not kept as a player's.
        """
        rows[index, 5] = referee
        self.box, self.time = rows[index, :4].copy(), timestamp
        self.relabelled += 1
        taken = {index}
        for other, colour in colours.items():
            if (
                other != index
                and overlap(rows[other], rows[index]) >= SAME_BODY_IOU
                and self.wears(colour)
            ):
                rows[other, 4] = 0.0
                self.dropped += 1
                taken.add(other)
        return taken

    def follow(self, image: NDArray[Any], motion: NDArray[Any] | None) -> None:
        """Move the remembered box by this frame's camera motion."""
        np = self.np
        if self.box is None or motion is None:
            return
        height, width = image.shape[:2]
        scale = np.diag([float(width), float(height), 1.0])
        pixels = scale @ motion @ np.linalg.inv(scale)
        corners = np.array([
            [self.box[0], self.box[1], 1.0],
            [self.box[2], self.box[3], 1.0],
        ])
        moved = corners @ pixels.T
        moved = moved[:, :2] / np.where(
            np.abs(moved[:, 2:]) < EPSILON, 1.0, moved[:, 2:]
        )
        if np.isfinite(moved).all():
            self.box = moved.ravel()

    def covers(self, box: list[float], image: NDArray[Any], timestamp: float) -> bool:
        """Whether a normalised `[x, y, w, h]` box is the referee seen this frame.

        A detection recovered from a crop never passed through `apply`; one on
        the referee's place this frame is the referee, not a recovered player.
        """
        if self.box is None or timestamp != self.time:
            return False
        height, width = image.shape[:2]
        x, y, w, h = box
        pixels = self.np.array([
            x * width,
            y * height,
            (x + w) * width,
            (y + h) * height,
        ])
        # A player in another kit in front of the referee is a player.
        return overlap(pixels, self.box) >= SAME_BODY_IOU and alike(
            kit(image, pixels), kit(image, self.box)
        )

    def remember(self, colours: dict, referee: set[int]) -> None:
        """Keep what the players wear, to judge how distinctive the kit is."""
        self.players.extend(
            colour
            for index, colour in colours.items()
            if index not in referee and colour is not None
        )

    def by_kit(
        self, rows: NDArray[Any], colours: dict, referee: int, timestamp: float
    ) -> NDArray[Any]:
        """Find the referee again by a kit no player wears, wherever they stand."""
        near = [
            index
            for index, colour in colours.items()
            if self.distance(colour) <= CLOSE_KIT_DISTANCE
        ]
        # Two boxes on one body are one person in that kit, not two.
        strongest = max(near, key=lambda index: rows[index, 4], default=None)
        alone = strongest is not None and all(
            overlap(rows[index], rows[strongest]) >= SAME_BODY_IOU
            for index in near
            if index != strongest
        )
        # The only person in the referee's kit may be the referee: until that
        # is settled, their colour is no evidence that players wear the kit.
        taken = set(near) if alone else set()
        if alone and strongest is not None and self.distinctive():
            taken = self.claim(rows, colours, strongest, referee, timestamp)
        self.remember(colours, taken)
        return rows

    def snapshot(self) -> dict:
        """Report how often the detector's class was corrected."""
        return {
            "version": 1,
            "relabelled_detections": self.relabelled,
            "dropped_duplicates": self.dropped,
        }
