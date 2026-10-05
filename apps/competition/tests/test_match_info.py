"""Public match information from imported KNKV venue, rules and club colours."""

from typing import Any

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.models import Club, Match
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
)


COLORS = {
    "HomeOutfit": {"Shirt": "Rood-Zwart", "Shorts": "Zwart", "Stocking": None},
    "AwayOutfit": {"Shirt": None, "Shorts": None, "Stocking": None},
    "ReserveOutfit": {"Shirt": "Grijs-Zwart", "Shorts": " Zwart ", "Stocking": ""},
}
FACILITY = {
    "FacilityName": "Sportpark Voorbeeld",
    "Address": "Voorbeeldlaan 3",
    "ZipCode": "0000AA",
    "City": "VOORBEELD",
    "PhoneNumber": "000-0000000",
    "SubFacilityName": "Veld 3",
    "FieldType": "kunstgras zand",
    "ClubInformationUrl": None,
    "HomeDressingRoom": "",
    "AwayDressingRoom": "2",
    "OfficialDressingRoom": "",
    "AssemblyTime": None,
}
RULES = {
    "Category": [
        {
            "Name": "ALGEMEEN",
            "Info": [
                {"Key": "Wedstrijdnummer", "Value": "12616"},
                {"Key": "Competitie", "Value": ""},
            ],
        },
        {"Name": "TEAM", "Info": [{"Key": "Minimum spelers", "Value": "6"}]},
        {"Name": "LEEG", "Info": []},
    ],
    "ClassExtraAttributes": {"MandatoryIdentification": True},
}


def published_match(season: Season) -> NativeMatch:
    """Import a fixture with club colours and details, then publish it natively."""
    row: dict[str, Any] = match_payload()
    row["HomeTeam"]["Club"]["ClubColors"] = COLORS
    now = timezone.now()
    importer = Importer(season, now)
    importer.apply("club_results", "CT1", {"MatchResult": [row]})
    importer.apply("match_facility", "M1", FACILITY)
    importer.apply("match_rules", "M1", RULES)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    native = Match.objects.get().local_match
    assert native is not None
    return native


@pytest.mark.django_db
def test_club_colors_keep_filled_outfits_only(season: Season) -> None:
    """Empty outfits and parts are dropped; values are trimmed."""
    published_match(season)
    assert Club.objects.get(external_id="CT1").colors == {
        "HomeOutfit": {"Shirt": "Rood-Zwart", "Shorts": "Zwart"},
        "ReserveOutfit": {"Shirt": "Grijs-Zwart", "Shorts": "Zwart"},
    }
    assert Club.objects.get(external_id="CT2").colors == {}


@pytest.mark.django_db
def test_club_colors_clear_when_provider_removes_outfits(season: Season) -> None:
    """An explicit empty colour payload replaces kits from an earlier poll."""
    published_match(season)
    data = {"ClubId": "CT1", "ClubName": "Example"}
    Importer(season, timezone.now()).club(data)
    assert Club.objects.get(external_id="CT1").colors
    Importer(season, timezone.now()).apply(
        "club_details", "CT1", {**data, "ClubColors": {}}
    )
    assert Club.objects.get(external_id="CT1").colors == {}


@pytest.mark.django_db
def test_match_info_endpoint(season: Season) -> None:
    """Expose the venue, labelled categories and kits without internal rules."""
    native = published_match(season)
    response = APIClient().get(f"/api/matches/{native.pk}/info/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "venue": {
            "name": "Sportpark Voorbeeld",
            "address": "Voorbeeldlaan 3",
            "postal_code": "0000AA",
            "city": "VOORBEELD",
            "phone": "000-0000000",
            "field": "Veld 3",
            "surface": "kunstgras zand",
            "visitor_info_url": None,
            "dressing_rooms": {"home": None, "away": "2", "officials": None},
        },
        "sections": [
            {
                "title": "Algemeen",
                "items": [{"label": "Wedstrijdnummer", "value": "12616"}],
            },
            {"title": "Team", "items": [{"label": "Minimum spelers", "value": "6"}]},
        ],
        "kits": {
            "home": [
                {
                    "kind": "home",
                    "shirt": "Rood-Zwart",
                    "shorts": "Zwart",
                    "socks": None,
                },
                {
                    "kind": "reserve",
                    "shirt": "Grijs-Zwart",
                    "shorts": "Zwart",
                    "socks": None,
                },
            ],
            "away": [],
        },
    }


@pytest.mark.django_db
def test_match_info_without_knkv_source(season: Season) -> None:
    """Matches created in the app have no provider information."""
    native = published_match(season)
    Match.objects.update(local_match=None)
    response = APIClient().get(f"/api/matches/{native.pk}/info/")
    assert response.json() == {
        "venue": None,
        "sections": [],
        "kits": {"home": [], "away": []},
    }
