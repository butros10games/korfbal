"""Disk-backed recording identities with bounded raw appearance galleries.

Sections own non-overlapping source-time ranges. Only finalized immutable chunks
may be committed here. Replays are video intervals, not duplicated live time.
Old observations remain on disk; a new section loads bounded identity galleries,
refits a common space, and writes a new alias revision in one transaction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
import importlib
import json
import math
from operator import itemgetter
from pathlib import Path
import sqlite3

from .clip_match_evidence import fit_fragments
from .clip_match_identity import (
    MAX_FRAGMENTS,
    MAX_GALLERY_SAMPLES,
    Anchor,
    Calibration,
    Fragment,
    Settings,
    associate,
)


MAX_PLAYERS = 64
MAX_VIEW_SAMPLES = 6
VIEW_AXES = 2
MAX_DESCRIPTOR_DIMENSIONS = 8192
RETAIN_OLD_VIEWS = 3
DESCRIPTOR_RECIPE = "dinov2_vits14:clip-discriminant:v1"


class RecordingGallery:
    """One owner-scoped recording database; callers publish section overlays."""

    def __init__(
        self, path: Path, namespace: str, *, descriptor_recipe: str = DESCRIPTOR_RECIPE
    ) -> None:
        """Create the local gallery schema without accessing production state.

        Raises:
            ValueError: The database is owned by another recording or descriptor recipe.

        """
        self.path, self.namespace = path, namespace
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recording (
                    id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL,
                    descriptor_recipe TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sections (
                    id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY, name TEXT, team TEXT NOT NULL,
                    number TEXT, confidence REAL NOT NULL, display TEXT
                );
                CREATE TABLE IF NOT EXISTS samples (
                    player TEXT NOT NULL, seed TEXT NOT NULL,
                    count INTEGER NOT NULL, dimensions INTEGER NOT NULL,
                    data BLOB NOT NULL, PRIMARY KEY(player, seed)
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO recording VALUES (1, ?, ?)",
                (namespace, descriptor_recipe),
            )
            if db.execute(
                "SELECT namespace, descriptor_recipe FROM recording WHERE id=1"
            ).fetchone() != (namespace, descriptor_recipe):
                raise ValueError("Gallery belongs to a different recording or recipe")

    def connect(self) -> sqlite3.Connection:
        """Open a bounded transaction; concurrent section commits are serialized."""
        return sqlite3.connect(self.path, timeout=30.0)

    def load(self, db: sqlite3.Connection) -> tuple[list[Fragment], list[Anchor]]:
        """Load at most 64 identities x 6 views x 6 float16 descriptors."""
        np = importlib.import_module("numpy")
        pieces, anchors = [], []
        for identity, name, team, number, confidence, display in db.execute(
            "SELECT id, name, team, number, confidence, display "
            "FROM players ORDER BY id LIMIT ?",
            (MAX_PLAYERS,),
        ):
            piece = Fragment(identity, "gallery", team, set())
            for seed, count, dimensions, data in db.execute(
                "SELECT seed, count, dimensions, data FROM samples "
                "WHERE player=? ORDER BY seed LIMIT ?",
                (identity, MAX_GALLERY_SAMPLES),
            ):
                piece.raw[seed] = (
                    np
                    .frombuffer(data, dtype=np.float16)
                    .reshape(count, dimensions)
                    .copy()
                )
            pieces.append(piece)
            # Gallery identities cannot collapse into another established player.
            anchors.append(
                Anchor(
                    identity,
                    name or identity.removeprefix(f"{self.namespace}:player:"),
                    team,
                    number,
                    "recording_gallery",
                    confidence,
                    display,
                )
            )
        return pieces, anchors

    def process(
        self,
        section: dict,
        pieces: list[Fragment],
        anchors: list[Anchor] | None = None,
        *,
        calibration: Calibration | None = None,
        stopped: Callable[[], bool] | None = None,
    ) -> dict:
        """Commit only complete, fingerprinted, non-overlapping owned sections.

        Raises:
            ValueError: A section is inconsistent or exceeds gallery capacity.

        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT fingerprint, result FROM sections WHERE id=?", (section["id"],)
            ).fetchone()
            if prior:
                if prior[0] != section["fingerprint"]:
                    raise ValueError(
                        "Section fingerprint changed; create a new gallery revision"
                    )
                return json.loads(prior[1])
            self.validate_section(db, section, pieces)
            gallery, carried = self.load(db)
            scoped = {f"{section['id']}:{p.identity}": p.identity for p in pieces}
            current = [
                replace(p, identity=f"{section['id']}:{p.identity}") for p in pieces
            ]
            local_ids = {p.identity for p in pieces}
            confirmed = [
                replace(a, identity=f"{section['id']}:{a.identity}")
                if a.identity in local_ids
                else a
                for a in (anchors or [])
            ]
            combined = gallery + current
            validate_views(combined)
            fit_fragments(combined)
            result = associate(
                combined,
                anchors=[*carried, *confirmed],
                calibration=calibration,
                settings=Settings(namespace=self.namespace),
                stopped=stopped,
            )
            if result["status"] != "completed":
                return result
            if stopped and stopped():
                return {**result, "status": "interrupted", "assignments": []}
            self.save(db, combined, result, [*carried, *confirmed])
            # Each immutable section owns its aliases. Earlier source ranges are
            # not rewritten when a new section arrives or a player is substituted.
            output = {
                **result,
                "assignments": [
                    {**a, "identity": scoped[a["identity"]]}
                    for a in result["assignments"]
                    if a["identity"] in scoped
                ],
                "section_id": section["id"],
                "recording_id": self.namespace,
            }
            db.execute(
                "INSERT INTO sections VALUES (?, ?, ?, ?, ?)",
                (
                    section["id"],
                    section["start"],
                    section["end"],
                    section["fingerprint"],
                    json.dumps(output),
                ),
            )
            return output

    def validate_section(
        self, db: sqlite3.Connection, section: dict, pieces: list[Fragment]
    ) -> None:
        """Enforce source-time ownership so section boundary context is used once.

        Raises:
            ValueError: Owned source ranges overlap or observations escape the range.

        """
        start, end = section["start"], section["end"]
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start < end
            or not section["fingerprint"]
        ):
            raise ValueError(
                "Section needs a valid source range and immutable fingerprint"
            )
        if db.execute(
            "SELECT 1 FROM sections WHERE start < ? AND end > ? LIMIT 1", (end, start)
        ).fetchone():
            raise ValueError("Section source range overlaps an already owned section")
        validate_views(pieces)
        if sum(bool(p.raw) for p in pieces) > MAX_FRAGMENTS:
            raise ValueError("Section appearance fragments exceed the fitting bound")
        if any(
            not math.isfinite(timestamp) or timestamp < start or timestamp >= end
            for piece in pieces
            for timestamp in piece.times
        ):
            raise ValueError(
                "Section observations must use recording source timestamps"
            )

    def save(
        self,
        db: sqlite3.Connection,
        pieces: list[Fragment],
        result: dict,
        anchors: list[Anchor],
    ) -> None:
        """Store fixed-size pure views; assigned IDs retain their original namespace.

        Raises:
            ValueError: A recording has more accepted players than the memory bound.

        """
        verified = {anchor.identity: anchor for anchor in anchors}
        by_id = {piece.identity: piece for piece in pieces}
        groups: dict[str, list[Fragment]] = defaultdict(list)
        assignments = {a["identity"]: a for a in result["assignments"]}
        for identity, assignment in assignments.items():
            if assignment["player_id"]:
                groups[assignment["player_id"]].append(by_id[identity])
        if len(groups) > MAX_PLAYERS:
            raise ValueError("Recording identity gallery capacity exceeded")
        for identity, members in groups.items():
            named = next(
                (verified[p.identity] for p in members if p.identity in verified), None
            )
            team = named.team if named else members[0].team
            confidence = min(assignments[p.identity]["confidence"] for p in members)
            display = assignments[members[0].identity]["display_id"]
            db.execute(
                "INSERT OR REPLACE INTO players VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    named.player_id if named else None,
                    team,
                    named.number if named else None,
                    confidence,
                    display,
                ),
            )
            # Keep old views plus new independent tracklet classes, bounded by
            # class count rather than averaging different people or camera views.
            kept = retain_views(members)
            db.execute("DELETE FROM samples WHERE player=?", (identity,))
            for seed, values in kept:
                raw = values.astype("float16")
                db.execute(
                    "INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
                    (identity, seed, len(raw), raw.shape[1], raw.tobytes()),
                )


def validate_views(pieces: list[Fragment]) -> None:
    """Reject unbounded or incompatible raw input before fitting any matrices.

    Raises:
        ValueError: A gallery view exceeds the documented memory contract.

    """
    dimensions = set()
    for piece in pieces:
        if len(piece.raw) > MAX_GALLERY_SAMPLES:
            raise ValueError("Too many raw gallery views")
        for values in piece.raw.values():
            if (
                values.ndim != VIEW_AXES
                or not 1 <= len(values) <= MAX_VIEW_SAMPLES
                or not 1 <= values.shape[1] <= MAX_DESCRIPTOR_DIMENSIONS
            ):
                raise ValueError("Raw gallery view exceeds the bounded sample shape")
            dimensions.add(values.shape[1])
    if len(dimensions) > 1:
        raise ValueError("Gallery views must share one descriptor recipe")


def retain_views(members: list[Fragment]) -> list[tuple]:
    """Retain early anchors and refresh half the pure gallery with current views."""
    previous = {
        seed: values
        for p in members
        if p.shot == "gallery"
        for seed, values in p.raw.items()
    }
    current = {
        f"{p.identity}:{seed}": values
        for p in members
        if p.shot != "gallery"
        for seed, values in p.raw.items()
    }
    old = sorted(previous.items(), key=itemgetter(0))
    new = list(current.items())
    if not new:
        return old[:MAX_GALLERY_SAMPLES]
    kept = old[:RETAIN_OLD_VIEWS]
    kept.extend(new[: MAX_GALLERY_SAMPLES - len(kept)])
    kept.extend(
        old[RETAIN_OLD_VIEWS : MAX_GALLERY_SAMPLES - len(kept) + RETAIN_OLD_VIEWS]
    )
    return kept
