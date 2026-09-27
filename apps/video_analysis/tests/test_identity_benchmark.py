"""Identity ground truth is labelled independently and scores a run's identity swaps."""

from typing import Any

from django.contrib.auth.models import User
import pytest

from apps.video_analysis.adapters.store import DatabaseStore
from apps.video_analysis.engine.store import Store, atomic_json
from apps.video_analysis.models import AnalysisJob, Frame, Workspace
from apps.video_analysis.services import identity_benchmark


pytestmark = pytest.mark.django_db

OPTIONS = {"start": 10.0, "duration": 4.0, "fps": 12.5}


def person(track: str, x: float, team: str = "team_a") -> dict[str, Any]:
    """One tracked player box in a clip frame."""
    box = [x, 0.3, 0.05, 0.3]
    return {
        "label": "player",
        "track_id": track,
        "bbox": box,
        "observed_bbox": box,
        "confidence": 0.9,
        "team": team,
    }


def clip_run(store: DatabaseStore, owner: User, swap_at: float | None) -> str:
    """Create a completed two-player run whose track IDs swap at ``swap_at``."""
    job = AnalysisJob.objects.create(
        workspace_id=store.workspace_id,
        requested_by=owner,
        kind="clip",
        status="completed",
        payload={"match_id": "demo", "model": "remote-test", "options": OPTIONS},
    )
    frames = []
    for n in range(50):
        time = round(10 + n * 0.08, 3)
        swapped = swap_at is not None and time >= swap_at
        frames.append({
            "time_seconds": time,
            "objects": [
                person("t2" if swapped else "t1", 0.2),
                person("t1" if swapped else "t2", 0.6, "team_b"),
            ],
        })
    root = store.root / "vision/clips" / str(job.pk)
    atomic_json(root / "chunk-00000.json", {"frames": frames})
    atomic_json(
        root / "run.json",
        {"status": "completed", "chunks": [{"name": "chunk-00000.json"}]},
    )
    return str(job.pk)


def fake_extract(_store: Store, match: dict, times: list[float]) -> list[dict]:
    """Stand in for ffmpeg: the rows extraction would publish."""
    return [
        {
            "id": f"at-{round(t * 1000):09d}",
            "time_seconds": t,
            "source_time_seconds": t,
            "image": f"{match['id']}/at-{round(t * 1000):09d}.jpg",
        }
        for t in times
    ]


@pytest.fixture
def benchmark(
    imported: tuple[User, DatabaseStore, Store], monkeypatch: pytest.MonkeyPatch
) -> tuple[DatabaseStore, Workspace, str]:
    """Label a two-player benchmark created from an identity-stable run."""
    owner, store, _ = imported
    monkeypatch.setattr(identity_benchmark, "extract_pipeline_frames", fake_extract)
    workspace = Workspace.objects.get()
    source = clip_run(store, owner, swap_at=None)
    created = identity_benchmark.create(store, workspace, source, "bench", 2.0)
    assert created == {"name": "bench", "match_id": "demo", "frames": 2}
    for frame in Frame.objects.filter(metadata__identity_benchmark__name="bench"):
        assert frame.proposal is not None
        # Drafts keep boxes and teams but never the tracker's identities.
        assert all("track_id" not in o for o in frame.proposal["objects"])
        assert [o["team"] for o in frame.proposal["objects"]] == ["team_a", "team_b"]
        frame.correction = {
            **frame.proposal,
            "objects": [
                {**frame.proposal["objects"][0], "track_id": "A7"},
                {**frame.proposal["objects"][1], "track_id": "B12"},
            ],
        }
        frame.status, frame.complete = "approved", True
        frame.save()
    return store, workspace, source


def test_a_stable_run_scores_perfectly(
    benchmark: tuple[DatabaseStore, Workspace, str],
) -> None:
    """Every labelled person keeps one tracker identity throughout."""
    store, workspace, source = benchmark
    report = identity_benchmark.evaluate(store, workspace, "bench", source)
    assert report["keyframes"] == 2  # noqa: PLR2004
    assert report["people"] == 2  # noqa: PLR2004
    assert report["idf1"] == pytest.approx(1.0)
    assert report["id_switches"] == 0


def test_a_swapped_run_counts_each_identity_switch(
    benchmark: tuple[DatabaseStore, Workspace, str],
    imported: tuple[User, DatabaseStore, Store],
) -> None:
    """Two players trading tracker IDs between keyframes is two switches."""
    store, workspace, _ = benchmark
    swapped = clip_run(store, imported[0], swap_at=11.0)
    report = identity_benchmark.evaluate(store, workspace, "bench", swapped)
    assert report["id_switches"] == 2  # noqa: PLR2004
    assert report["idf1"] == pytest.approx(0.5)


def test_unlabelled_people_and_off_grid_spacing_are_rejected(
    benchmark: tuple[DatabaseStore, Workspace, str],
) -> None:
    """Evaluation needs an ID on every person; labels must sit on analyzed frames."""
    store, workspace, source = benchmark
    frame = Frame.objects.filter(metadata__identity_benchmark__name="bench").first()
    assert frame is not None
    assert frame.correction is not None
    frame.correction["objects"][1].pop("track_id")
    frame.save()
    with pytest.raises(ValueError, match="give every player"):
        identity_benchmark.evaluate(store, workspace, "bench", source)
    with pytest.raises(ValueError, match="whole number of frames"):
        identity_benchmark.sample_times(OPTIONS, 1.0)
    with pytest.raises(ValueError, match="already exists"):
        identity_benchmark.create(store, workspace, source, "bench", 2.0)


def test_sample_times_stay_inside_the_clip() -> None:
    """A 4 s clip labelled every 2 s has keyframes at its start and middle."""
    assert identity_benchmark.sample_times(OPTIONS, 2.0) == [10.0, 12.0]
