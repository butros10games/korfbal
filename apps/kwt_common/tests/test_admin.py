"""Exercise every registered screen and catalogue-sized admin interactions."""

from datetime import timedelta
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles import finders
from django.db import connection, models
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.admin.shot_admin import ShotAdminForm
from apps.game_tracker.models import MatchData
from apps.kwt_common.templatetags.admin_assets import admin_asset
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


AUTOCOMPLETE_PAGE_SIZE = 20

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator() -> Client:
    """Authenticate a synthetic administrator without hashing an unused password."""
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="operator"))
    return client


@pytest.mark.parametrize(
    "model", list(admin.site._registry), ids=lambda m: m._meta.label_lower
)
def test_all_registered_screens(operator: Client, model: type[models.Model]) -> None:
    """Lists, searches and writable add forms remain valid for every model."""
    screen = admin.site.get_model_admin(model)
    prefix = f"admin:{model._meta.app_label}_{model._meta.model_name}"
    for query in ({}, {"q": "Example"}, {"q": str(UUID(int=1))}):
        response = operator.get(reverse(f"{prefix}_changelist"), query)
        assert response.status_code == HTTPStatus.OK
        assert b"kwt_admin/admin.css" in response.content
    request = RequestFactory().get("/admin/")
    request.user = get_user_model().objects.get(username="operator")
    if screen.has_add_permission(request):
        assert operator.get(reverse(f"{prefix}_add")).status_code == HTTPStatus.OK


@pytest.fixture
def match() -> Match:
    """Build a native fixture with the relations used by displayed labels."""
    now = timezone.now()
    season = Season.objects.create(
        name="Example season",
        start_date=now.date(),
        end_date=now.date() + timedelta(days=90),
    )
    club = Club.objects.create(name="Example club")
    home = Team.objects.create(name="Example home", club=club)
    away = Team.objects.create(name="Example away", club=club)
    return Match.objects.create(
        season=season, home_team=home, away_team=away, start_time=now
    )


@pytest.mark.parametrize("screen", ["schedule_match", "game_tracker_matchdata"])
def test_match_list_query_count_does_not_grow(
    operator: Client, match: Match, screen: str
) -> None:
    """Adding rows must not add SQL queries for related team and match labels."""
    MatchData.objects.get_or_create(match_link=match)
    url = reverse(f"admin:{screen}_changelist")
    operator.get(url)
    with CaptureQueriesContext(connection) as before:
        assert operator.get(url).status_code == HTTPStatus.OK
    for number in range(15):
        fixture = Match.objects.create(
            season=match.season,
            home_team=match.home_team,
            away_team=match.away_team,
            start_time=match.start_time + timedelta(hours=number + 1),
        )
        MatchData.objects.get_or_create(match_link=fixture)
    with CaptureQueriesContext(connection) as after:
        response = operator.get(url)
    assert response.status_code == HTTPStatus.OK
    assert len(after) == len(before)
    assert b"Example home" in response.content


def test_match_autocomplete_is_bounded_and_checks_permissions(
    operator: Client, match: Match
) -> None:
    """Bound team dropdowns and enforce view permission on search."""
    Team.objects.bulk_create([
        Team(name=f"Other {i}", club=match.home_team.club) for i in range(70)
    ])
    form = operator.get(reverse("admin:schedule_match_change", args=[match.pk]))
    assert b"admin-autocomplete" in form.content
    assert b"Other 69" not in form.content
    query = {
        "app_label": "schedule",
        "model_name": "match",
        "field_name": "home_team",
        "term": "Other",
    }
    response = operator.get(reverse("admin:autocomplete"), query)
    assert len(response.json()["results"]) == AUTOCOMPLETE_PAGE_SIZE
    assert response.json()["pagination"]["more"] is True
    staff = get_user_model().objects.create_user(username="restricted", is_staff=True)
    operator.force_login(staff)
    assert (
        operator.get(reverse("admin:autocomplete"), query).status_code
        == HTTPStatus.FORBIDDEN
    )


def test_exact_uuid_search_and_named_search(operator: Client, match: Match) -> None:
    """UUID searches use equality while ordinary names still return fixtures."""
    for term in (str(match.pk), "Example home"):
        response = operator.get(reverse("admin:schedule_match_changelist"), {"q": term})
        assert response.status_code == HTTPStatus.OK
        assert list(response.context["cl"].result_list) == [match]
    response = operator.get(
        reverse("admin:schedule_match_changelist"), {"q": str(UUID(int=1))}
    )
    assert response.context["cl"].result_count == 0


def test_relation_filter_preserves_search_and_handles_many_to_many(
    operator: Client,
) -> None:
    """Group filters work without enumerating groups or dropping existing controls."""
    user = get_user_model().objects.get(username="operator")
    group = Group.objects.create(name="Operations")
    user.groups.add(group)
    url = reverse("admin:auth_user_changelist")
    response = operator.get(
        url, {"groups__id__exact": group.pk, "q": "operator", "o": "1"}
    )
    assert response.status_code == HTTPStatus.OK
    assert list(response.context["cl"].result_list) == [user]
    assert b'name="q" value="operator"' in response.content
    assert b'name="o" value="1"' in response.content
    assert response.content.count(b"/admin/js/vendor/select2/select2.full") == 1


def test_permission_aware_navigation(operator: Client) -> None:
    """A restricted operator sees only permitted shortcuts and catalogue sections."""
    staff = get_user_model().objects.create_user(username="viewer", is_staff=True)
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="schedule", codename="view_match"
        )
    )
    operator.force_login(staff)
    response = operator.get(reverse("admin:index"))
    assert response.status_code == HTTPStatus.OK
    assert b"Polling monitor" not in response.content
    assert b"Tournaments</a>" not in response.content
    assert reverse("admin:schedule_match_changelist").encode() in response.content


def test_shot_form_validates_team_for_submitted_match(match: Match) -> None:
    """Add and change forms accept only teams belonging to the submitted fixture."""
    data, _ = MatchData.objects.get_or_create(match_link=match)
    player = Player.objects.create(name="Example defender")
    payload = {
        "match_data": str(data.pk),
        "player": str(player.pk),
        "team": str(match.away_team_id),
        "for_team": False,
        "scored": True,
    }
    form = ShotAdminForm(data=payload)
    assert form.is_valid(), form.errors
    shot = form.save()
    other = Team.objects.create(name="Unrelated", club=match.home_team.club)
    invalid = ShotAdminForm(data={**payload, "team": str(other.pk)}, instance=shot)
    assert not invalid.is_valid()
    assert "team" in invalid.errors
    malformed = ShotAdminForm(data={**payload, "match_data": "invalid"}, instance=shot)
    assert not malformed.is_valid()


@pytest.mark.parametrize(
    "path",
    [
        "competition/monitor.css",
        "competition/monitor.js",
        "kwt_admin/admin.css",
        "kwt_admin/admin.js",
    ],
)
def test_admin_assets_have_content_versions(path: str) -> None:
    """All custom asset references resolve locally and bypass stale immutable caches."""
    source = finders.find(path)
    assert isinstance(source, str)
    expected = sha256(Path(source).read_bytes()).hexdigest()[:16]
    assert parse_qs(urlsplit(admin_asset(path)).query)["v"] == [expected]
