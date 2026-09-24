"""Measure return-to-original-ID latency on explicitly reviewed recovery windows."""

import math
from statistics import median


TIME_TOLERANCE = 1e-6


def recovery_metrics(windows: list[dict], observations: list[dict]) -> dict:
    """Report sampled latency bounds and unresolved windows, never hide failures.

    Windows name the reviewed last-visible time, first-visible-again time and
    review end. An absent reference between those times is not a training negative.

    Raises:
        ValueError: A window lacks ordered times or reviewed boundary observations.

    """
    results: list[dict] = []
    for window in windows:
        start, visible, end = (
            window[k] for k in ("last_visible", "visible_again", "end")
        )
        if (
            not all(math.isfinite(t) for t in (start, visible, end))
            or not start < visible <= end
        ):
            raise ValueError("Recovery windows require ordered finite times")
        selected = [
            sample
            for sample in observations
            if sample["identity"] == window["track_id"]
            and sample["label"] == window.get("label", "player")
            and sample["segment"] == window.get("segment", 0)
        ]
        before = next(
            (s for s in selected if abs(s["time"] - start) < TIME_TOLERANCE), None
        )
        after = [
            s
            for s in selected
            if visible - TIME_TOLERANCE <= s["time"] <= end + TIME_TOLERANCE
        ]
        if (
            before is None
            or not after
            or abs(after[0]["time"] - visible) > TIME_TOLERANCE
        ):
            raise ValueError(
                "Recovery windows need reviewed last/first visible boundaries"
            )
        if abs(after[-1]["time"] - end) > TIME_TOLERANCE:
            raise ValueError("Recovery windows need a reviewed end boundary")
        original = before["track"]
        recovered = next(
            (s for s in after if original and s["track"] == original), None
        )
        previous = [
            s["time"] for s in after if recovered and s["time"] < recovered["time"]
        ]
        detected = next((s for s in after if s["track"] is not None), None)
        results.append({
            **window,
            "status": "no_prior_match"
            if original is None
            else "recovered"
            if recovered
            else "unresolved",
            "latency_seconds": recovered["time"] - visible if recovered else None,
            "latency_lower_bound_seconds": max(previous, default=visible) - visible
            if recovered
            else None,
            "first_detection_seconds": detected["time"] - visible if detected else None,
            "wrong_id_observations": sum(
                s["track"] is not None and s["track"] != original for s in after
            )
            if original
            else None,
            "reviewed_observations": len(after),
        })
    latencies = [r["latency_seconds"] for r in results if r["status"] == "recovered"]
    return {
        "windows": results,
        "total": len(results),
        "recovered": len(latencies),
        "unresolved": sum(r["status"] == "unresolved" for r in results),
        "no_prior_match": sum(r["status"] == "no_prior_match" for r in results),
        "median_latency_seconds": median(latencies) if latencies else None,
        "max_latency_seconds": max(latencies) if latencies else None,
        "sampled_bounds": True,
    }
