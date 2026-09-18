"""Associate temporal people with target boxes before refining their geometry."""

from copy import deepcopy
from itertools import product
from operator import itemgetter
from statistics import median

from .store import MAX_OBJECTS, validate_annotation
from .vision import iou


PEOPLE = {"player", "referee"}
MIN_SUPPORT = 2
MIN_CONSISTENCY = 0.65
ASSOCIATION_IOU = 0.3
ASSOCIATION_MARGIN = 0.15
NOVEL_IOU = 0.1
REFINEMENT_WEIGHT = 0.5
BOX_EPSILON = 1e-6


def candidate(
    observations: list[tuple[int, dict]], center: int, confidence: float
) -> dict | None:
    """Interpolate multiple reliable observations onto the target timestamp."""
    supported = [
        (time, obj) for time, obj in observations if obj["confidence"] >= confidence
    ]
    labels = {obj["label"] for _, obj in supported}
    if len(labels) != 1 or not labels <= PEOPLE:
        return None
    before = [(time, obj) for time, obj in supported if time < center]
    after = [(time, obj) for time, obj in supported if time > center]
    if len(before) < MIN_SUPPORT or len(after) < MIN_SUPPORT:
        return None
    projected = []
    for (a_time, a), (b_time, b) in product(before, after):
        ratio = (center - a_time) / (b_time - a_time)
        projected.append([
            x + ratio * (y - x) for x, y in zip(a["bbox"], b["bbox"], strict=True)
        ])
    box = [median(axis) for axis in zip(*projected, strict=True)]
    consistency = median(iou(box, projected_box) for projected_box in projected)
    if consistency < MIN_CONSISTENCY:
        return None
    return {
        "label": next(iter(labels)),
        "bbox": box,
        "confidence": min(
            median(obj["confidence"] for _, obj in before),
            median(obj["confidence"] for _, obj in after),
        ),
        "consistency": consistency,
    }


def unique_best(scores: list[float]) -> int | None:
    """Accept only a sufficiently separated best spatial association."""
    ranked = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    if not ranked or scores[ranked[0]] < ASSOCIATION_IOU:
        return None
    if len(ranked) > 1 and scores[ranked[0]] - scores[ranked[1]] < ASSOCIATION_MARGIN:
        return None
    return ranked[0]


def refine(original: dict, proposal: dict) -> dict:
    """Anchor a motion-consistent estimate to the actual target detection."""
    if original["label"] != proposal["label"]:
        return original
    box = [
        (1 - REFINEMENT_WEIGHT) * a + REFINEMENT_WEIGHT * b
        for a, b in zip(original["bbox"], proposal["bbox"], strict=True)
    ]
    if all(
        abs(a - b) < BOX_EPSILON for a, b in zip(original["bbox"], box, strict=True)
    ):
        return original
    return dict(original, bbox=box, temporal_estimate=True)


def fuse(
    target: dict,
    tracks: list[dict[int, dict]],
    confidence: float,
    center: int,
    *,
    refine_existing: bool = True,
) -> tuple[dict, int]:
    """Refine unambiguous one-to-one matches; add only spatially distinct tracks."""
    result = deepcopy(target)
    people = [
        index for index, obj in enumerate(result["objects"]) if obj["label"] in PEOPLE
    ]
    identities = sorted({identity for step in tracks for identity in step})
    candidates = []
    for identity in identities:
        observations = [
            (time, step[identity])
            for time, step in enumerate(tracks)
            if identity in step
        ]
        proposal = candidate(observations, center, confidence)
        if proposal is not None:
            candidates.append(proposal)
    scores = [
        [iou(proposal["bbox"], result["objects"][index]["bbox"]) for index in people]
        for proposal in candidates
    ]
    best_people = [unique_best(row) for row in scores]
    best_tracks = [
        unique_best([row[index] for row in scores]) for index in range(len(people))
    ]
    for track, person in enumerate(best_people):
        if refine_existing and person is not None and best_tracks[person] == track:
            index = people[person]
            result["objects"][index] = refine(
                result["objects"][index], candidates[track]
            )
    # Suppress candidate duplicates against original boxes as well as refinements.
    occupied = [obj["bbox"] for obj in target["objects"] if obj["label"] in PEOPLE]
    occupied.extend(obj["bbox"] for obj in result["objects"] if obj["label"] in PEOPLE)
    for proposal in sorted(candidates, key=itemgetter("confidence"), reverse=True):
        if len(result["objects"]) >= MAX_OBJECTS:
            break
        if any(iou(proposal["bbox"], box) > NOVEL_IOU for box in occupied):
            continue
        result["objects"].append({
            "label": proposal["label"],
            "bbox": proposal["bbox"],
            "confidence": min(proposal["confidence"], 0.49),
            "temporal_estimate": True,
        })
        occupied.append(proposal["bbox"])
    result = validate_annotation(result)
    return result, sum(bool(obj.get("temporal_estimate")) for obj in result["objects"])
