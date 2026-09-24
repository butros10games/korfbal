"""Recover a native ID stranded on a contained fragment of another player."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .clip_identity import IdentityMemory

MIN_HISTORY = 3
MAX_GAP = 0.32
MAX_COST = 0.75
MIN_MARGIN = 0.3
MIN_IMPROVEMENT = 0.5
MAX_HEIGHT_RATIO = 0.9
MIN_CONTAINMENT = 0.9


def contained(box: list, other: list) -> bool:
    """Require almost the entire small observation inside another body box."""
    x, y, w, h = box
    a, b, c, d = other
    overlap = max(0, min(x + w, a + c) - max(x, a)) * max(
        0, min(y + h, b + d) - max(y, b)
    )
    return overlap / max(1e-9, w * h) >= MIN_CONTAINMENT


def recover(
    memory: IdentityMemory,
    matched: dict,
    samples: list,
    *,
    time: float,
    key: object,
) -> None:
    """Challenge a truncated continuation only with a unique separate return."""
    objects = [obj for obj, _ in samples]
    colors = [color for _, color in samples]
    for index, identity in list(matched.items()):
        prior = memory.tracks[identity]
        obj = objects[index]
        if (
            obj["label"] != "player"
            or len(prior["colors"]) < MIN_HISTORY
            or time - prior["time"] > MAX_GAP
            or obj["observed_bbox"][3] >= prior["height"] * MAX_HEIGHT_RATIO
            or not any(
                j != index
                and other["label"] == "player"
                and j in matched
                and len(memory.tracks[matched[j]]["colors"]) >= MIN_HISTORY
                and contained(obj["observed_bbox"], other["observed_bbox"])
                for j, other in enumerate(objects)
            )
        ):
            continue
        ranked = sorted(
            (
                min(
                    memory.cost(prior, candidate, colors[j], time, ref)
                    for ref in dict.fromkeys((None, key))
                ),
                j,
            )
            for j, candidate in enumerate(objects)
            if j not in matched
            and candidate["label"] == "player"
            and memory.possible(prior, candidate, time, key)
        )
        old_cost = min(
            memory.cost(prior, obj, colors[index], time, ref)
            for ref in dict.fromkeys((None, key))
        )
        if (
            not ranked
            or ranked[0][0] >= MAX_COST
            or old_cost - ranked[0][0] < MIN_IMPROVEMENT
        ):
            continue
        cost, target = ranked[0]
        rivals = [
            min(
                memory.cost(other, objects[target], colors[target], time, ref)
                for ref in dict.fromkeys((None, key))
            )
            for name, other in memory.tracks.items()
            if name != identity
        ]
        if any(value - cost < MIN_MARGIN for value in rivals) or any(
            value - cost < MIN_MARGIN for value, _ in ranked[1:]
        ):
            continue
        matched.pop(index)
        matched[target] = identity
        obj["identity_uncertain"] = True
