"""Lossless MP4 repackaging so recordings open quickly in the browser.

Streaming-site downloads interleave audio and video every frame or two, so the
MP4 index lists hundreds of thousands of chunks and a full match needs a 10+ MB
index before the first frame. Regrouping the same packets into one-second
chunks roughly halves that index without touching a single encoded byte.
"""

from collections.abc import Callable
from pathlib import Path
import struct
import subprocess

from .media import binary


CHUNK_MICROSECONDS = 1_000_000
# Regrouped files have about one chunk per second per track.
MAX_COMPACT_CHUNKS_PER_SECOND = 4
HEADER_BYTES = 16
MIN_BOX_BYTES = 8
FULL_BOX_HEADER = 4
MDHD_V1 = 1
CONTAINERS = {b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """List child boxes as ``(type, payload_start, box_end)``.

    Returns:
        The boxes between ``start`` and ``end``.

    """
    found, offset = [], start
    while offset + MIN_BOX_BYTES <= end:
        size, kind = struct.unpack(">I4s", data[offset : offset + MIN_BOX_BYTES])
        header = MIN_BOX_BYTES
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = HEADER_BYTES
        elif size == 0:
            size = end - offset
        if size < header:
            break
        found.append((kind, offset + header, offset + size))
        offset += size
    return found


def _video_chunks(moov: bytes) -> tuple[int, float] | None:
    """Count the video track's chunks and its duration from an index.

    Returns:
        ``(chunks, seconds)``, or None when the index has no video track.

    """
    for kind, start, end in _boxes(moov, 0, len(moov)):
        if kind != b"trak":
            continue
        tables: dict[bytes, tuple[int, int]] = {}
        pending = [(start, end)]
        while pending:
            first, last = pending.pop()
            for child, child_start, child_end in _boxes(moov, first, last):
                if child in CONTAINERS:
                    pending.append((child_start, child_end))
                else:
                    tables[child] = (child_start, child_end)
        if b"hdlr" not in tables or b"mdhd" not in tables:
            continue
        handler = tables[b"hdlr"][0]
        if moov[handler + 8 : handler + 12] != b"vide":
            continue
        offsets = tables.get(b"stco") or tables.get(b"co64")
        if offsets is None:
            return None
        chunks = struct.unpack(">I", moov[offsets[0] + 4 : offsets[0] + 8])[0]
        media = tables[b"mdhd"][0]
        if moov[media] == MDHD_V1:
            scale, duration = struct.unpack(">IQ", moov[media + 20 : media + 32])
        else:
            scale, duration = struct.unpack(">II", moov[media + 12 : media + 20])
        return chunks, duration / scale if scale else 0.0
    return None


def index_is_compact(read: Callable[[int, int], bytes], size: int) -> bool:
    """Check the front index with a few ranged reads, never the media itself.

    Returns:
        Whether the file already starts with a regrouped index.

    """
    offset = 0
    while offset < size:
        header = read(offset, min(size, offset + HEADER_BYTES) - 1)
        length, kind = struct.unpack(">I4s", header[:MIN_BOX_BYTES])
        if length == 1:
            length = struct.unpack(">Q", header[8:HEADER_BYTES])[0]
        elif length == 0:
            length = size - offset
        if kind == b"mdat" or length < MIN_BOX_BYTES:
            return False
        if kind == b"moov":
            counted = _video_chunks(read(offset, offset + length - 1)[MIN_BOX_BYTES:])
            if counted is None:
                return False
            chunks, seconds = counted
            return chunks <= max(1.0, seconds) * MAX_COMPACT_CHUNKS_PER_SECOND
        offset += length
    return False


def repackage(source: Path, target: Path) -> None:
    """Copy every stream into one-second chunks with the index first."""
    subprocess.run(
        [
            binary("ffmpeg"),
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-chunk_duration",
            str(CHUNK_MICROSECONDS),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


def _packets(path: Path) -> str:
    """Describe every packet's stream, timing and keyframe flag.

    Returns:
        ffprobe's packet table.

    """
    return subprocess.run(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "packet=stream_index,pts_time,dts_time,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _stream_hashes(path: Path) -> str:
    """Hash every stream's encoded packets without decoding them.

    Returns:
        One hash line per stream.

    """
    return subprocess.run(
        [
            binary("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def verify_repackaged(source: Path, target: Path) -> None:
    """Require identical packets and timing, so annotations cannot shift.

    Raises:
        ValueError: The repackaged file is not an exact copy of the streams.

    """
    if _stream_hashes(source) != _stream_hashes(target):
        raise ValueError("Repackaging changed encoded media")
    if sorted(_packets(source).splitlines()) != sorted(_packets(target).splitlines()):
        raise ValueError("Repackaging changed packet timing")
