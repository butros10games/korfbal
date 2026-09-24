"""Defer ambiguous returns without assigning somebody else's public identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_identity import IdentityMemory

CONFIRM_SAMPLES = 3
CONFIRM_SECONDS = 0.15
MAX_PENDING_SECONDS = 0.8
MAX_PENDING_GAP = 0.24
CANDIDATE_COST = 1.3
MARGIN = 0.3
MIN_HISTORY = 1
CLEAR_CONFIRM_SAMPLES = 2
MIN_CANDIDATES = 2


class PendingIdentities:
    """Keep bounded hypotheses while observations retain a provisional identity."""

    def __init__(self, memory: IdentityMemory) -> None:
        """Start with no unresolved returns in this camera segment."""
        self.pending: dict[str, dict] = {}
        self.memory = memory

    def provisional_ids(self) -> set[str]:
        """Do not let another detection inherit an unresolved temporary identity."""
        return {v["provisional"] for v in self.pending.values() if v["provisional"]}

    def start(
        self,
        memory: IdentityMemory,
        obj: dict,
        ranked: list,
        assigned: str | None,
        timestamp: float,
    ) -> dict | None:
        """Defer only genuinely ambiguous returns, leaving clear continuations alone."""
        if (
            assigned is not None
            and timestamp - memory.tracks[assigned]["time"] < MAX_PENDING_GAP
            and not obj.get("identity_uncertain")
        ):
            return None
        challenged = bool(
            assigned is not None
            and len(memory.tracks[assigned]["colors"]) >= MIN_HISTORY
            and ranked
            and ranked[0][1] != assigned
            and ranked[0][0] < 1
        )
        ambiguous = (
            len(ranked) >= MIN_CANDIDATES and ranked[1][0] - ranked[0][0] < MARGIN
        )
        if not challenged and not ambiguous:
            return None
        candidates = [key for _, key in ranked]
        if challenged and assigned not in candidates:
            candidates.append(assigned)
        state = {
            "start": timestamp,
            "last": timestamp,
            "candidates": candidates,
            "provisional": None,
            "winner": None,
            "hits": 0,
            "since": timestamp,
        }
        self.pending[obj["track_id"]] = state
        return state

    def rank(
        self,
        obj: dict,
        color: NDArray[Any] | None,
        candidates: list,
        *,
        timestamp: float,
        court_key: object,
    ) -> list:
        """Appearance and physical gates apply to every hypothesis on every frame."""
        memory = self.memory
        return sorted(
            (cost, key)
            for key in candidates
            if key in memory.tracks
            and memory.possible(memory.tracks[key], obj, timestamp, court_key)
            and (
                cost := min(
                    memory.cost(memory.tracks[key], obj, color, timestamp, reference)
                    for reference in dict.fromkeys((None, court_key))
                )
            )
            < CANDIDATE_COST
        )

    def reconcile(
        self,
        objects: list,
        colors: list,
        matched: dict,
        *,
        timestamp: float,
        court_key: object,
    ) -> None:
        """Confirm unique observations while bounding gaps in visible evidence."""
        memory = self.memory
        self.pending = {
            k: v
            for k, v in self.pending.items()
            if timestamp - v["last"] <= MAX_PENDING_GAP
        }
        for index, obj in enumerate(objects):
            if obj["label"] != "player" or obj.get("recovery_target"):
                continue
            native = obj["track_id"]
            state = self.pending.get(native)
            occupied = {v for k, v in matched.items() if k != index}
            if state is not None:
                # A candidate seen beside this provisional body can never be its
                # replay alias, even if that other body disappears again later.
                state["candidates"] = [
                    key for key in state["candidates"] if key not in occupied
                ]
            candidates = (
                state["candidates"]
                if state
                else [
                    k
                    for k, v in memory.tracks.items()
                    if len(v["colors"]) >= MIN_HISTORY
                    and k not in self.provisional_ids()
                ]
            )
            ranked = self.rank(
                obj,
                colors[index],
                [k for k in candidates if k not in occupied],
                timestamp=timestamp,
                court_key=court_key,
            )
            if state is None:
                state = self.start(memory, obj, ranked, matched.get(index), timestamp)
            if state is None:
                continue
            matched.pop(index, None)
            memory.metric_matches.discard(index)
            identity = self.resolve(
                memory,
                obj,
                state,
                ranked
                if colors[index] is not None
                and (
                    memory.clear_torso(obj, objects)
                    or obj["track_id"] in memory.visible_shirts
                    or (
                        state["hits"] >= CLEAR_CONFIRM_SAMPLES
                        and ranked
                        and ranked[0][1] == state["winner"]
                        and memory.shirts.vote(colors[index]) is not None
                    )
                )
                else None,
                timestamp,
            )
            if identity is not None:
                matched[index] = identity

    def resolve(
        self,
        memory: IdentityMemory,
        obj: dict,
        state: dict,
        ranked: list | None,
        timestamp: float,
    ) -> str | None:
        """Either retain the provisional ID, or publish one confirmed replay alias."""
        native = obj["track_id"]
        state["last"] = timestamp
        winner = (
            ranked[0][1]
            if ranked
            and ranked[0][0] < 1
            and (len(ranked) == 1 or ranked[1][0] - ranked[0][0] >= MARGIN)
            else None
        )
        unsupported = (
            ranked is None
            and timestamp - state.get("evidence_time", state["start"])
            <= MAX_PENDING_GAP + 1e-6
        )
        if not unsupported and (winner != state["winner"] or winner is None):
            state.update(winner=winner, hits=0, since=timestamp)
        if winner is not None:
            state["hits"] += 1
            state["evidence_time"] = timestamp
        provisional = state["provisional"]
        if (
            winner is not None
            and ranked is not None
            and state["hits"] >= CONFIRM_SAMPLES
            and timestamp - state["since"] >= CONFIRM_SECONDS
        ):
            obj["identity_confirmation"] = {
                "from_track_id": provisional,
                "to_track_id": winner,
                "display_id": memory.tracks[winner]["display_id"],
                "gap_seconds": round(timestamp - state["start"], 3),
                "evidence_score": round(max(0, 1 - ranked[0][0]), 3),
            }
            if provisional is not None:
                memory.tracks.pop(provisional, None)
            del self.pending[native]
            return winner
        if timestamp - state["start"] >= MAX_PENDING_SECONDS:
            del self.pending[native]
        else:
            obj["identity_status"] = "pending"
        return provisional if provisional in memory.tracks else None

    def remember(self, native: str, identity: str) -> None:
        """Remember the temporary public number without refreshing candidate tracks."""
        if native in self.pending:
            self.pending[native]["provisional"] = identity
