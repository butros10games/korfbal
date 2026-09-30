"""Find referee whistles in a recording's sound track.

ffmpeg measures, every tenth of a second, how loud the whistle band
(2.5-4.5 kHz) is and how loud the whole signal is. A whistle is loud in that
band and dominates the signal there; crowd noise and voices spread their
energy lower. Only the sound is decoded, so an hour of video takes about a
minute and no pixels are processed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import subprocess
import tempfile

from .media import binary


WINDOW_SECONDS = 0.1
SAMPLE_RATE = 8000
BAND = (2500, 4500)
# The band must hold most of the sound: at most this many dB below the whole.
DOMINANCE_DB = 4.0
# ...and be among the loudest moments of the recording.
LOUDNESS_PERCENTILE = 0.9
MIN_SECONDS = 0.2
MERGE_GAP_SECONDS = 0.3
TIMEOUT_SECONDS = 1800

_FRAME = re.compile(r"pts_time:([0-9.]+)")
_LEVEL = re.compile(r"RMS_level=(-?[0-9.]+|-inf)")


@dataclass(frozen=True, slots=True)
class Whistle:
    """One whistle: where it starts in the video, how long, and how clear."""

    seconds: float
    duration: float
    strength: float


def parse_levels(text: str) -> dict[float, float]:
    """Parse ffmpeg ``ametadata=print`` output into window start -> RMS dB.

    Returns:
        The level of every window.

    """
    levels: dict[float, float] = {}
    at: float | None = None
    for line in text.splitlines():
        frame = _FRAME.search(line)
        if frame:
            at = round(float(frame.group(1)), 2)
            continue
        level = _LEVEL.search(line)
        if level and at is not None:
            value = level.group(1)
            levels[at] = -math.inf if value == "-inf" else float(value)
    return levels


def detect(band: dict[float, float], full: dict[float, float]) -> list[Whistle]:
    """Turn per-window levels into whistles.

    Returns:
        Whistles in video order.

    """
    finite = sorted(level for level in band.values() if math.isfinite(level))
    if not finite:
        return []
    loud = finite[min(len(finite) - 1, int(len(finite) * LOUDNESS_PERCENTILE))]
    hits = [
        (at, band[at] - loud)
        for at in sorted(band)
        if math.isfinite(band[at])
        and band[at] >= loud
        and band[at] >= full.get(at, math.inf) - DOMINANCE_DB
    ]
    whistles: list[Whistle] = []
    start: float | None = None
    last = 0.0
    peak = 0.0
    for at, margin in hits:
        if start is not None and at - last <= MERGE_GAP_SECONDS + 1e-9:
            last = at
            peak = max(peak, margin)
            continue
        if start is not None:
            whistles.append(_whistle(start, last, peak))
        start, last, peak = at, at, margin
    if start is not None:
        whistles.append(_whistle(start, last, peak))
    return [whistle for whistle in whistles if whistle.duration >= MIN_SECONDS]


def _whistle(start: float, last: float, peak: float) -> Whistle:
    return Whistle(
        seconds=round(start, 2),
        duration=round(last - start + WINDOW_SECONDS, 2),
        strength=round(peak, 2),
    )


def _filter(output: Path) -> str:
    samples = int(SAMPLE_RATE * WINDOW_SECONDS)
    meter = (
        f"asetnsamples={samples}:p=0,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level"
    )
    return (
        f"[0:a]aresample={SAMPLE_RATE},pan=mono|c0=c0,asplit[band][full];"
        f"[band]highpass=f={BAND[0]},lowpass=f={BAND[1]},"
        f"{meter}:file={output / 'band.txt'};"
        f"[full]{meter}:file={output / 'full.txt'}"
    )


def find_whistles(source: str) -> list[Whistle]:
    """Decode a recording's sound (path or HTTPS URL) and find its whistles.

    Returns:
        Whistles in video order.

    """
    protocols = "file,https,tls,tcp" if source.startswith("https://") else "file"
    with tempfile.TemporaryDirectory(prefix="whistles-") as temporary:
        output = Path(temporary)
        subprocess.run(
            [
                binary("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                protocols,
                "-vn",
                "-i",
                source,
                "-filter_complex",
                _filter(output),
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
        band = parse_levels((output / "band.txt").read_text())
        full = parse_levels((output / "full.txt").read_text())
    return detect(band, full)
