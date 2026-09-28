"""One bounded upload policy shared by all audio creation entry points."""

from pathlib import Path
import re

from django.core.files.uploadedfile import UploadedFile


MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024
MP3_ID3_TAG = b"ID3"
MPEG_FRAME_SYNC = re.compile(rb"\xff[\xe0-\xff]")


class InvalidAudioUploadError(ValueError):
    """The submitted file is outside the supported audio upload contract."""


def validate_audio_upload(uploaded: UploadedFile) -> None:
    """Reject unsupported names and sizes before storage or job dispatch.

    Raises:
        InvalidAudioUploadError: The upload violates the media policy.

    """
    if Path(uploaded.name or "").suffix.strip().lower() != ".mp3":
        raise InvalidAudioUploadError("Only MP3 uploads are supported.")
    content_type = (uploaded.content_type or "").lower()
    if content_type and content_type not in {"audio/mpeg", "audio/mp3"}:
        raise InvalidAudioUploadError("Invalid content type (expected MP3).")
    if not uploaded.size or uploaded.size > MAX_AUDIO_UPLOAD_BYTES:
        raise InvalidAudioUploadError("Audio must contain between 1 byte and 25 MB.")
    if not _looks_like_mp3(uploaded):
        raise InvalidAudioUploadError("The file is not a valid MP3.")


def _looks_like_mp3(uploaded: UploadedFile) -> bool:
    """Check the content, not the client-controlled name and content type."""
    position = uploaded.tell() if uploaded.seekable() else None
    header = uploaded.read(3)
    if position is not None:
        uploaded.seek(position)
    return header.startswith(MP3_ID3_TAG) or bool(MPEG_FRAME_SYNC.match(header))
