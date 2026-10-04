"""Private, owner-scoped cached evidence for incremental roster confirmations.

The clip process persists projected crop samples once. A caller supplies the
server-owned run path and scope; answers cannot select another file or recording.
SQLite transactions serialize answers across workers and retain replay receipts.

Run as ``python -m <package>.clip_identity_review request.json`` in the isolated
vision runtime: it applies queued answers, writes a compact review snapshot and,
optionally, crops for the next questions. Web processes never import NumPy.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
import importlib
from itertools import starmap
import json
import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from .clip_closed_set import (
    MAX_DIMENSIONS,
    MAX_SAMPLES,
    MAX_TRACKLETS,
    PROPOSAL_MARGIN,
    RECIPE,
    Confidence,
    Player,
    ReviewSession,
    Scope,
    Settings,
    merge_additions,
    samples,
)
from .clip_match_identity import Fragment, NumberEvidence
from .clip_section_identity import (
    GalleryLink,
    add_players,
    added_players,
    corrections,
    open_gallery,
    reconcile,
    recorded,
    revised,
)


if TYPE_CHECKING:
    from .clip_match_gallery import RecordingGallery


FLOAT_BYTES = 8
TIMED_OUT = "The re-solve ran out of time; nothing changed. Answer again."
MAX_BYTES = MAX_TRACKLETS * MAX_SAMPLES * MAX_DIMENSIONS * FLOAT_BYTES
SCHEMA = """
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY CHECK (id = 1), namespace TEXT NOT NULL,
    fingerprint TEXT NOT NULL, settings TEXT NOT NULL, roster TEXT NOT NULL,
    state TEXT NOT NULL, refinement TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fragments (
    position INTEGER PRIMARY KEY, metadata TEXT NOT NULL,
    count INTEGER NOT NULL, dimension INTEGER NOT NULL, samples BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS gallery (
    id INTEGER PRIMARY KEY CHECK (id = 1), parent TEXT NOT NULL,
    section TEXT NOT NULL, recipe TEXT NOT NULL
);
"""


class ReviewCache:
    """A private clip-run artifact; no crop images, unsafe pickle or global names."""

    def __init__(self, path: Path, scope: Scope) -> None:
        """Require the caller's authorized run path and recording/evidence scope."""
        self.path, self.scope = path, scope

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Lease the review atomically, including ownership checks and re-solving.

        Yields:
            The connection holding this review's exclusive answer transaction.

        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=20) as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise

    def create(
        self,
        session: ReviewSession,
        *,
        refinement: dict | None = None,
        gallery: GalleryLink | None = None,
    ) -> None:
        """Persist fitted samples once, without overwriting later confirmations.

        A section of a replay links its replay's gallery: answers here then
        correct the views this section committed, and corrections logged by
        other sections revise the views this section carried.

        Raises:
            ValueError: The evidence is unbounded or belongs to another review.

        """
        expected = self.scope.namespace, self.scope.fingerprint
        if (session.namespace, session.fingerprint) != expected:
            raise ValueError("Review cache belongs to different evidence")
        np = importlib.import_module("numpy")
        with self.connect() as db:
            if db.execute("SELECT id FROM review").fetchone():
                existing = self.load(db)
                if existing.settings != session.settings:
                    raise ValueError("Review cache belongs to different settings")
                return
            db.execute(
                "INSERT INTO review VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    *expected,
                    json.dumps(asdict(session.settings)),
                    json.dumps([asdict(p) for p in session.roster]),
                    json.dumps(session.state()),
                    json.dumps(refinement or {}),
                ),
            )
            if gallery is not None:
                db.execute(
                    "INSERT INTO gallery VALUES (1, ?, ?, ?)",
                    (gallery.parent, gallery.section, gallery.recipe),
                )
            for index, piece in enumerate(session.pieces):
                values = np.asarray(samples(piece), dtype="<f8")
                if values.size:
                    count, dimension = values.shape
                else:
                    count, dimension = 0, 0
                metadata = {
                    "identity": piece.identity,
                    "shot": piece.shot,
                    "team": piece.team,
                    "times": sorted(piece.times),
                    "numbers": [asdict(n) for n in piece.numbers],
                    "replay": piece.replay,
                    "reference": piece.reference,
                    "court": piece.court,
                    "weight": piece.weight,
                    "conflicted": piece.conflicted,
                    "reference_verified": piece.reference_verified,
                    "representative_track_id": piece.representative_track_id,
                    "placement": list(piece.placement),
                }
                db.execute(
                    "INSERT INTO fragments VALUES (?, ?, ?, ?, ?)",
                    (index, json.dumps(metadata), count, dimension, values.tobytes()),
                )

    def load(self, db: sqlite3.Connection) -> ReviewSession:
        """Restore the same fitted space and current owner-leased confirmations.

        Raises:
            ValueError: The stored artifact is absent, cross-owned or malformed.

        """
        header = db.execute("SELECT * FROM review WHERE id=1").fetchone()
        if not header or tuple(header[1:3]) != (
            self.scope.namespace,
            self.scope.fingerprint,
        ):
            raise ValueError("Review cache belongs to different evidence")
        policy, roster, state = (json.loads(value) for value in header[3:6])
        raw_calibration = policy.pop("calibration")
        if raw_calibration:
            raw_calibration["bins"] = tuple(
                tuple(bin_) for bin_ in raw_calibration["bins"]
            )
        policy["uncalibrated"] = tuple(policy.get("uncalibrated", ()))
        settings = Settings(
            **policy,
            calibration=Confidence(**raw_calibration) if raw_calibration else None,
        )
        rows = db.execute("SELECT * FROM fragments ORDER BY position").fetchall()
        if len(rows) > MAX_TRACKLETS or sum(len(r[4]) for r in rows) > MAX_BYTES:
            raise ValueError("Review cache exceeds its evidence bound")
        pieces = [decode_fragment(row) for row in rows]
        session = ReviewSession(
            self.scope, pieces, [Player(**p) for p in roster], settings
        )
        session.restore(state)
        gallery = self.gallery(db)
        if gallery is not None:
            # Other sections' reviewers may have corrected carried views or
            # changed the recording's kit-orientation evidence since.
            current = revised(session.carried, corrections(gallery), session.roster)
            ledger = recorded(gallery, self.section(db))
            # Players another section's reviewer added are on this roster too.
            _, _, added = merge_additions(
                session.roster, list(starmap(Player, added_players(gallery)))
            )
            if (
                added
                or current != session.carried
                or set(ledger) != set(session.ledger)
            ):
                session.recarry(current, ledger, added)
        return session

    def section(self, db: sqlite3.Connection) -> str:
        """Return this review's section ID in its replay's gallery, if linked."""
        row = db.execute("SELECT section FROM gallery").fetchone()
        return str(row[0]) if row else ""

    def gallery(self, db: sqlite3.Connection) -> RecordingGallery | None:
        """Open the linked replay gallery, if this review belongs to a section.

        Raises:
            ValueError: The section's gallery was not restored: answers would
                neither correct it nor see other sections' evidence.

        """
        row = db.execute("SELECT parent, section, recipe FROM gallery").fetchone()
        if row is None:
            return None
        link = GalleryLink(*row)
        path = link.path(self.path.parent)
        if not path.is_file():
            raise ValueError("The replay's identity gallery is unavailable")
        return open_gallery(path, self.scope.namespace, link.recipe)

    def correct(
        self,
        db: sqlite3.Connection,
        session: ReviewSession,
        added: list[Player] | None = None,
    ) -> dict | None:
        """Let this section's current answers win over the views it committed.

        ``added`` are players this section's reviewer just added to the roster:
        the gallery records them for the recording's other sections.

        Returns:
            The kept/moved/retracted counts, or ``None`` without a gallery.

        """
        gallery = self.gallery(db)
        if gallery is None:
            return None
        add_players(gallery, self.section(db), added or [])
        return reconcile(
            gallery,
            self.section(db),
            session.result,
            {p.identity: p.team for p in session.pieces},
            session.roster,
            session.sightings(),
        )

    def snapshot(self) -> dict:
        """Read current questions and proposal aliases after a process restart."""
        with self.connect() as db:
            return self.response(db, self.load(db).snapshot())

    def answer(self, payload: dict) -> dict:
        """Apply the versioned answer contract under the persistent review lease.

        Raises:
            ValueError: The answer is invalid or its re-solve ran out of time.

        """
        with self.connect() as db:
            session = self.load(db)
            known = {p.player_id for p in session.roster}
            result = session.answer(payload)
            if result.get("status") == "stopped":
                raise ValueError(TIMED_OUT)
            self.save(db, session)
            self.correct(
                db, session, [p for p in session.roster if p.player_id not in known]
            )
            return self.response(db, result)

    def save(self, db: sqlite3.Connection, session: ReviewSession) -> None:
        """Persist answers and free-form roster additions in the leased transaction."""
        db.execute(
            "UPDATE review SET state=?, roster=? WHERE id=1",
            (
                json.dumps(session.state()),
                json.dumps([asdict(p) for p in session.roster]),
            ),
        )

    def apply(self, payloads: list[dict]) -> tuple[dict, list[dict]]:
        """Apply queued answers in order under one lease; each one fails alone.

        An answer whose re-solve stopped (its deadline) changed nothing: it is
        rejected as ``timed_out`` at the unchanged revision and leaves no
        receipt, so the same request can be applied once there is time.

        Returns:
            The compact review after the batch and one receipt per answer.

        """
        receipts = []
        with self.connect() as db:
            session = self.load(db)
            known = {p.player_id for p in session.roster}
            for payload in payloads:
                try:
                    applied = session.answer(payload)
                except (ValueError, TypeError, KeyError) as error:
                    message = str(error) if isinstance(error, ValueError) else ""
                    receipts.append({
                        "request_id": str(payload.get("request_id", ""))[:128],
                        "status": "rejected",
                        "code": "revision_conflict"
                        if "revision conflict" in message
                        else "invalid",
                        "message": message[:300] or "Invalid review answer",
                    })
                    continue
                if applied.get("status") == "stopped":
                    receipts.append({
                        "request_id": payload["request_id"],
                        "status": "rejected",
                        "code": "timed_out",
                        "message": TIMED_OUT,
                        "revision": session.revision,
                    })
                    continue
                receipts.append({
                    "request_id": payload["request_id"],
                    "status": "applied",
                    "revision": applied.get("applied_revision", session.revision),
                })
            self.save(db, session)
            self.correct(
                db, session, [p for p in session.roster if p.player_id not in known]
            )
            return compact(session), receipts

    def refinement(self) -> dict:
        """Read the immutable alias overlay written when the clip finished."""
        with self.connect() as db:
            row = db.execute("SELECT refinement FROM review WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def response(self, db: sqlite3.Connection, snapshot: dict) -> dict:
        """Apply fresh proposals to cached video/court aliases for the review tool."""
        row = db.execute("SELECT refinement FROM review WHERE id=1").fetchone()
        refinement = json.loads(row[0])
        assignments = {
            a["identity"]: a for a in snapshot["result"].get("assignments", [])
        }
        for key in ("links", "frame_links"):
            for link in refinement.get(key, []):
                assignment = assignments.get(link.get("fragment_identity"))
                if assignment and assignment["player_id"]:
                    link.update(
                        to_track_id=assignment["player_id"],
                        display_id=assignment["display_id"],
                        name_origin=assignment.get("origin"),
                    )
                elif assignment and link.get("source") != "linked_propagation":
                    # Unnamed tracklets keep their within-shot linked identity.
                    link.update(
                        to_track_id=link.get("unnamed_track_id")
                        or assignment["identity"],
                        display_id=link.get("unnamed_display_id") or "?",
                        name_origin=None,
                    )
        refinement["match_identity"] = snapshot["result"]
        return {**snapshot, "identity_refinement": refinement}


def compact(session: ReviewSession) -> dict:
    """Return what a review tool needs, without per-frame aliases or samples.

    Clients patch the clip's existing ``fragment_identity`` aliases with
    ``fragments``; automatic names must stay visibly distinct from confirmed.
    """
    naming = session.naming()
    result = session.result
    kits = result.get("orientation", {})
    return {
        "version": 1,
        "namespace": session.namespace,
        "fingerprint": session.fingerprint,
        "revision": session.revision,
        "status": result.get("status", "completed"),
        "classifier_recipe": RECIPE,
        "descriptor_recipe": session.settings.descriptor_recipe,
        "calibration": result.get("calibration"),
        "proposal_margin": PROPOSAL_MARGIN,
        "roster": [asdict(p) for p in session.roster],
        "orientation": {
            "swapped": session.swapped,
            "oriented": session.oriented,
            **{
                key: kits.get(key)
                for key in ("source", "state", "outliers", "suspect_views")
            },
        },
        "questions": session.questions(),
        "fragments": naming,
        "anchors": [
            {"identity": a.identity, "player_id": a.player_id} for a in session.anchors
        ],
        "dismissals": [
            {"identity": identity, "reason": reason}
            for identity, reason in session.dismissals.items()
        ],
        "summary": session.summary(naming),
    }


def main() -> None:
    """Apply queued answers for one server-selected clip run and write a snapshot.

    The request names the private cache, its owner scope and an output path; the
    caller (the vision worker) resolves those from authorized database rows.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    request = json.loads(parser.parse_args().request.read_text(encoding="utf-8"))
    run = Path(request["run_directory"])
    cache = ReviewCache(
        run / "identity-review.sqlite",
        Scope(request["namespace"], request["fingerprint"]),
    )
    review, receipts = cache.apply(request.get("answers", []))
    output = {"review": review, "answers": receipts}
    crops = request.get("crops")
    if crops:
        module = importlib.import_module(f"{__package__}.clip_identity_crops")
        try:
            review["crops"] = module.extract(
                run,
                crops["video"],
                review["questions"],
                cache.refinement(),
                [run / name for name in crops.get("chunks", [])],
            )
        except (OSError, ValueError, KeyError, ImportError) as error:
            output["crop_error"] = str(error)[:300]
    target = Path(request["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(output), encoding="utf-8")
    os.replace(temporary, target)


def decode_fragment(row: tuple) -> Fragment:
    """Decode bounded float samples with no executable object deserialization.

    Raises:
        ValueError: Sample shape or payload length exceeds the fitted-space bound.

    """
    _, metadata, count, dimension, data = row
    if (
        not 0 <= count <= MAX_SAMPLES
        or not 0 <= dimension <= MAX_DIMENSIONS
        or len(data) != count * dimension * FLOAT_BYTES
    ):
        raise ValueError("Invalid review sample shape")
    np = importlib.import_module("numpy")
    values = np.frombuffer(data, dtype="<f8").reshape(count, dimension).copy()
    attributes = json.loads(metadata)
    attributes["times"] = set(attributes["times"])
    attributes["numbers"] = [NumberEvidence(**n) for n in attributes["numbers"]]
    attributes["placement"] = tuple(attributes.get("placement", (0, 0)))
    return Fragment(**attributes, samples=list(values))


if __name__ == "__main__":
    main()
