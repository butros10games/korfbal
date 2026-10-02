"""Cross-season Elo per club team, replayed over the complete imported history.

Unlike the per-season ``elo-v1`` baseline, a club team (the native team behind
every season's provider entry) keeps its rating between seasons. Parameters were
chosen on 2022-07..2024-12 results and checked on untouched 2025-01..2026-10
results (358k matches, October 2026). Against ``elo-v1`` on the same held-out
matches, the expected-score Brier score fell from 0.221 to 0.193 for teams early
in a phase and from 0.192 to 0.152 for established teams:

* goal margin relative to the match total scales each update, since one result
  carries little information without it (Brier 0.175 versus 0.152);
* home advantage, and a high K factor: ratings must follow squads that change;
* poules are regraded by strength every phase, so a team enters a new phase at
  30% of its old rating plus 70% of its new poule's current average.

Separate indoor and outdoor ratings were worse than one shared rating.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import fmean


MODEL_VERSION = "elo-v2"
INITIAL_RATING = 1500.0
K_FACTOR = 64.0
HOME_ADVANTAGE = 40.0
RATING_SCALE = 400.0
MARGIN_WEIGHT = 4.0
PHASE_CARRY = 0.3
PROVISIONAL_GAMES = 10
# Draws peak at about 9% between equal teams and fall off with the rating gap.
DRAW_SHARE = 0.09


@dataclass(frozen=True, slots=True)
class Fixture:
    """One provider match; unplayed fixtures only establish poule membership."""

    match: int
    phase: str
    pool: int | None
    starts_at: datetime
    home: str
    away: str
    home_score: int | None = None
    away_score: int | None = None

    @property
    def result(self) -> bool:
        """Report whether this fixture carries a rateable score."""
        return self.home_score is not None and self.away_score is not None


@dataclass(slots=True)
class TeamState:
    """A club team's rating with the phase it last entered."""

    rating: float = INITIAL_RATING
    games: int = 0
    phase: str = ""
    phase_start: float = INITIAL_RATING
    last_played: datetime | None = None
    group: str = ""


@dataclass(frozen=True, slots=True)
class MatchRating:
    """Ratings both teams brought into a match and the resulting home change."""

    match: int
    home_rating: float
    away_rating: float
    home_expected: float
    home_change: float
    home_games: int
    away_games: int
    home_phase_start: float
    away_phase_start: float
    home: str
    away: str
    starts_at: datetime
    phase: str


@dataclass
class Replay:
    """Final team states and every rated match, in kickoff order."""

    teams: dict[str, TeamState] = field(default_factory=dict)
    matches: list[MatchRating] = field(default_factory=list)


def expected_score(home: float, away: float) -> float:
    """Return the expected home result (win 1, draw 0.5) with home advantage."""
    gap = (away - home - HOME_ADVANTAGE) / RATING_SCALE
    return 1 / (1 + 10 ** max(-100.0, min(100.0, gap)))


def outcome_probabilities(expected: float) -> dict[str, float]:
    """Split an expected score into win, draw and loss with the same expectation."""
    draw = DRAW_SHARE * 4 * expected * (1 - expected)
    return {"home": expected - draw / 2, "draw": draw, "away": 1 - expected - draw / 2}


def entering(rating: float | None, poule: list[float]) -> float:
    """Rating a team brings into a new phase, anchored on its new poule."""
    target = fmean(poule) if poule else INITIAL_RATING
    return target if rating is None else target + PHASE_CARRY * (rating - target)


def membership(fixtures: list[Fixture]) -> dict[tuple[str, int | None], set[str]]:
    """Club teams scheduled in each phase's poule, played or not."""
    members: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    for fixture in fixtures:
        members[fixture.phase, fixture.pool] |= {fixture.home, fixture.away}
    return members


def replay(
    fixtures: list[Fixture],
    *,
    initial: dict[str, TeamState] | None = None,
    members: dict[tuple[str, int | None], set[str]] | None = None,
) -> Replay:
    """Rate results chronologically so corrections are never double counted.

    ``initial`` resumes from the team states just before the earliest fixture, and
    ``members`` then supplies complete poules, including fixtures before the resume
    point. Resuming yields exactly the ratings of a replay from the start.
    """
    poules = members if members is not None else membership(fixtures)
    state = Replay(
        teams={team: replace(value) for team, value in (initial or {}).items()}
    )
    parents = {team: value.group or team for team, value in state.teams.items()}
    results = sorted(
        (fixture for fixture in fixtures if fixture.result),
        key=lambda fixture: (fixture.starts_at, fixture.match),
    )
    for fixture in results:
        if fixture.home == fixture.away:
            continue
        for team in (fixture.home, fixture.away):
            enter(
                state.teams,
                team,
                fixture,
                poules.get((fixture.phase, fixture.pool), set()),
            )
        home, away = state.teams[fixture.home], state.teams[fixture.away]
        expected = expected_score(home.rating, away.rating)
        change = update(fixture, expected)
        state.matches.append(
            MatchRating(
                fixture.match,
                home.rating,
                away.rating,
                expected,
                change,
                home.games,
                away.games,
                home.phase_start,
                away.phase_start,
                fixture.home,
                fixture.away,
                fixture.starts_at,
                fixture.phase,
            )
        )
        home.rating += change
        away.rating -= change
        for team in (home, away):
            team.games += 1
            team.last_played = fixture.starts_at
        join(parents, fixture.home, fixture.away)
    for team, rating in state.teams.items():
        rating.group = root(parents, team)
    return state


def enter(
    teams: dict[str, TeamState], team: str, fixture: Fixture, poule: set[str]
) -> None:
    """Move a team into the fixture's phase on its first result there."""
    current = teams.get(team)
    if current is not None and current.phase == fixture.phase:
        return
    rating = entering(
        current.rating if current else None,
        [teams[member].rating for member in sorted(poule) if member in teams],
    )
    if current is None:
        current = teams[team] = TeamState()
    current.rating = current.phase_start = rating
    current.phase = fixture.phase


def update(fixture: Fixture, expected: float) -> float:
    """Home rating change, larger for decisive margins relative to the total."""
    assert fixture.home_score is not None
    assert fixture.away_score is not None
    home, away = fixture.home_score, fixture.away_score
    outcome = 0.5 if home == away else float(home > away)
    margin = 1 + MARGIN_WEIGHT * abs(home - away) / max(home + away, 1)
    return K_FACTOR * margin * (outcome - expected)


def join(parents: dict[str, str], first: str, second: str) -> None:
    """Merge two teams' comparison groups once they have met."""
    parents.setdefault(first, first)
    parents.setdefault(second, second)
    left, right = root(parents, first), root(parents, second)
    parents[max(left, right)] = min(left, right)


def root(parents: dict[str, str], team: str) -> str:
    """Resolve a connected schedule group with path compression."""
    parents.setdefault(team, team)
    while parents[team] != team:
        parents[team] = parents[parents[team]]
        team = parents[team]
    return team
