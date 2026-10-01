"""Public club information from the club-level KNKV endpoints."""

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.models import Club, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.tests.test_match_info import published_match
from apps.schedule.models import Season


CONTACT = {
    "ClubId": "CT1",
    "Website": " http://www.club.example ",
    "Founded": "1918-11-10",
    "PhoneNumber": "06-00000000",
    "Email": "secretary@club.example",
    "Facility": {
        "FacilityId": "F1",
        "FacilityName": "Sportpark Voorbeeld",
        "Address": "Voorbeeldlaan 3",
        "ZipCode": "0000AA",
        "City": "VOORBEELD",
        "PhoneNumber": "000-0000000",
    },
}
SPORTS = {
    "ClubId": "CT1",
    "ClubSport": [
        {"SportId": "KORFBALL-FIT-WK", "SportDescription": "KombiFit Week"},
        {"SportId": "KORFBALL-VE-WK", "SportDescription": "Veld Week"},
        {"SportId": "KORFBALL-VE-WK", "SportDescription": "Veld Week"},
        {"SportId": "KORFBALL-X", "SportDescription": ""},
    ],
}


@pytest.mark.django_db
def test_club_feeds_are_discovered_and_imported(season: Season) -> None:
    """Never store the club's own phone or email; keep the venue's phone."""
    published_match(season)
    assert set(
        SyncResource.objects.filter(
            kind__in=("club_contact", "club_sports")
        ).values_list("kind", "source_id")
    ) == {
        ("club_contact", "CT1"),
        ("club_contact", "CT2"),
        ("club_sports", "CT1"),
        ("club_sports", "CT2"),
    }
    importer = Importer(season, timezone.now())
    importer.apply("club_contact", "CT1", CONTACT)
    importer.apply("club_sports", "CT1", SPORTS)
    club = Club.objects.get(external_id="CT1")
    assert club.contact == {
        "website": "http://www.club.example",
        "founded": "1918-11-10",
        "facility": {
            "name": "Sportpark Voorbeeld",
            "address": "Voorbeeldlaan 3",
            "postal_code": "0000AA",
            "city": "VOORBEELD",
            "phone": "000-0000000",
        },
    }
    assert club.sports == ["KombiFit Week", "Veld Week"]
    with pytest.raises(ValueError, match="Unrecognized"):
        importer.apply("club_contact", "CT2", CONTACT)


@pytest.mark.django_db
def test_club_info_endpoint(season: Season) -> None:
    """Expose website, founding date, venue, sports and kits in one read."""
    published_match(season)
    importer = Importer(season, timezone.now())
    importer.apply("club_contact", "CT1", CONTACT)
    importer.apply("club_sports", "CT1", SPORTS)
    local = Club.objects.get(external_id="CT1").local_club
    assert local is not None
    response = APIClient().get(f"/api/club/clubs/{local.pk}/info/")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["website"] == "http://www.club.example"
    assert body["founded"] == "1918-11-10"
    assert body["venue"]["name"] == "Sportpark Voorbeeld"
    assert body["venue"]["phone"] == "000-0000000"
    assert body["activities"] == ["KombiFit Week", "Veld Week"]
    assert [outfit["kind"] for outfit in body["kits"]] == ["home", "reserve"]
    assert "06-00000000" not in response.content.decode()


@pytest.mark.django_db
def test_club_info_without_knkv_source(season: Season) -> None:
    """Clubs created in the app have no provider information."""
    published_match(season)
    local = Club.objects.get(external_id="CT2").local_club
    assert local is not None
    Club.objects.filter(external_id="CT2").update(local_club=None)
    response = APIClient().get(f"/api/club/clubs/{local.pk}/info/")
    assert response.json() == {
        "website": None,
        "founded": None,
        "venue": None,
        "activities": [],
        "kits": [],
    }
