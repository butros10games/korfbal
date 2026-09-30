"""Referee whistles in the sound track, for editors syncing a match video."""

from http import HTTPStatus
from pathlib import Path
import shutil
import subprocess

from django.test.client import Client
import pytest

from apps.kwt_common.models import BackgroundJob
from apps.schedule.tests import match_api_test_support as support
from apps.video_analysis.engine.whistles import (
    Whistle,
    detect,
    find_whistles,
    parse_levels,
)
from apps.video_analysis.models import MatchVideoPublication
from apps.video_analysis.services import match_video
from apps.video_analysis.tests.test_match_video import FakeUrls, link_recording


def test_whistles_are_loud_band_dominated_runs() -> None:
    """Crowd noise spreads its energy; a whistle fills the band for a moment."""
    band = {round(index / 10, 2): -60.0 for index in range(100)}
    full = dict.fromkeys(band, -30.0)
    for at in (2.0, 2.1, 2.2, 2.3, 2.5):  # one whistle with a short gap
        band[at], full[at] = -20.0, -18.0
    band[5.0], full[5.0] = -20.0, -18.0  # too short
    for at in (7.0, 7.1, 7.2):  # loud but mostly other sound
        band[at], full[at] = -20.0, -5.0
    whistles = detect(band, full)
    assert whistles == [Whistle(seconds=2.0, duration=0.6, strength=40.0)]
    assert detect({}, {}) == []


def test_ffmpeg_levels_are_parsed() -> None:
    """Each window prints one frame line and one level line."""
    text = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.astats.Overall.RMS_level=-42.5\n"
        "frame:1    pts:800     pts_time:0.1\n"
        "lavfi.astats.Overall.RMS_level=-inf\n"
    )
    assert parse_levels(text) == {0.0: -42.5, 0.1: float("-inf")}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_whistle_tone_is_found_in_real_audio(tmp_path: Path) -> None:
    """A three-kilohertz tone over noise is found at its moment."""
    source = tmp_path / "match.wav"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=6:c=pink:a=0.02:r=8000",
            "-f",
            "lavfi",
            "-i",
            "sine=f=3200:d=0.8:r=8000",
            "-filter_complex",
            "[1]adelay=3000,volume=4[w];[0][w]amix=inputs=2:duration=first",
            str(source),
        ],
        check=True,
    )
    whistles = find_whistles(str(source))
    assert [round(whistle.seconds) for whistle in whistles] == [3]


@pytest.mark.django_db
def test_editors_start_a_search_and_only_they_see_the_whistles(client: Client) -> None:
    """The search runs in the background; viewers never receive the whistles."""
    graph = support.create_match_graph(prefix="whistles")
    recording = link_recording(graph)
    support.login_coach(client, graph, username="whistle-coach")
    url = f"/api/matches/{graph.match.id_uuid}/video/whistles/"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "apps.schedule.api.match_viewset_video.match_video_urls", FakeUrls
        )
        started = client.post(url)
        assert started.status_code == HTTPStatus.ACCEPTED
        assert started.json()["video"]["whistles_status"] == "queued"
        assert BackgroundJob.objects.filter(queue="vision").count() == 1
        # Asking again while queued does not queue a second search.
        client.post(url)
        assert BackgroundJob.objects.filter(queue="vision").count() == 1

        match_video.store_whistles(
            recording.pk,
            urls=FakeUrls(),
            find=lambda _source: [Whistle(seconds=12.3, duration=0.4, strength=9.0)],
        )
        video = client.get(f"/api/matches/{graph.match.id_uuid}/video/").json()["video"]
        assert video["whistles_status"] == "done"
        assert video["whistles"] == [
            {"seconds": 12.3, "duration": 0.4, "strength": 9.0}
        ]

        MatchVideoPublication.objects.filter(recording=recording).update(published=True)
        public = Client().get(f"/api/matches/{graph.match.id_uuid}/video/").json()
        assert "whistles" not in public["video"]
        assert Client().post(url).status_code in {
            HTTPStatus.FORBIDDEN,
            HTTPStatus.UNAUTHORIZED,
        }


@pytest.mark.django_db
def test_a_failed_search_is_reported(client: Client) -> None:
    """Editors see that the search failed and can start it again."""
    graph = support.create_match_graph(prefix="whistles-fail")
    recording = link_recording(graph)
    MatchVideoPublication.objects.create(recording=recording, whistles_status="queued")

    def broken(_source: str) -> list[Whistle]:
        raise RuntimeError("ffmpeg failed")

    match_video.store_whistles(recording.pk, urls=FakeUrls(), find=broken)
    assert MatchVideoPublication.objects.get().whistles_status == "failed"


@pytest.mark.django_db
def test_a_playback_url_failure_leaves_the_search_retryable() -> None:
    """Storage failures must not leave editors polling a running search forever."""
    graph = support.create_match_graph(prefix="whistles-url-fail")
    recording = link_recording(graph)
    MatchVideoPublication.objects.create(recording=recording, whistles_status="queued")

    class BrokenUrls(FakeUrls):
        def playback_url(self, recording: object) -> str:
            raise RuntimeError("storage unavailable")

    match_video.store_whistles(recording.pk, urls=BrokenUrls(), find=lambda _source: [])
    assert MatchVideoPublication.objects.get().whistles_status == "failed"
    match_video.request_whistles(graph.match)
    assert MatchVideoPublication.objects.get().whistles_status == "queued"
