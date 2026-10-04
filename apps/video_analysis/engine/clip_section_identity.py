"""One match-wide roster identity across the sections of a long recording.

A long recording is analysed as bounded sections (``adapters/replay.py``), each
its own clip run with its own tracker. Roster names are already match-wide
targets: a tracklet named ``alice`` in section 3 is the same ``alice`` as in
section 1. What a later section lacks is evidence for players it never reads a
number for. The parent replay therefore owns one recording gallery: after each
section, the raw descriptors of tracklets named by a confirmation or a shirt
number are added to their player (bounded views), and the next section loads
them as confirmed gallery classes before solving its own roster.

Only anchored names (human or number) enter the gallery; appearance-only
propagations never become evidence for later sections, so one wrong
propagation cannot snowball through a match. The gallery is tied to one
recording and one descriptor recipe, and each section commits once.

Every committed view keeps its origin (a confirmation or an automatic number),
the kit it was seen in and, for a number, its basis: the kit orientation it
was named under. A reviewer's later answer on a committed view wins
everywhere: ``reconcile`` moves the view to the confirmed player or retracts it
(a dismissal, an undo, or a number the answer overruled) and logs a correction,
and later sections load each view as its own carried piece, so ``revised``
drops or renames what an earlier section's reviewer corrected even after that
later section was solved.

The kit orientation is one hypothesis for the whole recording
(``clip_number_anchors``). Each section records its current sightings in the
gallery's ledger (on commit and after every review answer), and every section
decides from its own sightings plus everybody else's (``recorded``). A view
named by number loads only while the recording holds the orientation it was
named under, so a re-opened or flipped orientation retracts it from earlier and
later sections alike; a confirmed view always loads.

Players a section's reviewer adds to the roster ("Speler toevoegen", for an
unlinked recording or a substitute the line-up missed) are the recording's
players: the gallery records them (``add_players``), later sections start with
them, and other sections' reviews offer them (``clip_closed_set.merge_additions``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import importlib
from itertools import starmap
import json
from pathlib import Path
import sqlite3
from typing import Any, NamedTuple

from .clip_match_gallery import MAX_PLAYERS, MAX_VIEW_SAMPLES, RecordingGallery
from .clip_match_identity import MAX_GALLERY_SAMPLES, TEAMS, Anchor, Fragment
from .clip_number_anchors import Sighting


GALLERY_FILE = "identity-gallery.sqlite"
SOURCE = "recording_gallery"
# A carried view that came from a shirt number, not from a confirmation.
AUTOMATIC_SOURCE = "recording_gallery_automatic"
RETAIN_OLD_VIEWS = 3
VIEW_SEPARATOR = "#"
SCHEMA = """
CREATE TABLE IF NOT EXISTS views (
    player TEXT NOT NULL, seed TEXT NOT NULL, origin TEXT NOT NULL, kit TEXT,
    basis TEXT, PRIMARY KEY(player, seed)
);
CREATE TABLE IF NOT EXISTS corrections (
    seed TEXT PRIMARY KEY, player TEXT, revision INTEGER NOT NULL,
    origin TEXT, basis TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
    section TEXT NOT NULL, view TEXT NOT NULL, player TEXT NOT NULL,
    kit TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(section, view)
);
CREATE TABLE IF NOT EXISTS additions (
    player TEXT PRIMARY KEY, team TEXT NOT NULL, number TEXT,
    section TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class GalleryLink:
    """Where a section run's review finds its replay's gallery, and as what.

    ``parent`` is the replay's run ID (a sibling run directory), ``section``
    the section's gallery ID and ``recipe`` the descriptor recipe.
    """

    parent: str
    section: str
    recipe: str

    def path(self, run_directory: Path) -> Path:
        """Resolve the gallery beside a section run, never outside the clip root.

        Raises:
            ValueError: The parent is not a plain run ID.

        """
        if not self.parent or Path(self.parent).name != self.parent:
            raise ValueError("A gallery link names a sibling replay run")
        return run_directory.parent / self.parent / GALLERY_FILE


def player_key(namespace: str, player_id: str) -> str:
    """Gallery identity of one roster player."""
    return f"{namespace}:player:{player_id}"


def prepare(db: Any) -> None:  # noqa: ANN401 - an open sqlite3 connection
    """Create the identity tables; add the basis column to older galleries."""
    db.executescript(SCHEMA)
    for table, column in (
        ("views", "basis"),
        ("corrections", "origin"),
        ("corrections", "basis"),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def open_gallery(path: Path, namespace: str, recipe: str) -> RecordingGallery | None:
    """Open the recording's gallery, or ``None`` when its recipe differs."""
    try:
        gallery = RecordingGallery(path, namespace, descriptor_recipe=recipe)
    except ValueError:
        return None
    with gallery.connect() as db:
        prepare(db)
    return gallery


def carried(
    gallery: RecordingGallery, roster: list[Any]
) -> tuple[list[Fragment], list[Anchor]]:
    """Load each gallery view of a roster player as a confirmed class member.

    The gallery keeps the kit tag a view was seen in (kit tags stay fixed across
    a replay's sections). ``roster`` uses roster sides: a carried piece keeps
    its kit, its anchor names the player's roster side and its origin, and the
    review session decides the orientation from the recording's ledger. A view
    named by number keeps its basis (the orientation it was named under), so
    the session loads it only while that still holds. One piece per view lets a
    later correction remove exactly that view.
    """
    np = importlib.import_module("numpy")
    players = {(p.player_id, p.number): p for p in roster}
    pieces, anchors = [], []
    with gallery.connect() as db:
        prepare(db)
        rows = db.execute(
            "SELECT id, name, team, number, confidence, display "
            "FROM players ORDER BY id LIMIT ?",
            (MAX_PLAYERS,),
        ).fetchall()
        for key, name, team, number, confidence, display in rows:
            player = players.get((name, number))
            if player is None:
                continue
            for seed, count, dims, data, origin, kit, basis in db.execute(
                "SELECT s.seed, s.count, s.dimensions, s.data, v.origin, v.kit, "
                "v.basis "
                "FROM samples s LEFT JOIN views v "
                "ON v.player = s.player AND v.seed = s.seed "
                "WHERE s.player=? ORDER BY s.seed LIMIT ?",
                (key, MAX_GALLERY_SAMPLES),
            ):
                worn = kit if kit in TEAMS else team
                if worn not in TEAMS:
                    continue
                identity = f"{key}{VIEW_SEPARATOR}{seed}"
                raw = np.frombuffer(data, dtype=np.float16).reshape(count, dims)
                pieces.append(
                    Fragment(identity, "gallery", worn, set(), raw={seed: raw})
                )
                human = origin == "human"
                anchors.append(
                    Anchor(
                        identity,
                        player.player_id,
                        player.team,
                        player.number,
                        # Views written without an origin may be automatic,
                        # and without a basis they load under no orientation.
                        SOURCE if human else AUTOMATIC_SOURCE,
                        float(confidence),
                        display,
                        None if human else basis,
                    )
                )
    return pieces, anchors


def add_players(gallery: RecordingGallery, section: str, players: list[Any]) -> None:
    """Record players a section's reviewer added to the recording's roster."""
    if not players:
        return
    with gallery.connect() as db:
        prepare(db)
        db.executemany(
            "INSERT OR IGNORE INTO additions VALUES (?, ?, ?, ?)",
            [(p.player_id, p.team, p.number, section) for p in players],
        )


def added_players(gallery: RecordingGallery) -> list[tuple[str, str, str | None]]:
    """Return the recording's added players (ID, roster side, number) in order."""
    with gallery.connect() as db:
        prepare(db)
        return [
            (str(player), str(team), number)
            for player, team, number in db.execute(
                "SELECT player, team, number FROM additions ORDER BY rowid"
            ).fetchall()
        ]


def withdraw(path: Path, section: str) -> dict[str, int]:
    """Remove a discarded section attempt's commit from a replay's gallery.

    A section can fail after it committed: its views name tracklets the next
    attempt will not have, and its kit sightings would count beside the next
    attempt's. Its views, their corrections, its sightings, its additions and
    its section row go; every other section's commit stays. Pure SQLite: the
    replay adapter calls it without the vision runtime.

    Returns:
        How many views and sightings were removed.

    """
    if not path.is_file():
        return {"views": 0, "sightings": 0}
    prefix = f"{section}:"
    with sqlite3.connect(path, timeout=20) as db:
        prepare(db)
        db.execute("BEGIN IMMEDIATE")
        own = (len(prefix), prefix)
        views = db.execute("DELETE FROM samples WHERE substr(seed, 1, ?) = ?", own)
        removed = views.rowcount
        db.execute("DELETE FROM views WHERE substr(seed, 1, ?) = ?", own)
        db.execute("DELETE FROM corrections WHERE substr(seed, 1, ?) = ?", own)
        sightings = db.execute("DELETE FROM sightings WHERE section=?", (section,))
        db.execute("DELETE FROM additions WHERE section=?", (section,))
        db.execute("DELETE FROM sections WHERE id=?", (section,))
    return {"views": removed, "sightings": sightings.rowcount}


def recorded(gallery: RecordingGallery, section: str) -> list[Sighting]:
    """Return the kit-orientation sightings every other section recorded."""
    with gallery.connect() as db:
        prepare(db)
        rows = db.execute(
            "SELECT player, kit, kind, view, section FROM sightings "
            "WHERE section != ? ORDER BY section, view",
            (section,),
        ).fetchall()
    return list(starmap(Sighting, rows))


def record(db: Any, section: str, sightings: list[Sighting]) -> None:  # noqa: ANN401
    """Replace one section's sightings in the ledger (open transaction)."""
    db.execute("DELETE FROM sightings WHERE section=?", (section,))
    db.executemany(
        "INSERT OR REPLACE INTO sightings VALUES (?, ?, ?, ?, ?)",
        [(section, s.view, s.player_id, s.kit, s.kind) for s in sightings],
    )


class Correction(NamedTuple):
    """A committed view's current name: its player (or none) and provenance."""

    player: str | None
    origin: str = "human"
    basis: str | None = None


def corrections(gallery: RecordingGallery) -> dict[str, Correction]:
    """Return every corrected view by seed: retracted, moved or re-sourced."""
    with gallery.connect() as db:
        prepare(db)
        rows = db.execute("SELECT seed, player, origin, basis FROM corrections")
        return {
            seed: Correction(player, origin or "human", basis)
            for seed, player, origin, basis in rows.fetchall()
        }


def revised(
    anchors: list[Anchor], corrected: Mapping[str, Correction], roster: list[Any]
) -> list[Anchor]:
    """Apply an earlier section reviewer's corrections to carried view anchors.

    A retracted view names nobody any more; a moved view names the confirmed
    player as human evidence (or nobody if that player left the roster); a
    view whose confirmation was undone is automatic evidence again.
    """
    players = {p.player_id: p for p in roster}
    output = []
    for anchor in anchors:
        seed = anchor.identity.rpartition(VIEW_SEPARATOR)[2]
        if seed not in corrected:
            output.append(anchor)
            continue
        correction = corrected[seed]
        player = players.get(correction.player or "")
        if player is not None:
            human = correction.origin == "human"
            output.append(
                replace(
                    anchor,
                    player_id=player.player_id,
                    team=player.team,
                    number=player.number,
                    source=SOURCE if human else AUTOMATIC_SOURCE,
                    display_id=None,
                    named_under=None if human else correction.basis,
                )
            )
    return output


def fingerprint(values: Mapping[str, Any]) -> str:
    """Stable digest of a section's identifying inputs."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def remember(  # noqa: PLR0913, PLR0917 - one section's complete commit.
    gallery: RecordingGallery,
    section: Mapping[str, Any],
    pieces: list[Fragment],
    result: Mapping[str, Any],
    roster: list[Any],
    sightings: list[Sighting] | tuple[Sighting, ...] = (),
) -> dict[str, Any]:
    """Commit this section's anchored views to their players, once per section.

    ``sightings`` are the section's kit-orientation evidence for the ledger. A
    view named by number records the orientation of ``result`` as its basis
    ("any" without a kit tag: its number then names one player whatever the
    orientation).

    Raises:
        ValueError: The section's source range is invalid.

    """
    start, end = float(section["start"]), float(section["end"])
    if not 0 <= start < end:
        raise ValueError("A gallery section needs a source range")
    local = {piece.identity: piece for piece in pieces}
    slots = {p.player_id: p for p in roster}
    groups: dict[str, list[Fragment]] = defaultdict(list)
    named: dict[str, dict] = {}
    origins: dict[str, str] = {}
    swapped = (result.get("orientation") or {}).get("swapped")
    state = None if swapped is None else "swapped" if swapped else "kept"
    for row in result.get("assignments", []):
        piece = local.get(row["identity"])
        if (
            piece is None
            or row.get("status") != "anchored"
            or row.get("source") in {SOURCE, AUTOMATIC_SOURCE}
            or row.get("player_id") not in slots
            or not piece.raw
        ):
            continue
        groups[row["player_id"]].append(piece)
        named[row["player_id"]] = row
        origins[piece.identity] = (
            "human" if row.get("origin") == "human" else ("automatic")
        )
    with gallery.connect() as db:
        prepare(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT fingerprint FROM sections WHERE id=?", (section["id"],)
        ).fetchone()
        if prior is not None:
            return {
                "status": "already_committed"
                if prior[0] == section["fingerprint"]
                else "fingerprint_changed",
                "players": 0,
            }
        if db.execute(
            "SELECT 1 FROM sections WHERE start < ? AND end > ? LIMIT 1", (end, start)
        ).fetchone():
            return {"status": "overlapping_section", "players": 0}
        stored = {row[0] for row in db.execute("SELECT id FROM players").fetchall()}
        corrected = {row[0] for row in db.execute("SELECT seed FROM corrections")}
        for player_id, members in sorted(groups.items()):
            key = player_key(gallery.namespace, player_id)
            if key not in stored and len(stored) >= MAX_PLAYERS:
                continue
            stored.add(key)
            fresh = [
                (piece, f"{section['id']}:{piece.identity}")
                for piece in sorted(members, key=lambda p: -p.weight)
            ]
            store_views(
                db,
                (key, slots[player_id], named[player_id]),
                [
                    (
                        piece,
                        seed,
                        origins[piece.identity],
                        None
                        if origins[piece.identity] == "human"
                        else state
                        if piece.team in TEAMS
                        else "any",
                    )
                    for piece, seed in fresh
                    if seed not in corrected
                ],
            )
        db.execute(
            "INSERT INTO sections VALUES (?, ?, ?, ?, ?)",
            (
                section["id"],
                start,
                end,
                section["fingerprint"],
                json.dumps({"players": sorted(groups)}),
            ),
        )
        record(db, section["id"], list(sightings))
    return {"status": "committed", "players": len(groups)}


def store_views(
    db: Any,  # noqa: ANN401 - an open sqlite3 connection in a transaction
    owner: tuple[str, Any, Mapping[str, Any]],
    fresh: list[tuple[Fragment, str, str, str | None]],
) -> None:
    """Keep a player's oldest views, then this section's best, within the bound.

    ``owner`` is the gallery key, the kit-oriented roster player and the
    section's assignment row; ``fresh`` holds each new view's piece, seed,
    origin and basis. Each kept view records its origin, the kit it was seen in
    and (for a number) the kit orientation it was named under.
    """
    np = importlib.import_module("numpy")
    key, player, row = owner
    previous = [
        (seed, np.frombuffer(data, dtype=np.float16).reshape(count, dims))
        for seed, count, dims, data in db.execute(
            "SELECT seed, count, dimensions, data FROM samples "
            "WHERE player=? ORDER BY seed",
            (key,),
        )
    ]
    labels = {
        seed: (origin, kit, basis)
        for seed, origin, kit, basis in db.execute(
            "SELECT seed, origin, kit, basis FROM views WHERE player=?", (key,)
        )
    }
    for piece, seed, origin, basis in fresh:
        worn = piece.team if piece.team in TEAMS else player.team
        labels[seed] = origin, worn, basis
    new = [
        (seed, subsample(np.asarray(next(iter(piece.raw.values()))), np))
        for piece, seed, _, _ in fresh
    ]
    kept = previous[:RETAIN_OLD_VIEWS]
    kept += new[: MAX_GALLERY_SAMPLES - len(kept)]
    kept += previous[RETAIN_OLD_VIEWS:][: MAX_GALLERY_SAMPLES - len(kept)]
    db.execute(
        "INSERT OR REPLACE INTO players VALUES (?, ?, ?, ?, ?, ?)",
        (
            key,
            player.player_id,
            player.team,
            player.number,
            float(row.get("confidence") or 1.0),
            row.get("display_id"),
        ),
    )
    db.execute("DELETE FROM samples WHERE player=?", (key,))
    db.execute("DELETE FROM views WHERE player=?", (key,))
    for seed, values in kept:
        raw = np.asarray(values, dtype=np.float16)
        db.execute(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
            (key, seed, len(raw), raw.shape[1], raw.tobytes()),
        )
        origin, kit, basis = labels.get(seed, ("automatic", player.team, None))
        db.execute(
            "INSERT INTO views VALUES (?, ?, ?, ?, ?)", (key, seed, origin, kit, basis)
        )


def reconcile(  # noqa: PLR0913, PLR0917 - one section's complete correction.
    gallery: RecordingGallery,
    section_id: str,
    result: Mapping[str, Any],
    kits: Mapping[str, str],
    roster: list[Any],
    sightings: list[Sighting] | None = None,
) -> dict[str, int]:
    """Make this section's committed views follow its reviewer's current answers.

    ``result`` is the section's current solve and ``kits`` the kit tag of each
    of its views. A committed view still anchored to its player stays (and
    becomes human evidence once confirmed); a view a reviewer confirmed as
    another player moves to that player; any other committed view (dismissed,
    undone, a number the answers overruled, or one the recording's orientation
    no longer names) is retracted. Each change is logged as a correction so
    sections that already loaded the view revise it, and so is a kept view
    whose provenance changed (confirmed, or its confirmation undone so only its
    number names it again). ``sightings`` replace the section's ledger entries.

    Returns:
        How many views were kept, moved and retracted.

    """
    players = {p.player_id: p for p in roster}
    rows = {row["identity"]: row for row in result.get("assignments", [])}
    swapped = (result.get("orientation") or {}).get("swapped")
    state = None if swapped is None else "swapped" if swapped else "kept"
    counts = {"kept": 0, "moved": 0, "retracted": 0}
    prefix = f"{section_id}:"
    with gallery.connect() as db:
        prepare(db)
        db.execute("BEGIN IMMEDIATE")
        if sightings is not None:
            record(db, section_id, sightings)
        committed = db.execute(
            "SELECT s.player, s.seed, p.name, v.origin, v.basis FROM samples s "
            "JOIN players p ON p.id = s.player LEFT JOIN views v "
            "ON v.player = s.player AND v.seed = s.seed "
            "WHERE substr(s.seed, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
        revision = db.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM corrections"
        ).fetchone()[0]
        for key, seed, name, origin, basis in committed:
            row = rows.get(seed.removeprefix(prefix), {})
            anchored = row.get("status") == "anchored"
            human = anchored and row.get("origin") == "human"
            if anchored and row.get("player_id") == name:
                counts["kept"] += 1
                # The view's provenance follows the answers in both directions.
                worn = kits.get(seed.removeprefix(prefix))
                now = (
                    Correction(name)
                    if human
                    else Correction(
                        name, "automatic", state if worn in TEAMS else "any"
                    )
                )
                if (now.origin, now.basis) != (origin, basis):
                    db.execute(
                        "UPDATE views SET origin=?, basis=? WHERE player=? AND seed=?",
                        (now.origin, now.basis, key, seed),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO corrections VALUES (?, ?, ?, ?, ?)",
                        (seed, now.player, revision, now.origin, now.basis),
                    )
                continue
            target = players.get(row.get("player_id") or "") if human else None
            moved = target is not None and move(
                db, gallery, (key, seed), target, kits.get(seed.removeprefix(prefix))
            )
            if not moved:
                db.execute("DELETE FROM samples WHERE player=? AND seed=?", (key, seed))
                db.execute("DELETE FROM views WHERE player=? AND seed=?", (key, seed))
            db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?, ?, ?, 'human', NULL)",
                (seed, target.player_id if moved and target else None, revision),
            )
            counts["moved" if moved else "retracted"] += 1
    return counts


def move(
    db: Any,  # noqa: ANN401 - an open sqlite3 connection in a transaction
    gallery: RecordingGallery,
    view: tuple[str, str],
    player: Any,  # noqa: ANN401 - a roster player
    kit: str | None,
) -> bool:
    """Move one committed view to a confirmed player as human evidence.

    Returns:
        Whether the view was moved; a full gallery or player keeps it out.

    """
    source, seed = view
    key = player_key(gallery.namespace, player.player_id)
    if key == source:
        return True
    stored = {row[0] for row in db.execute("SELECT id FROM players").fetchall()}
    if key not in stored and len(stored) >= MAX_PLAYERS:
        return False
    if (
        db.execute("SELECT COUNT(*) FROM samples WHERE player=?", (key,)).fetchone()[0]
        >= MAX_GALLERY_SAMPLES
    ):
        return False
    worn = kit if kit in TEAMS else None
    if key not in stored:
        db.execute(
            "INSERT INTO players VALUES (?, ?, ?, ?, ?, ?)",
            (key, player.player_id, worn or "unknown", player.number, 1.0, None),
        )
    db.execute(
        "UPDATE samples SET player=? WHERE player=? AND seed=?", (key, source, seed)
    )
    db.execute("DELETE FROM views WHERE player=? AND seed=?", (source, seed))
    db.execute(
        "INSERT OR REPLACE INTO views VALUES (?, ?, 'human', ?, NULL)",
        (key, seed, worn),
    )
    return True


def subsample(values: Any, np: Any) -> Any:  # noqa: ANN401 - lazy NumPy arrays
    """At most six evenly spaced raw descriptors of one view."""
    if len(values) <= MAX_VIEW_SAMPLES:
        return values
    return values[np.linspace(0, len(values) - 1, MAX_VIEW_SAMPLES).astype(int)]
