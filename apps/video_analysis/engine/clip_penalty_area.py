"""Fit the complete optional penalty oval when a pole base is occluded.

The two circular ends and straight sides supply the direction missing from a
single circle. This fallback is used on masked temporal reference images only;
a coloured patch, an isolated ellipse, or a basket alone is insufficient.
"""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Any

from .clip_auto_court import Landmarks, circle_plane, project
from .clip_penalty_fit import refine


if TYPE_CHECKING:
    from numpy.typing import NDArray

MIN_SUPPORT = 0.72
MIN_VISIBLE = 0.65
MIN_INNER_VISIBLE = 0.5
INNER_ARC_INDEX = 4
MIN_SAMPLES = 30
MAX_DISTANCE = 4
MAX_CONTOURS = 20
MIN_CONTOUR_POINTS = 60
MIN_SPOT_INK = 3
SPOT_PIXEL_LIMIT = 6
ANGLE_SAMPLES = 720
AMBIGUITY_MARGIN = 0.04
MAX_DISAGREEMENT_METRES = 0.75
MAX_REFINEMENT_COST = 6
MAX_REFINEMENTS = 4


def fragments(observed: Landmarks) -> list:
    """Split bounded contours so an attached capsule side cannot distort a circle."""
    contours, _ = observed.cv.findContours(
        observed.ink, observed.cv.RETR_LIST, observed.cv.CHAIN_APPROX_NONE
    )
    parts = []
    for contour in sorted(
        contours, key=lambda c: int(observed.np.ptp(c[:, 0, 0])), reverse=True
    )[:MAX_CONTOURS]:
        for fraction in (1, 0.75, 0.5, 0.25):
            size = round(len(contour) * fraction)
            if size >= MIN_CONTOUR_POINTS:
                parts.extend(
                    contour[start : start + size]
                    for start in range(0, len(contour) - size + 1, max(1, size // 2))
                )
    return parts


def boundaries(observed: Landmarks, court: dict, left: bool) -> list:
    """Sample both round ends and both straight sides independently in metres."""
    np = observed.np
    post = court["length"] * (1 / 6 if left else 5 / 6)
    direction = 1 if left else -1
    centre = court["width"] / 2
    pieces = []
    for offset, start in ((0, np.pi / 2), (2.5, -np.pi / 2)):
        angles = np.linspace(start, start + np.pi, 120)
        pieces.append(
            np.c_[
                post + direction * (offset + 2.5 * np.cos(angles)),
                centre + 2.5 * np.sin(angles),
            ]
        )
    pieces.extend(
        np.c_[np.linspace(post, post + direction * 2.5, 60), np.full(60, centre + y)]
        for y in (-2.5, 2.5)
    )
    # The inner half of the compulsory circle distinguishes the real spot from
    # a shadow inside an otherwise correctly fitted coloured oval.
    angles = np.linspace(np.pi / 2, 3 * np.pi / 2, 120)
    pieces.append(
        np.c_[
            post + direction * (2.5 + 2.5 * np.cos(angles)),
            centre + 2.5 * np.sin(angles),
        ]
    )
    return pieces


def measure(observed: Landmarks, floor: NDArray[Any], pieces: list) -> tuple:
    """Require independently visible, supported ends and sides, not one good arc."""
    np = observed.np
    inverse = np.linalg.inv(floor)
    scores, errors = [], []
    for index, piece in enumerate(pieces):
        homogeneous = np.c_[piece, np.ones(len(piece))] @ inverse.T
        if not np.isfinite(homogeneous).all() or (homogeneous[:, 2] <= 0).any():
            return 0.0, float("inf")
        pixels = (
            (
                homogeneous[:, :2]
                / homogeneous[:, 2:]
                * [observed.width, observed.height]
            )
            .round()
            .astype(int)
        )
        inside = (
            (pixels[:, 0] >= 0)
            & (pixels[:, 0] < observed.width)
            & (pixels[:, 1] >= 0)
            & (pixels[:, 1] < observed.height)
        )
        pixels = pixels[inside]
        pixels = pixels[observed.mask[pixels[:, 1], pixels[:, 0]] > 0]
        if len(pixels) < max(
            MIN_SAMPLES,
            len(piece)
            * (MIN_INNER_VISIBLE if index == INNER_ARC_INDEX else MIN_VISIBLE),
        ):
            return 0.0, float("inf")
        score = float(
            (observed.distance[pixels[:, 1], pixels[:, 0]] <= MAX_DISTANCE).mean()
        )
        errors.append(
            float(np.minimum(observed.distance[pixels[:, 1], pixels[:, 0]], 12).mean())
        )
        scores.append(score)
    return (
        float(np.mean(scores)) if min(scores) >= MIN_SUPPORT else 0.0,
        float(np.mean(errors)),
    )


def candidates(observed: Landmarks, court: dict, circles: list) -> list:
    """Use an observed spot and basket to disambiguate the capsule's physical axis."""
    np = observed.np
    found, pending = [], []
    angles = np.linspace(0, 2 * np.pi, ANGLE_SAMPLES, endpoint=False)
    unit = np.c_[np.cos(angles), np.sin(angles)]
    for ellipse, circle_support in circles:
        spot = observed.spot(ellipse, pixel_limit=SPOT_PIXEL_LIMIT)
        if spot is None:
            continue
        poles = project(np.linalg.inv(observed.ellipse_map(ellipse)), unit)
        for basket in observed.baskets:
            x, y, w, h = np.array(basket["bbox"]) * [
                observed.width,
                observed.height,
                observed.width,
                observed.height,
            ]
            eligible = poles[
                (poles[:, 0] > x - w * 0.5)
                & (poles[:, 0] < x + w * 1.5)
                & (poles[:, 1] > y + h + observed.height * 0.15)
            ]
            pieces = boundaries(observed, court, x + w / 2 < spot[0])
            for pole in eligible:
                try:
                    floor = circle_plane(
                        ellipse, spot, pole, court, observed.gray.shape
                    )
                    score, cost = measure(observed, floor, pieces)
                except (ValueError, np.linalg.LinAlgError):
                    continue
                if cost < MAX_REFINEMENT_COST:
                    pending.append((
                        cost,
                        (ellipse, spot, pole, pieces),
                        circle_support,
                    ))
                if score:
                    found.append((
                        score,
                        floor,
                        {
                            "status": "automatic",
                            "estimated": True,
                            "method": "penalty_area",
                            "circle_support": round(circle_support, 3),
                            "area_support": round(score, 3),
                            "observed_spot": (
                                spot / [observed.width, observed.height]
                            ).tolist(),
                            "inferred_pole": (
                                pole / [observed.width, observed.height]
                            ).tolist(),
                            "segments": [],
                        },
                    ))
    if not found:
        found = refined_candidates(observed, court, pending)
    return sorted(found, key=itemgetter(0), reverse=True)


def estimate(
    image: NDArray[Any], objects: list, court: dict, valid: NDArray[Any]
) -> tuple | None:
    """Accept only a complete, unambiguous penalty-area fit on a temporal image."""
    observed = Landmarks(image, objects)
    cv, np = observed.cv, observed.np
    if not observed.baskets:
        return None
    observed.mask[
        cv.resize(
            valid, (observed.width, observed.height), interpolation=cv.INTER_NEAREST
        )
        == 0
    ] = 0
    observed.mask = cv.erode(observed.mask, np.ones((5, 5), np.uint8))
    gray = cv.GaussianBlur(observed.gray, (3, 3), 0.7)
    kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (21, 21))
    blackhat = cv.morphologyEx(observed.gray, cv.MORPH_BLACKHAT, kernel)
    _, _, stats, centres = cv.connectedComponentsWithStats(
        np.uint8((blackhat > MIN_SPOT_INK) & (observed.mask > 0))
    )
    observed.spots = list(zip(stats[1:], centres[1:], strict=True))
    edges = cv.Canny(gray, 10, 30)
    edges[observed.mask == 0] = 0
    observed.distance = cv.distanceTransform(255 - edges, cv.DIST_L2, 3)
    observed.ink = edges.copy()
    observed.ink[observed.ink_mask == 0] = 0
    # Excluded shaft pixels must not count against the circle proposal. Restore
    # the actual visibility mask for the independent complete-outline check.
    visible = observed.mask.copy()
    observed.mask &= observed.ink_mask
    circles = observed.circles(fragments(observed))
    observed.mask = visible
    # Keep contour fitting bounded and reuse precisely the verified proposals.
    found = candidates(observed, court, circles)
    if not found:
        return None
    score, floor, evidence = found[0]
    spot = np.array(evidence["observed_spot"])
    sample = spot + np.array([[0, 0], [-0.08, 0], [0.08, 0], [0, 0.03]])
    for other_score, other, _ in found[1:]:
        if other_score < score - AMBIGUITY_MARGIN:
            break
        if (
            np.linalg.norm(
                project(floor, sample) - project(other, sample), axis=1
            ).max()
            > MAX_DISAGREEMENT_METRES
        ):
            return None
    return floor, evidence


def refined_candidates(observed: Landmarks, court: dict, pending: list) -> list:
    """Refine a few plausible measurements, retaining the same visibility gates."""
    found = []
    for _, proposal, circle_support in sorted(pending, key=itemgetter(0))[
        :MAX_REFINEMENTS
    ]:
        floor = refine(observed, court, proposal)
        if floor is None:
            continue
        score, _ = measure(observed, floor, proposal[3])
        if score:
            spot = proposal[1]
            post_x = court["length"] * (1 / 6 if proposal[2][0] < spot[0] else 5 / 6)
            pole = project(
                observed.np.linalg.inv(floor), [[post_x, court["width"] / 2]]
            )[0]
            pixel = pole * [observed.width, observed.height]
            if not any(
                (b["bbox"][0] - b["bbox"][2] * 0.5) * observed.width
                < pixel[0]
                < (b["bbox"][0] + b["bbox"][2] * 1.5) * observed.width
                and pixel[1] > (b["bbox"][1] + b["bbox"][3] + 0.15) * observed.height
                for b in observed.baskets
            ):
                continue
            found.append((
                score,
                floor,
                {
                    "status": "automatic",
                    "estimated": True,
                    "method": "penalty_area",
                    "circle_support": round(circle_support, 3),
                    "area_support": round(score, 3),
                    "observed_spot": (
                        spot / [observed.width, observed.height]
                    ).tolist(),
                    "inferred_pole": pole.tolist(),
                    "refined": True,
                    "segments": [],
                },
            ))
    return found
