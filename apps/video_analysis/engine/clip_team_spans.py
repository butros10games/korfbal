"""Per-track shirt-team intervals that let replays fill earlier unknown frames.

Frame chunks are immutable once published, but a track's first observations
usually precede the votes that confirm its team. A span covers one identity
from its first observation (or last boundary) until it ends. Contradictory
shirt evidence closes a span at its last supporting vote, so a body swap is
never painted with the previous player's team. Likewise a span never reaches
back past an opposing clear shirt or across a longer absence, after which the
tracker may have revived the identity on another body.
"""

from collections.abc import Iterable
from copy import deepcopy
from operator import itemgetter


VERSION = 1
MAX_SPANS = 4096
# Filling earlier frames amplifies a wrong track label, so publish only
# intervals whose shirt evidence is repeated and nearly unanimous.
MIN_SPAN_VOTES = 5
MIN_SPAN_SHARE = 0.9
MAX_ABSENCE_SECONDS = 1.0
EPSILON = 1e-6


class TeamSpans:
    """Bound published intervals; open spans stay mutable until a boundary."""

    def __init__(self) -> None:
        """Start without intervals."""
        self.closed: list[dict] = []
        self.open: dict[str, dict] = {}
        self.truncated = False

    def observe(self, identity: str, timestamp: float) -> dict:
        """Extend or open the identity's current interval."""
        span = self.open.get(identity)
        if span is not None and timestamp - span["end"] > MAX_ABSENCE_SECONDS:
            self.close(identity)
            span = None
        if span is None:
            span = self.open[identity] = {
                "track_id": identity,
                "team": None,
                "start": timestamp,
                "end": timestamp,
                "last_vote": None,
                "votes": 0,
                "share": 0.0,
                "latest": {},
            }
        span["end"] = timestamp
        return span

    def vote(self, identity: str, team: str, timestamp: float) -> None:
        """Remember each team's latest clear shirt in the unconfirmed interval."""
        self.open[identity]["latest"][team] = timestamp

    def confirm(
        self, identity: str, team: str, last_vote: float, votes: Iterable[tuple]
    ) -> None:
        """Attach the confirmed team and the purity of votes inside this interval.

        ``votes`` holds ``(team_index, margin, time)`` shirt votes of the track.
        """
        span = self.open[identity]
        if span["team"] not in {None, team}:
            self.boundary(identity, span["end"], resume=last_vote)
            span = self.open[identity]
        if span["team"] is None:
            opposing = [t for k, t in span["latest"].items() if k != team]
            if opposing:
                span["start"] = max(span["start"], max(opposing) + EPSILON)
        inside = [v for v in votes if span["start"] <= v[2] <= last_vote]
        support = sum(f"team_{'ab'[v[0]]}" == team for v in inside)
        span.update(
            team=team,
            last_vote=last_vote,
            votes=support,
            share=round(support / max(1, len(inside)), 3),
        )

    def boundary(
        self, identity: str, timestamp: float, *, resume: float | None = None
    ) -> None:
        """Close at the last supporting vote; the identity resumes as a new body."""
        span = self.open.pop(identity, None)
        if span is not None and span["last_vote"] is not None:
            span["end"] = span["last_vote"]
            self.publish(span)
        self.observe(identity, timestamp if resume is None else resume)
        self.open[identity]["end"] = timestamp

    def close(self, identity: str) -> None:
        """End an interval whose identity disappeared without contrary evidence."""
        span = self.open.pop(identity, None)
        if span is not None:
            self.publish(span)

    def close_all(self) -> None:
        """End every interval at a camera cut or the end of analysis."""
        for identity in list(self.open):
            self.close(identity)

    def publish(self, span: dict) -> None:
        """Keep only well-supported intervals, within a fixed receipt size."""
        if not supported(span):
            return
        if len(self.closed) >= MAX_SPANS:
            self.truncated = True
            return
        self.closed.append(span)

    def snapshot(self) -> dict:
        """Return closed and currently confirmed intervals as detached records."""
        spans = self.closed + [s for s in self.open.values() if supported(s)]
        return {
            "version": VERSION,
            "spans": [
                {
                    k: deepcopy(v)
                    for k, v in s.items()
                    if k not in {"last_vote", "latest"}
                }
                for s in sorted(spans, key=itemgetter("start", "track_id"))
            ][:MAX_SPANS],
            "truncated": self.truncated or len(spans) > MAX_SPANS,
        }


def supported(span: dict) -> bool:
    """Require repeated, nearly unanimous shirt votes before filling other frames."""
    return bool(
        span["team"]
        and span["votes"] >= MIN_SPAN_VOTES
        and span["share"] >= MIN_SPAN_SHARE
    )
