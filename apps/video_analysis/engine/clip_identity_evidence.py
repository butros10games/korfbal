"""Let independently visible shirt patches support identity through box overlaps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .clip_clothing import sample


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_identity import IdentityMemory


def samples(
    memory: IdentityMemory, image: NDArray[Any], objects: list[dict]
) -> tuple[list, list]:
    """Keep overlapping jerseys out of history unless masked evidence agrees."""
    colors, reliable = [], []
    memory.visible_shirts = set()
    for obj in objects:
        color = memory.shirts.observe(image, obj["observed_bbox"])
        supported = memory.clear_torso(obj, objects)
        if not supported:
            masked, evidence = sample(
                memory.shirts,
                image,
                obj["observed_bbox"],
                [other["observed_bbox"] for other in objects if other is not obj],
            )
            if evidence == "visible" and memory.shirts.vote(masked) is not None:
                color, supported = masked, True
                memory.visible_shirts.add(obj["track_id"])
        colors.append(color)
        reliable.append(color if supported else None)
    return colors, reliable
