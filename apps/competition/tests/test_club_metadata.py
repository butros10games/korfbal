"""Current directory proof, public club details and historical logo precedence."""

from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.club.models import Club as NativeClub
from apps.competition.models import Club, Match, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.logos import cache_logo, logo_name
from apps.competition.services.publishing import Publisher
from apps.competition.tests.test_club_info import CONTACT, SPORTS
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


def details(source: str = "C1", digest: str = "ABC1") -> dict:
    """Use synthetic public catalogue content."""
    return {
        "ClubId": source,
        "ClubName": "Example",
        "City": "Example city",
        "ClubColors": {"HomeOutfit": {"Shirt": "Red"}},
        "ClubLogo": {"Bucket": "KNKV-production-REPL", "Hash": digest},
        "PhoneNumber": "private",
        "Email": "private@example.test",
    }


@pytest.mark.django_db
def test_club_details_have_no_program_results_or_team_discovery(season: Season) -> None:
    """A dedicated Club v1 request cannot schedule unrelated provider traffic."""
    Club.objects.create(external_id="C1", name="Before")
    importer = Importer(season, timezone.now())
    importer.apply("club_details", "C1", details())
    club = Club.objects.get(external_id="C1")
    assert (club.name, club.city, club.logo_hash) == ("Example", "Example city", "ABC1")
    assert club.colors == {"HomeOutfit": {"Shirt": "Red"}}
    assert club.current_directory_observed_at is None
    assert set(SyncResource.objects.values_list("kind", flat=True)) == {"club_logo"}
    with pytest.raises(ValueError, match="Unrecognized"):
        importer.apply("club_details", "C1", details("another"))
    with pytest.raises(ValueError, match="Unrecognized"):
        importer.apply("club_details", "C1", {})
    assert "private" not in str(club.metadata_observations)


@pytest.mark.django_db
def test_directory_proof_is_separate_from_historical_existence(season: Season) -> None:
    """A named historical club is not proof of current directory membership."""
    now = timezone.now()
    Importer(season, now, discover=False).club(details())
    assert Club.objects.get().current_directory_observed_at is None
    Importer(season, now).apply("clubs", "", {"Club": [details()]})
    assert Club.objects.get().current_directory_observed_at == now


@pytest.mark.django_db
def test_historical_logo_fills_absent_reference_without_replacing_current(
    season: Season,
) -> None:
    """Past-period logo resources belong to the shared global metadata lane."""
    now = timezone.now()
    historical = Importer(season, now, discover=False)
    historical.club(details())
    assert Club.objects.get().logo_hash == "ABC1"
    assert SyncResource.objects.filter(kind="club_logo").count() == 1
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_details", "C1", details(digest="ABC2")
    )
    historical.club(details(digest="ABC3"))
    assert Club.objects.get().logo_hash == "ABC2"


@pytest.mark.django_db
def test_equal_newer_logo_fences_delayed_changed_reference(season: Season) -> None:
    """An unchanged current logo observation still proves which reference is newer."""
    now = timezone.now()
    Club.objects.create(external_id="C1", name="Example")
    Importer(season, now).apply("club_details", "C1", details(digest="ABC1"))
    Importer(season, now + timedelta(seconds=2)).apply(
        "club_details", "C1", details(digest="ABC1")
    )
    checkpoint = SyncResource.objects.get(kind="club_logo")
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_details", "C1", details(digest="ABC2")
    )
    club = Club.objects.get()
    checkpoint.refresh_from_db()
    assert club.logo_hash == "ABC1"
    assert (
        club.metadata_observations["logo"] == (now + timedelta(seconds=2)).isoformat()
    )


@pytest.mark.django_db
def test_equal_newer_contact_fences_delayed_changed_values(season: Season) -> None:
    """Dedicated public metadata uses its latest successful value observation."""
    now = timezone.now()
    Club.objects.create(external_id="CT1", name="Example")
    Importer(season, now).apply("club_contact", "CT1", CONTACT)
    Importer(season, now + timedelta(seconds=2)).apply("club_contact", "CT1", CONTACT)
    Importer(season, now + timedelta(seconds=1)).apply(
        "club_contact", "CT1", {**CONTACT, "Website": "https://older.example"}
    )
    club = Club.objects.get()
    assert club.contact["website"] == "http://www.club.example"
    assert (
        club.metadata_observations["contact"]
        == (now + timedelta(seconds=2)).isoformat()
    )


@pytest.mark.django_db
def test_equal_newer_summary_catalogue_values_fence_delayed_changes(
    season: Season,
) -> None:
    """Nested summaries advance field ordering without dirtying source fixtures."""
    now = timezone.now()
    row = match_payload()
    row["RoundNr"] = 1
    Importer(season, now).apply("club_results", "CT1", {"MatchResult": [row]})
    before = Match.objects.get()
    Importer(season, now + timedelta(seconds=2)).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    after = Match.objects.get()
    assert after.updated_at == before.updated_at
    assert after.revisions.count() == 1
    assert (
        after.source_context["fields"]["RoundNr"]["observed_at"]
        == (now + timedelta(seconds=2)).isoformat()
    )
    Importer(season, now + timedelta(seconds=1)).club({
        "ClubId": "CT1",
        "ClubName": "Delayed old name",
    })
    club = Club.objects.get(external_id="CT1")
    assert club.name == "Club T1"
    assert (
        club.metadata_observations["name"] == (now + timedelta(seconds=2)).isoformat()
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "colors", [{}, {"HomeOutfit": {"Shirt": None, "Shorts": None, "Stocking": None}}]
)
def test_blank_summary_colors_preserve_rich_dedicated_observation(
    season: Season, colors: dict
) -> None:
    """An abbreviated catalogue row does not establish an authoritative kit removal."""
    now = timezone.now()
    Club.objects.create(external_id="C1", name="Example")
    Importer(season, now).apply("club_details", "C1", details())
    Importer(season, now + timedelta(seconds=1)).club({
        "ClubId": "C1",
        "ClubName": "Example",
        "ClubColors": colors,
    })
    assert Club.objects.get().colors == {"HomeOutfit": {"Shirt": "Red"}}


@pytest.mark.django_db
def test_structured_contact_sports_and_empty_observation(season: Season) -> None:
    """Keep structured IDs and current empty outcomes without storing own contacts."""
    Club.objects.create(external_id="CT1", name="Example")
    now = timezone.now()
    importer = Importer(season, now)
    importer.apply("club_contact", "CT1", CONTACT)
    importer.apply("club_sports", "CT1", SPORTS)
    club = Club.objects.get()
    assert club.contact["facility"]["id"] == "F1"
    assert club.sport_definitions[0] == {
        "id": "KORFBALL-FIT-WK",
        "description": "KombiFit Week",
    }
    assert club.sports == ["KombiFit Week", "Veld Week"]
    assert "06-00000000" not in str(club.contact)
    assert "secretary@club.example" not in str(club.contact)
    newer = Importer(season, now + timedelta(days=1))
    newer.apply("club_contact", "CT1", {"ClubId": "CT1", "Facility": None})
    newer.apply("club_sports", "CT1", {"ClubId": "CT1", "ClubSport": []})
    importer.apply("club_contact", "CT1", CONTACT)
    importer.apply("club_sports", "CT1", SPORTS)
    club.refresh_from_db()
    assert (club.contact, club.sports, club.sport_definitions) == ({}, [], [])
    assert set(club.metadata_observations) >= {"contact", "sports", "sport_definitions"}


@pytest.mark.django_db
def test_logo_reference_change_during_image_validation_is_rejected(
    season: Season,
) -> None:
    """An old digest cannot become the current cached image after validation I/O."""
    club = Importer(season, timezone.now(), discover=False).club(details())
    name = logo_name(club.logo_bucket, club.logo_hash)

    def changed_reference(*args: object) -> str:
        Club.objects.filter(pk=club.pk).update(logo_hash="ABC2")
        return name

    with (
        patch(
            "apps.competition.services.logos.store_image", side_effect=changed_reference
        ),
        pytest.raises(ValueError, match="Stale club logo"),
    ):
        cache_logo("C1", {"name": name})
    club.refresh_from_db()
    assert not club.cached_logo


@pytest.mark.django_db
def test_dissolution_is_monotonic_and_keeps_native_name_and_upload(
    season: Season,
) -> None:
    """Only linked source-dissolved/native-active candidates need a status write."""
    local = NativeClub.objects.create(
        name="Reviewed name", logo="club_pictures/manual.png"
    )
    active = NativeClub.objects.create(name="Source name")
    source = Club.objects.create(
        external_id="C1", name="Source name", dissolved=True, local_club=local
    )
    publisher = Publisher(None)
    publisher.clubs()
    local.refresh_from_db()
    active.refresh_from_db()
    assert (local.dissolved, local.name, local.logo.name) == (
        True,
        "Reviewed name",
        "club_pictures/manual.png",
    )
    assert not active.dissolved
    assert publisher.counts["clubs_dissolved"] == 1
    with CaptureQueriesContext(connection) as queries:
        Publisher(None).clubs()
    assert not [query for query in queries if query["sql"].split()[0] == "UPDATE"]
    source.dissolved = False
    source.save(update_fields=("dissolved",))
    Publisher(None).clubs()
    local.refresh_from_db()
    assert local.dissolved
