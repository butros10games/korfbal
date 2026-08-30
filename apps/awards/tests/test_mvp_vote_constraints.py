"""Database-level identity invariants for MVP votes."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from django.db import IntegrityError, transaction
import pytest

from apps.awards.models import MatchMvpVote

from .helpers import AwardsScenario


pytestmark = pytest.mark.django_db
EXPECTED_MATCH_VOTE_COUNT = 2


def test_vote_requires_exactly_one_voter_identity() -> None:
    """Votes with neither or both identity forms are rejected by the database."""
    scenario = AwardsScenario.create()
    candidate = scenario.player("candidate")
    voter = scenario.player("voter")

    invalid_creates: tuple[Callable[[], MatchMvpVote], ...] = (
        lambda: MatchMvpVote.objects.create(
            match=scenario.tracker.match,
            candidate=candidate,
        ),
        lambda: MatchMvpVote.objects.create(
            match=scenario.tracker.match,
            candidate=candidate,
            voter=voter,
            voter_token=uuid4(),
        ),
    )
    for create_vote in invalid_creates:
        with pytest.raises(IntegrityError), transaction.atomic():
            create_vote()


def test_authenticated_identity_is_unique_per_match_but_not_globally() -> None:
    """A player gets one vote in each match, not one vote across all matches."""
    first = AwardsScenario.create()
    second = AwardsScenario.create()
    voter = first.player("voter")
    first_candidate = first.player("candidate")
    second_candidate = second.player("candidate")
    MatchMvpVote.objects.create(
        match=first.tracker.match,
        voter=voter,
        candidate=first_candidate,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchMvpVote.objects.create(
            match=first.tracker.match,
            voter=voter,
            candidate=first_candidate,
        )

    MatchMvpVote.objects.create(
        match=second.tracker.match,
        voter=voter,
        candidate=second_candidate,
    )
    assert MatchMvpVote.objects.filter(voter=voter).count() == EXPECTED_MATCH_VOTE_COUNT


def test_anonymous_identity_is_unique_per_match_but_not_globally() -> None:
    """A signed-cookie token gets one vote in each match independently."""
    first = AwardsScenario.create()
    second = AwardsScenario.create()
    token = uuid4()
    first_candidate = first.player("candidate")
    second_candidate = second.player("candidate")
    MatchMvpVote.objects.create(
        match=first.tracker.match,
        voter_token=token,
        candidate=first_candidate,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MatchMvpVote.objects.create(
            match=first.tracker.match,
            voter_token=token,
            candidate=first_candidate,
        )

    MatchMvpVote.objects.create(
        match=second.tracker.match,
        voter_token=token,
        candidate=second_candidate,
    )
    assert (
        MatchMvpVote.objects.filter(voter_token=token).count()
        == EXPECTED_MATCH_VOTE_COUNT
    )
