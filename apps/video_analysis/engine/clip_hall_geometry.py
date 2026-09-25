"""Camera geometry and image measurements for the fixed-camera court model.

Image points are centred and expressed in image-width units, so a camera is
K = diag(f, f, 1) with a rotation R (world to camera). World coordinates are
court metres with z pointing down into the floor.
"""

from __future__ import annotations

import importlib
import math
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from . import clip_geometry as geometry
from .clip_signals import modules


if TYPE_CHECKING:
    from numpy.typing import NDArray

WIDTH, HEIGHT = 960, 540
FEATURES = 1500
NEIGHBOURS = 2
RATIO = 0.75
RANSAC_PIXELS = 2.5
MIN_INLIERS = 40
MIN_SUPPORT = 0.3
MIN_SPREAD_X = 0.3
MIN_SPREAD_Y = 0.15
MAX_ERROR_PIXELS = 1.5
BOX_MARGIN = 6
MIN_FOCAL, MAX_FOCAL = 0.25, 25.0
DEFAULT_FOCAL = 1.0
CLEAR_ROTATION_RESIDUAL = 0.1
MAX_ROTATION_RESIDUAL = 1.0
MAX_LINK_PIXELS = 1.2
LINK_FOCALS = (0.8, 1.2, 1.8, 2.7)
EPSILON = 1e-9
MIN_SEGMENT_PIXELS = 40
MAX_SEGMENTS = 200
OVERLAY_COLUMNS, OVERLAY_ROWS = 48, 27
OVERLAY_TOP, OVERLAY_BOTTOM = 0.12, 0.84
PLAYER_HEIGHT = 1.85
MIN_PERSON_HEIGHT = 0.04
MAX_PERSON_HEIGHT = 4.0
MIN_FOOT_DEPRESSION = math.sin(math.radians(3))
MAX_FOOT_DEPRESSION = math.sin(math.radians(35))


def rotations(vectors: NDArray[Any]) -> NDArray[Any]:
    """Convert axis-angle rows to rotation matrices (world to camera)."""
    _, np = modules()
    vectors = np.atleast_2d(vectors)
    theta = np.linalg.norm(vectors, axis=1)
    axis = vectors / np.maximum(theta, EPSILON)[:, None]
    x, y, z = axis.T
    zero = np.zeros_like(x)
    skew = np.stack(
        [
            np.stack([zero, -z, y], axis=1),
            np.stack([z, zero, -x], axis=1),
            np.stack([-y, x, zero], axis=1),
        ],
        axis=1,
    )
    sin, cos = np.sin(theta)[:, None, None], np.cos(theta)[:, None, None]
    return np.eye(3) + sin * skew + (1 - cos) * skew @ skew


def axis_angle(matrix: NDArray[Any]) -> NDArray[Any]:
    """Return the axis-angle vector of one rotation matrix."""
    cv, np = modules()
    return cv.Rodrigues(np.asarray(matrix, dtype=float))[0].ravel()


def orthonormal(matrix: NDArray[Any]) -> NDArray[Any]:
    """Project a near-rotation onto SO(3)."""
    _, np = modules()
    u, _, vt = np.linalg.svd(matrix)
    result = u @ vt
    return -result if np.linalg.det(result) < 0 else result


def centring(aspect: float) -> NDArray[Any]:
    """Map normalized image coordinates to square, centred image-width units."""
    _, np = modules()
    return np.array([[1, 0, -0.5], [0, aspect, -0.5 * aspect], [0, 0, 1.0]])


def project(
    world: NDArray[Any], rotation: NDArray[Any], focal: float, centre: NDArray[Any]
) -> tuple[NDArray[Any], NDArray[Any]]:
    """Project floor points (metres) into centred image units, with their depth."""
    _, np = modules()
    points = np.c_[world, np.zeros(len(world))] - centre
    camera = points @ rotation.T
    depth = camera[:, 2]
    safe = np.where(np.abs(depth) < EPSILON, EPSILON, depth)
    return focal * camera[:, :2] / safe[:, None], depth


def floor_matrix(
    rotation: NDArray[Any], focal: float, centre: NDArray[Any], aspect: float
) -> NDArray[Any]:
    """Build the normalized-image to court-metre homography for one camera pose."""
    _, np = modules()
    court = np.c_[rotation[:, 0], rotation[:, 1], -rotation @ centre]
    image = np.diag([focal, focal, 1.0]) @ court
    floor = np.linalg.inv(np.linalg.inv(centring(aspect)) @ image)
    # Keep the sign: points in front of the camera have a positive third coordinate.
    return floor / np.linalg.norm(floor[2])


def relative(homography: NDArray[Any], focal: float) -> tuple | None:
    """Split a rotation homography into a relative rotation and the new focal."""
    _, np = modules()
    m = homography @ np.diag([focal, focal, 1.0])
    product = m @ m.T
    if product[2, 2] <= EPSILON:
        return None
    squared = (product[0, 0] + product[1, 1]) / (2 * product[2, 2])
    if not MIN_FOCAL**2 <= squared <= MAX_FOCAL**2:
        return None
    new = math.sqrt(squared)
    return orthonormal(np.diag([1 / new, 1 / new, 1.0]) @ m), new


def features(image: NDArray[Any], boxes: list) -> dict:
    """Describe the whole view except people, balls and broadcast graphics."""
    cv, np = modules()
    gray = cv.cvtColor(
        cv.resize(image, (WIDTH, HEIGHT), interpolation=cv.INTER_AREA),
        cv.COLOR_BGR2GRAY,
    )
    mask = np.full(gray.shape, 255, dtype=np.uint8)
    for x, y, w, h in boxes:
        cv.rectangle(
            mask,
            (int(x * WIDTH) - BOX_MARGIN, int(y * HEIGHT) - BOX_MARGIN),
            (int((x + w) * WIDTH) + BOX_MARGIN, int((y + h) * HEIGHT) + BOX_MARGIN),
            0,
            -1,
        )
    geometry.exclude_overlays(mask)
    detector = cv.ORB_create(nfeatures=FEATURES, fastThreshold=10)
    keys, descriptors = detector.detectAndCompute(gray, mask)
    aspect = image.shape[0] / image.shape[1]
    normalized = np.float64([k.pt for k in keys]).reshape(-1, 2) / [WIDTH, HEIGHT]
    points = np.c_[normalized, np.ones(len(normalized))] @ centring(aspect).T
    return {"points": points[:, :2], "descriptors": descriptors, "aspect": aspect}


def ratio_matches(a: dict, b: dict) -> tuple | None:
    """Nearest-neighbour descriptor matches passing the ratio test."""
    cv, _ = modules()
    if (
        a["descriptors"] is None
        or b["descriptors"] is None
        or len(a["descriptors"]) < MIN_INLIERS
        or len(b["descriptors"]) < MIN_INLIERS
    ):
        return None
    matcher = cv.BFMatcher(cv.NORM_HAMMING)
    good = [
        pair[0]
        for pair in matcher.knnMatch(a["descriptors"], b["descriptors"], k=2)
        if len(pair) == NEIGHBOURS and pair[0].distance < RATIO * pair[1].distance
    ]
    if len(good) < MIN_INLIERS:
        return None
    return (
        a["points"][[m.queryIdx for m in good]],
        b["points"][[m.trainIdx for m in good]],
    )


def cell(points: NDArray[Any], aspect: float) -> NDArray[Any]:
    """Overlay-grid cell index of centred points."""
    _, np = modules()
    x = np.clip(
        ((points[:, 0] + 0.5) * OVERLAY_COLUMNS).astype(int), 0, OVERLAY_COLUMNS - 1
    )
    y = np.clip(
        ((points[:, 1] / aspect + 0.5) * OVERLAY_ROWS).astype(int), 0, OVERLAY_ROWS - 1
    )
    return y * OVERLAY_COLUMNS + x


def without(found: dict, overlay: NDArray[Any] | None) -> dict:
    """Drop features inside learned broadcast-graphic cells."""
    if overlay is None or found["descriptors"] is None or not overlay.any():
        return found
    keep = ~overlay[cell(found["points"], found["aspect"])]
    return {
        **found,
        "points": found["points"][keep],
        "descriptors": found["descriptors"][keep] if keep.any() else None,
    }


def correspond(a: dict, b: dict) -> tuple | None:
    """Return distributed RANSAC-supported correspondences from view a to view b."""
    cv, np = modules()
    matched = ratio_matches(a, b)
    if matched is None:
        return None
    p, q = matched
    homography, inliers = cv.findHomography(
        p * WIDTH, q * WIDTH, cv.RANSAC, RANSAC_PIXELS
    )
    if homography is None or inliers is None:
        return None
    keep = inliers.ravel().astype(bool)
    if keep.sum() < MIN_INLIERS or keep.mean() < MIN_SUPPORT:
        return None
    for points in (p[keep], q[keep]):
        if (
            np.ptp(points[:, 0]) < MIN_SPREAD_X
            or np.ptp(points[:, 1]) < MIN_SPREAD_Y * a["aspect"]
        ):
            return None
    scale = np.diag([WIDTH, WIDTH, 1.0])
    return p[keep], q[keep], np.linalg.inv(scale) @ homography @ scale


def rays(points: NDArray[Any], rotation: NDArray[Any], focal: float) -> NDArray[Any]:
    """Back-project centred image points to world-frame viewing directions."""
    _, np = modules()
    return np.c_[points / focal, np.ones(len(points))] @ rotation


def pose(
    directions: NDArray[Any],
    points: NDArray[Any],
    rotation: NDArray[Any],
    focal: float,
) -> tuple | None:
    """Refine one frame's rotation and focal from known world directions.

    Returns the pose and its median reprojection error in WIDTH pixels.
    """
    _, np = modules()
    optimize = importlib.import_module("scipy.optimize")

    def residual(x: NDArray[Any]) -> NDArray[Any]:
        camera = directions @ rotations(x[:3])[0].T
        depth = np.where(camera[:, 2] > EPSILON, camera[:, 2], EPSILON)
        image = math.exp(x[3]) * camera[:, :2] / depth[:, None]
        return ((image - points) * WIDTH).ravel()

    start = np.r_[axis_angle(rotation), math.log(focal)]
    result = optimize.least_squares(
        residual, start, loss="soft_l1", f_scale=1.0, max_nfev=40
    )
    solved, new = rotations(result.x[:3])[0], math.exp(result.x[3])
    camera = directions @ solved.T
    if (camera[:, 2] <= 0).any() or not MIN_FOCAL <= new <= MAX_FOCAL:
        return None
    error = np.median(np.linalg.norm(residual(result.x).reshape(-1, 2), axis=1))
    return solved, new, float(error)


def track(reference: dict, current: dict) -> tuple | None:
    """Locate the current view from one posed reference view."""
    match = correspond(reference["features"], current)
    if match is None:
        return None
    p, q, homography = match
    start = relative(homography, reference["focal"])
    if start is None:
        return None
    rotation = start[0] @ reference["rotation"]
    solved = pose(
        rays(p, reference["rotation"], reference["focal"]), q, rotation, start[1]
    )
    if solved is None or solved[2] > MAX_ERROR_PIXELS:
        return None
    return solved, len(p)


def segments(image: NDArray[Any], people: list, aspect: float) -> list:
    """Long straight segments outside people and broadcast graphics."""
    cv, _ = modules()
    gray = cv.cvtColor(
        cv.resize(image, (WIDTH, HEIGHT), interpolation=cv.INTER_AREA),
        cv.COLOR_BGR2GRAY,
    )
    detected = cv.createLineSegmentDetector().detect(gray)[0]
    if detected is None:
        return []
    found = []
    for x1, y1, x2, y2 in detected.reshape(-1, 4):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < MIN_SEGMENT_PIXELS:
            continue
        mx, my = (x1 + x2) / 2 / WIDTH, (y1 + y2) / 2 / HEIGHT
        if not OVERLAY_TOP < my < OVERLAY_BOTTOM or any(
            x <= mx <= x + w and y <= my <= y + h for x, y, w, h in people
        ):
            continue
        found.append((
            length,
            [
                [x1 / WIDTH - 0.5, (y1 / HEIGHT - 0.5) * aspect],
                [x2 / WIDTH - 0.5, (y2 / HEIGHT - 0.5) * aspect],
            ],
        ))
    found.sort(key=itemgetter(0), reverse=True)
    return [segment for _, segment in found[:MAX_SEGMENTS]]


def spread(count: int, samples: int) -> list[float]:
    """Evenly spaced positions across a sequence."""
    if count <= samples:
        return [float(i) for i in range(count)]
    return [i * (count - 1) / (samples - 1) for i in range(samples)]


def rotation_residual(homography: NDArray[Any]) -> float:
    """How far a view-to-view homography is from any rotation with zoom.

    Two views of one fixed camera differ by K2 R K1^-1. Views of a distant wall
    from two nearby cameras can match well, but they are not exactly such a
    rotation, so this separates cameras that feature counts alone cannot.
    """
    _, np = modules()
    focal = np.exp(np.linspace(math.log(0.3), math.log(12), 40))[:, None]
    zoom = np.exp(np.linspace(math.log(0.5), math.log(2), 21))[None, :]
    f1, f2 = (focal * np.ones_like(zoom)).ravel(), (focal * zoom).ravel()
    k1 = np.zeros((len(f1), 3, 3))
    k1[:, 0, 0], k1[:, 1, 1], k1[:, 2, 2] = f1, f1, 1
    inverse = np.zeros_like(k1)
    inverse[:, 0, 0], inverse[:, 1, 1], inverse[:, 2, 2] = 1 / f2, 1 / f2, 1
    m = inverse @ homography @ k1
    determinant = np.linalg.det(m)
    usable = np.abs(determinant) > EPSILON
    m = m[usable] / np.cbrt(determinant[usable])[:, None, None]
    errors = np.linalg.norm(m @ np.transpose(m, (0, 2, 1)) - np.eye(3), axis=(1, 2))
    return float(errors.min()) if len(errors) else math.inf


def rotational(p: NDArray[Any], q: NDArray[Any], homography: NDArray[Any]) -> bool:
    """Accept a view link only if its matches fit one rotating, zooming camera.

    Most links pass the cheap homography check. Borderline ones are fitted
    directly: repeated seats or a far wall seen from a second camera can give
    plenty of matches that still miss a pure rotation by pixels.
    """
    _, np = modules()
    residual = rotation_residual(homography)
    if residual <= CLEAR_ROTATION_RESIDUAL:
        return True
    if residual > MAX_ROTATION_RESIDUAL:
        return False
    for focal in LINK_FOCALS:
        start = relative(homography, focal)
        if start is None:
            continue
        solved = pose(rays(p, np.eye(3), focal), q, start[0], start[1])
        if solved is not None and solved[2] <= MAX_LINK_PIXELS:
            return True
    return False


def rotation_focal(edges: list[dict]) -> float:
    """Pick the focal that makes pairwise homographies closest to rotations."""
    _, np = modules()
    best, focal = math.inf, DEFAULT_FOCAL
    for candidate in np.exp(np.linspace(math.log(0.3), math.log(12), 60)):
        k = np.diag([candidate, candidate, 1.0])
        inverse = np.diag([1 / candidate, 1 / candidate, 1.0])
        errors = []
        for edge in edges:
            m = inverse @ edge["homography"] @ k
            determinant = np.linalg.det(m)
            if abs(determinant) < EPSILON:
                continue
            m /= np.cbrt(determinant)
            errors.append(float(np.linalg.norm(m @ m.T - np.eye(3))))
        if errors and np.median(errors) < best:
            best, focal = float(np.median(errors)), float(candidate)
    return focal


def standing(people: list) -> list[tuple]:
    """Foot and head points of whole, separated people in centred units."""
    found = []
    for n, (x, y, w, h) in enumerate(people):
        bottom = y + h
        if h < MIN_PERSON_HEIGHT or w <= 0:
            continue
        # Feet hidden behind another person are not floor contacts.
        if any(
            m != n and ox < x + w / 2 < ox + ow and oy < bottom < oy + oh
            for m, (ox, oy, ow, oh) in enumerate(people)
        ):
            continue
        found.append(([x + w / 2, bottom], [x + w / 2, y]))
    return found


def heights(
    rotation: NDArray[Any],
    focal: NDArray[Any],
    centre: NDArray[Any],
    feet: NDArray[Any],
    heads: NDArray[Any],
) -> NDArray[Any]:
    """Height above the floor of each head, standing on its projected foot point."""
    _, np = modules()
    foot = np.einsum(
        "ni,nij->nj", np.c_[feet / focal[:, None], np.ones(len(feet))], rotation
    )
    head = np.einsum(
        "ni,nij->nj", np.c_[heads / focal[:, None], np.ones(len(heads))], rotation
    )
    reach = -centre[2] / np.maximum(foot[:, 2], EPSILON)
    offset = reach[:, None] * foot
    # Closest point between the head ray and the vertical through the foot.
    b = -head[:, 2]
    c = np.einsum("ni,ni->n", head, head)
    d = -offset[:, 2]
    e = np.einsum("ni,ni->n", head, offset)
    height = (b * e - c * d) / np.maximum(c - b * b, EPSILON)
    # Feet near the horizon have no stable floor point; they carry no evidence.
    # Seen steeply from above, a box's height also includes the body's depth.
    depression = foot[:, 2] / np.linalg.norm(foot, axis=1)
    unusable = (depression < MIN_FOOT_DEPRESSION) | (depression > MAX_FOOT_DEPRESSION)
    return np.where(unusable, PLAYER_HEIGHT, np.clip(height, 0, MAX_PERSON_HEIGHT))
