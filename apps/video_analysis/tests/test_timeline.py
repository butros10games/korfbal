"""Active-play edits and image preparation across the native review boundary."""

from http import HTTPStatus
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.media import prepare_active_frames
from apps.video_analysis.engine.store import Store
from apps.video_analysis.engine.timeline import (
    is_active_time,
    sample_times,
    validate_periods,
)
from apps.video_analysis.models import Frame, Recording
from apps.video_analysis.tests.test_review import verified


pytestmark = pytest.mark.django_db


def test_eyecons_import_waits_for_timeline(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """A new native Eyecons recording starts with video and zero review images."""
    _, store, _ = imported

    def download(_source: dict, destination: Path) -> None:
        destination.write_bytes(b"synthetic recording")

    with (
        patch(
            "apps.video_analysis.management.commands.import_eyecons_recording.discover",
            return_value={
                "title": "Synthetic match",
                "source_url": "https://eyecons.com/videos/synthetic",
                "external_id": "12345678",
            },
        ),
        patch(
            "apps.video_analysis.management.commands.import_eyecons_recording.download",
            side_effect=download,
        ),
        patch(
            "apps.video_analysis.engine.media.probe",
            return_value={
                "duration_seconds": 4000,
                "width": 1280,
                "height": 720,
                "fps": "25/1",
            },
        ),
    ):
        call_command(
            "import_eyecons_recording",
            "https://eyecons.com/videos/synthetic",
            id="new-match",
        )
    match = next(item for item in store.read()["matches"] if item["id"] == "new-match")
    assert match["frames"] == []
    assert match["timeline_required"] is True
    assert store.media(match["video"]).read_bytes() == b"synthetic recording"


def test_intervals_exclude_halftime_and_pauses() -> None:
    """Sampling restarts after every break and never reaches its end boundary."""
    periods = validate_periods(
        [{"start": 100, "end": 121}, {"start": 300, "end": 321}], 400
    )
    assert sample_times(periods, 10) == [100, 110, 120, 300, 310, 320]
    assert is_active_time({"active_periods": periods}, 120)
    assert not is_active_time({"active_periods": periods}, 121)
    assert not is_active_time({"active_periods": [], "timeline_required": True}, 100)
    with pytest.raises(ValueError, match="overlap"):
        validate_periods([{"start": 100, "end": 200}, {"start": 199, "end": 300}], 400)
    with pytest.raises(ValueError, match="number between"):
        validate_periods([{"start": 100, "end": 401}], 400)


def test_saved_timeline_prepares_only_active_images_and_keeps_reviews(
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Persist a versioned cut and extract only its source-video seconds."""
    owner, store, _ = imported
    recording = Recording.objects.get(source_id="demo")
    recording.metadata = {
        **recording.metadata,
        "video": "demo/recording.mp4",
        "duration_seconds": 4000,
    }
    recording.save(update_fields=["metadata"])
    (store.root / "demo/recording.mp4").write_bytes(b"synthetic recording")
    original = list(
        Frame.objects.filter(recording=recording).values_list("source_id", flat=True)
    )
    client = verified(owner)
    state = client.get("/video-analysis/state").json()
    payload = {
        "revision": state["revision"],
        "match_id": "demo",
        "active_periods": [
            {"start": 100, "end": 121},
            {"start": 300, "end": 321},
        ],
    }
    response = client.post(
        "/video-analysis/timeline",
        json.dumps(payload),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=state["csrf"],
    )
    assert response.status_code == HTTPStatus.OK
    assert (
        client.post(
            "/video-analysis/timeline",
            json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        ).status_code
        == HTTPStatus.CONFLICT
    )
    with patch(
        "apps.video_analysis.api.views.schedule",
        return_value=Mock(pk="synthetic-job"),
    ) as schedule:
        queued = client.post(
            "/video-analysis/prepare",
            json.dumps({"match_id": "demo", "interval": 10}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=state["csrf"],
        )
    assert queued.status_code == HTTPStatus.ACCEPTED
    assert schedule.call_args.args[2:] == (
        "prepare",
        {
            "match_id": "demo",
            "interval": 10,
            "active_periods": payload["active_periods"],
        },
    )
    with pytest.raises(ValueError, match="Timeline changed"):
        prepare_active_frames(store, "demo", 10, [{"start": 0, "end": 10}])

    def extract(command: list[str], **kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"synthetic frame")

    with (
        patch("apps.video_analysis.engine.media.binary", side_effect=lambda name: name),
        patch("apps.video_analysis.engine.media.subprocess.run", side_effect=extract),
    ):
        assert prepare_active_frames(store, "demo", 10) == {"added": 6, "selected": 6}
        assert prepare_active_frames(store, "demo", 10) == {"added": 0, "selected": 6}
    recording.refresh_from_db()
    assert recording.metadata["active_periods"] == payload["active_periods"]
    assert (
        recording.metadata["match_start_seconds"]
        == payload["active_periods"][0]["start"]
    )
    frames = list(Frame.objects.filter(recording=recording).order_by("position"))
    assert set(original).issubset({frame.source_id for frame in frames})
    added = [
        frame.metadata["time_seconds"]
        for frame in frames
        if frame.source_id not in original
    ]
    assert added == [100, 110, 120, 300, 310, 320]
