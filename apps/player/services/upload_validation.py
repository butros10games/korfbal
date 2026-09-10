"""One bounded upload policy shared by all audio creation entry points."""

from pathlib import Path

from django.core.files.uploadedfile import UploadedFile


MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024


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
