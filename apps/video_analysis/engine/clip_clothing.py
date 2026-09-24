"""Visible clothing evidence against a learned palette, excluding other bodies."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_signals import Teams


MIN_PIXELS = 30
MIN_SUPPORT = 0.12
MIN_DOMINANCE = 0.8
MIN_VISIBLE = 0.2
MAX_DISTANCE = 55
MIN_MARGIN = 0.55
BACKGROUND_DISTANCE = 22


def foreground(
    teams: Teams, values: NDArray[Any], background: NDArray[Any] | None
) -> NDArray[Any]:
    """Remove a coherent surrounding colour when a distinct patch survives.

    A leaning body's shirt may occupy a small fraction of its bounding box.
    Require actual pixel support rather than a foreground percentage, and keep
    uniform or varied scenes unchanged when there is no separable background.
    """
    values = values.reshape(-1, 3)
    if (
        background is None
        or len(values) < MIN_PIXELS
        or background.size < MIN_PIXELS * 3
    ):
        return values
    cv, np = teams.cv, teams.np
    lab = (
        cv
        .cvtColor(values.reshape(-1, 1, 3), cv.COLOR_BGR2LAB)
        .reshape(-1, 3)
        .astype(float)
    )
    surroundings = (
        cv
        .cvtColor(background.reshape(-1, 1, 3), cv.COLOR_BGR2LAB)
        .reshape(-1, 3)
        .astype(float)
    )
    median = np.median(surroundings, axis=0)
    spread = np.median(np.linalg.norm(surroundings - median, axis=1))
    keep = np.linalg.norm(lab - median, axis=1) > BACKGROUND_DISTANCE
    if spread < BACKGROUND_DISTANCE and min(keep.sum(), (~keep).sum()) >= MIN_PIXELS:
        return values[keep]
    return values


def pixels(
    teams: Teams,
    image: NDArray[Any],
    box: list,
    others: Sequence[list] = (),
    *,
    wide: bool = False,
) -> tuple:
    """Mask overlapping boxes rather than learning their jerseys as this player's."""
    np = teams.np
    h, w = image.shape[:2]
    x, y, bw, bh = box
    inset = 0.05 if wide else 0.25
    left, right = (
        max(0, int((x + inset * bw) * w)),
        min(w, int((x + (1 - inset) * bw) * w)),
    )
    top, bottom = max(0, int((y + 0.18 * bh) * h)), min(h, int((y + 0.48 * bh) * h))
    crop = image[top:bottom, left:right]
    mask = np.ones(crop.shape[:2], dtype=bool)
    for a, b, c, d in others:
        x0, x1 = max(left, int(a * w)), min(right, int((a + c) * w))
        y0, y1 = max(top, int(b * h)), min(bottom, int((b + d) * h))
        if x1 > x0 and y1 > y0:
            mask[y0 - top : y1 - top, x0 - left : x1 - left] = False
    return (
        foreground(teams, crop[mask], teams.background(image, box)),
        float(mask.mean()) if mask.size else 0,
    )


def votes(teams: Teams, values: NDArray[Any]) -> tuple:
    """Count confident palette pixels, allowing skin and numbers to abstain."""
    assert teams.centers is not None
    cv, np = teams.cv, teams.np
    lab = (
        cv
        .cvtColor(values.reshape(-1, 1, 3), cv.COLOR_BGR2LAB)
        .reshape(-1, 3)
        .astype(float)
    )
    features = teams.features(lab)
    distances = np.linalg.norm(
        features[:, None] - teams.features(teams.centers), axis=2
    )
    margin = abs(distances[:, 0] - distances[:, 1]) / np.maximum(
        1, distances.sum(axis=1)
    )
    winners = distances.argmin(axis=1)
    reliable = (distances.min(axis=1) < MAX_DISTANCE) & (margin >= MIN_MARGIN)
    return lab, [reliable & (winners == k) for k in (0, 1)]


def sample(
    teams: Teams,
    image: NDArray[Any],
    box: list,
    others: Sequence[list] = (),
    *,
    wide: bool = False,
) -> tuple:
    """Return a single supported shirt, or explicit mixed/occluded evidence."""
    np = teams.np
    values, visible = pixels(teams, image, box, others, wide=wide)
    if visible < MIN_VISIBLE or len(values) < MIN_PIXELS:
        return None, "occluded"
    if teams.centers is None:
        return teams.sample(values.reshape(-1, 1, 3)), "unclassified"
    lab, masks = votes(teams, values)
    counts = [int(mask.sum()) for mask in masks]
    winner = int(np.argmax(counts))
    if counts[winner] < MIN_PIXELS or counts[winner] / len(values) < MIN_SUPPORT:
        return None, "unclassified"
    if counts[winner] / max(1, sum(counts)) < MIN_DOMINANCE:
        return None, "mixed"
    return np.median(lab[masks[winner]], axis=0), "visible"


def mixed(teams: Teams, image: NDArray[Any], box: list) -> bool:
    """Look for a second shirt at torso edges as well as in the centre.

    A foreground body can fill the central sample while the returning player's
    shirt remains visible beside it. This only requests a detector crop; two
    independently supported bodies are still required before splitting a box.
    """
    if teams.centers is None:
        return False
    for wide in (False, True):
        values, _ = pixels(teams, image, box, wide=wide)
        if len(values) < MIN_PIXELS:
            continue
        _, masks = votes(teams, values)
        counts = [int(mask.sum()) for mask in masks]
        # Requesting another observation is weaker than assigning a team. A
        # smaller opposing patch can justify a crop without making the main
        # player's supported shirt classification ambiguous.
        if (
            min(counts) >= MIN_PIXELS
            and min(counts) / max(1, sum(counts)) >= MIN_SUPPORT
        ):
            return True
    return False
