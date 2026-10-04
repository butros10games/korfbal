"""Fit match appearance on pure tracklets and adapt the existing replay contract."""

from __future__ import annotations

from collections import Counter, defaultdict
import importlib
from typing import TYPE_CHECKING, Any

from . import clip_discriminant
from .clip_linking import overlaps
from .clip_match_identity import MAX_GALLERY_SAMPLES, Fragment


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_linking import Linker

MIN_SAMPLES = 3
MIN_CLASSES = 4
MIN_CLASS_SAMPLES = 6
SAME_BODY_IOU = 0.65
DISTINCT_BODIES = 2
DIMENSIONS = 32
SHRINKAGE = 0.3
EPSILON = 1e-9


def fit(
    values: NDArray[Any], labels: NDArray[Any], *, dimensions: int = DIMENSIONS
) -> tuple[NDArray[Any], NDArray[Any]] | None:
    """Fit the linker discriminant recipe on pure-tracklet classes, never names."""
    np = importlib.import_module("numpy")
    classes = [
        label
        for label, count in Counter(labels.tolist()).items()
        if label >= 0 and count >= MIN_CLASS_SAMPLES
    ]
    if len(classes) < MIN_CLASSES:
        return None
    centre = values.mean(axis=0)
    values = values.copy()
    values -= centre
    if clip_discriminant.gpu():
        return centre, clip_discriminant.transform(
            values,
            labels,
            classes,
            clip_discriminant.Fit(dimensions, SHRINKAGE, EPSILON),
        )
    within = np.zeros((values.shape[1],) * 2)
    centres = []
    for label in classes:
        samples = values[labels == label]
        mean = samples.mean(axis=0)
        centres.append(mean)
        within += (samples - mean).T @ (samples - mean)
    within /= sum(int((labels == label).sum()) for label in classes)
    within = (1 - SHRINKAGE) * within + SHRINKAGE * np.trace(within) / len(
        within
    ) * np.eye(len(within))
    weights, vectors = np.linalg.eigh(within)
    whiten = vectors @ np.diag(np.maximum(weights, EPSILON) ** -0.5) @ vectors.T
    between = np.array(centres) @ whiten
    _, _, axes = np.linalg.svd(between - between.mean(axis=0), full_matrices=False)
    return centre, whiten @ axes[:dimensions].T


def project(
    values: NDArray[Any], space: tuple[NDArray[Any], NDArray[Any]]
) -> NDArray[Any]:
    """Project samples into one common fitted space and normalise per sample."""
    np = importlib.import_module("numpy")
    centre, transform = space
    projected = (values - centre) @ transform
    return projected / np.maximum(
        np.linalg.norm(projected, axis=1, keepdims=True), EPSILON
    )


def resolved(linker: Linker, report: dict) -> dict[int, tuple[str, str]]:
    """Resolve whole-track and frame aliases with referee/supersession precedence."""
    aliases = {link["from_track_id"]: link for link in report.get("links", [])}
    frames = {
        (link["time_seconds"], link["from_track_id"]): link
        for link in report.get("frame_links", [])
    }
    superseded = {
        (link["time_seconds"], link["superseded_track_id"])
        for link in report.get("frame_links", [])
        if link.get("superseded_track_id")
    }
    output = {}
    for index, row in enumerate(linker.rows):
        key = round(row.time, 6), row.track_id
        if row.referee or key in superseded:
            continue
        link = frames.get(key, aliases.get(row.track_id, {}))
        if link.get("label") == "referee":
            continue
        team = link.get("team") or (
            "unknown" if row.team is None else ("team_a", "team_b")[row.team]
        )
        output[index] = link.get("to_track_id", row.track_id), team
    return output


def chosen(linker: Linker) -> list[Any]:
    """Return the descriptors this clip was linked with (broadcast or default)."""
    return (
        linker.alternates if getattr(linker, "broadcast", False) else linker.descriptors
    )


def fragments(
    linker: Linker,
    report: dict,
    *,
    replay_shots: set[int] | None = None,
    dimensions: int = DIMENSIONS,
) -> list[Fragment]:
    """Describe shot identities through links + frame_links, fitting only pure seeds.

    Motion, provisional court estimates and uncalibrated shirt numbers are not
    cross-cut evidence. Sparse fragments without adequate descriptors stay private.
    """
    np = importlib.import_module("numpy")
    mapping = resolved(linker, report)
    owners = [i for i, row in enumerate(linker.rows) if row.descriptor >= 0]
    if not owners:
        return describe(linker, mapping, {}, replay_shots or set())
    tracks = linker.tracklets()
    labels = np.full(len(linker.rows), -1)
    for label, track in enumerate(tracks):
        labels[track] = label
    raw: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index in owners:
        if index in mapping and labels[index] >= 0:
            raw[mapping[index][0]][str(int(labels[index]))].append(index)
    pieces = describe(linker, mapping, {}, replay_shots or set())
    for piece in pieces:
        views = sorted(
            raw[piece.identity].items(), key=lambda item: len(item[1]), reverse=True
        )[:MAX_GALLERY_SAMPLES]
        for seed, indices in views:
            if len(indices) >= MIN_CLASS_SAMPLES:
                selected = np.linspace(0, len(indices) - 1, MIN_CLASS_SAMPLES).astype(
                    int
                )
                piece.raw[seed] = np.stack([
                    chosen(linker)[linker.rows[indices[int(i)]].descriptor]
                    for i in selected
                ]).astype(np.float16)
    fit_fragments(pieces, dimensions=dimensions)
    return pieces


def fit_fragments(pieces: list[Fragment], *, dimensions: int = DIMENSIONS) -> None:
    """Refit one common space from bounded pure raw galleries across sections."""
    np = importlib.import_module("numpy")
    samples, labels = [], []
    owners: list[tuple[Fragment, str, int]] = []
    for piece in pieces:
        for seed, values in piece.raw.items():
            label = len(owners)
            owners.append((piece, seed, len(values)))
            samples.extend(values)
            labels.extend([label] * len(values))
    if not samples:
        return
    values = np.asarray(samples, dtype=np.float64)
    space = fit(values, np.asarray(labels), dimensions=dimensions)
    if space is None:
        return
    embedded = project(values, space)
    for piece in pieces:
        piece.vectors = []
        piece.samples = []
    offset = 0
    for piece, _, count in owners:
        piece.samples.extend(embedded[offset : offset + count])
        mean = embedded[offset : offset + count].mean(axis=0)
        piece.vectors.append(mean / max(float(np.linalg.norm(mean)), EPSILON))
        offset += count


def describe(
    linker: Linker,
    mapping: dict[int, tuple[str, str]],
    galleries: dict[str, list[Any]],
    replays: set[int],
) -> list[Fragment]:
    """Retain occupancy for all identities; short fragments cannot gain visual joins."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, (identity, _) in mapping.items():
        grouped[identity].append(index)
    output = []
    for identity, indices in sorted(grouped.items()):
        votes = Counter(
            mapping[i][1] for i in indices if mapping[i][1] in {"team_a", "team_b"}
        )
        team, count = votes.most_common(1)[0] if votes else ("unknown", 0)
        if count < MIN_SAMPLES or count < 0.7 * sum(votes.values()):
            team = "unknown"
        shots = {linker.segments[linker.rows[i].frame] for i in indices}
        live = [
            i for i in indices if linker.segments[linker.rows[i].frame] not in replays
        ]
        moments = sorted({round(linker.rows[i].time, 6) for i in live})
        representative = (
            min(
                live,
                key=lambda i: abs(linker.rows[i].time - moments[len(moments) // 2]),
            )
            if live
            else indices[0]
        )
        output.append(
            Fragment(
                identity,
                ",".join(map(str, sorted(shots))),
                team,
                {
                    round(linker.rows[i].time, 6)
                    for i in indices
                    if linker.segments[linker.rows[i].frame] not in replays
                },
                galleries.get(identity, []),
                replay=shots.issubset(replays),
                weight=len(indices),
                conflicted=body_conflict(linker, indices, replays),
                representative_track_id=linker.rows[representative].track_id,
            )
        )
    return output


def body_conflict(linker: Linker, indices: list[int], replays: set[int]) -> bool:
    """Check whether one upstream identity already contains two live bodies."""
    np = importlib.import_module("numpy")
    frames: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        frame = linker.rows[index].frame
        if linker.segments[frame] not in replays:
            frames[frame].append(index)
    for members in frames.values():
        if len(members) < DISTINCT_BODIES:
            continue
        boxes = np.array([linker.rows[i].box for i in members])
        scores = overlaps(boxes, boxes)
        if np.any(scores[np.triu_indices(len(members), 1)] < SAME_BODY_IOU):
            return True
    return False


def pure_fragments(
    linker: Linker, report: dict, *, replay_shots: set[int] | None = None
) -> tuple[list[Fragment], dict[int, str]]:
    """Bypass upstream joins while preserving referee and supersession decisions."""
    np = importlib.import_module("numpy")
    visible = resolved(linker, report)
    mapping = {}
    seeds = {}
    for label, track in enumerate(linker.tracklets()):
        identity = f"tracklet:{label}"
        seeds[identity] = [i for i in track if i in visible]
        for index in seeds[identity]:
            mapping[index] = identity, visible[index][1]
    pieces = describe(linker, mapping, {}, replay_shots or set())
    for piece in pieces:
        indices = [i for i in seeds[piece.identity] if linker.rows[i].descriptor >= 0]
        if indices:
            selected = np.linspace(0, len(indices) - 1, min(24, len(indices))).astype(
                int
            )
            piece.raw[piece.identity] = np.stack([
                chosen(linker)[linker.rows[indices[int(i)]].descriptor]
                for i in selected
            ]).astype(np.float16)
    fit_fragments(pieces, dimensions=128)
    return pieces, {index: identity for index, (identity, _) in mapping.items()}


def compose_tracklets(
    linker: Linker,
    report: dict,
    result: dict,
    ownership: dict[int, str],
    *,
    propagate: bool = False,
) -> dict:
    """Override every pure row without baking in wrong joins.

    A named tracklet shows its player. An unnamed one keeps the identity the
    within-shot linker gave it, so unknown players still follow one track per
    shot instead of breaking into raw tracklets. With ``propagate``, unnamed
    tracklets of a linked identity whose named rows all carry one player take
    that name as an automatic, linker-propagated name.
    """
    if result.get("status") != "completed":
        return {**report, "match_identity": result}
    assignments = {a["identity"]: a for a in result["assignments"]}
    aliases = {link["from_track_id"]: link for link in report.get("links", [])}
    frames = {
        (f["time_seconds"], f["from_track_id"]): dict(f)
        for f in report.get("frame_links", [])
    }
    upstream: dict[int, dict] = {}
    for index in ownership:
        row = linker.rows[index]
        key = round(row.time, 6), row.track_id
        upstream[index] = frames.get(key) or aliases.get(row.track_id) or {}
    names = linked_names(ownership, upstream, assignments) if propagate else {}
    # A player is one body per moment: never lend a name where it already shows.
    shown = {
        (round(linker.rows[i].time, 6), assignments[f]["player_id"])
        for i, f in ownership.items()
        if assignments[f]["player_id"]
    }
    for index, identity in ownership.items():
        row = linker.rows[index]
        key = round(row.time, 6), row.track_id
        assignment = assignments[identity]
        before = upstream[index]
        unnamed = before.get("to_track_id", identity), before.get("display_id") or "?"
        player, display, origin = (
            assignment["player_id"],
            assignment["display_id"],
            assignment.get("origin"),
        )
        source = "closed_set_tracklet"
        if (
            player is None
            and unnamed[0] in names
            and (key[0], names[unnamed[0]][0]) not in shown
        ):
            player, display = names[unnamed[0]]
            origin, source = "automatic", "linked_propagation"
            shown.add((key[0], player))
        frames[key] = {
            **frames.get(key, {}),
            "time_seconds": key[0],
            "from_track_id": row.track_id,
            "to_track_id": player or unnamed[0],
            "display_id": display or unnamed[1],
            "source": source,
            "fragment_identity": identity,
            "name_origin": origin if player else None,
            "unnamed_track_id": unnamed[0],
            "unnamed_display_id": unnamed[1],
        }
    return {**report, "frame_links": list(frames.values()), "match_identity": result}


def linked_names(
    ownership: dict[int, str],
    upstream: dict[int, dict],
    assignments: dict[str, dict],
) -> dict[str, tuple[str, str]]:
    """Linked identities whose named rows agree on exactly one player."""
    seen: dict[str, Counter] = defaultdict(Counter)
    displays: dict[str, str] = {}
    for index, identity in ownership.items():
        linked = upstream[index].get("to_track_id")
        assignment = assignments[identity]
        if linked is None:
            continue
        seen[linked][assignment["player_id"]] += 1
        if assignment["player_id"]:
            displays[assignment["player_id"]] = assignment["display_id"]
    output = {}
    for linked, counts in seen.items():
        named = {k for k in counts if k is not None}
        if len(named) == 1:
            player = next(iter(named))
            output[linked] = player, displays[player]
    return output
