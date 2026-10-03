"""Small WebP copies of stored logos and profile pictures.

Clients draw logos and avatars at most 80 CSS pixels wide, so a 256-pixel copy
stays sharp on three-times displays at a fraction of the uploaded size. Copies
are made on first request and stored beside their original under a derived
key, so they share its immutable, owner-scoped location.
"""

from __future__ import annotations

from io import BytesIO
import warnings

from PIL import Image, ImageOps

from apps.player.application.ports import ImageVariantStorage
from apps.player.media_paths import VARIANT_SIZE, variant_key


VARIANT_CONTENT_TYPE = "image/webp"
_WEBP_QUALITY = 80
# Anonymous requests can trigger a resize, so bound what is decoded. Uploads and
# imported logos stay far below these limits.
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PIXELS = 25_000_000


class VariantBusyError(Exception):
    """Another request is creating this copy; serve the original meanwhile."""


def _resized(original: bytes) -> bytes | None:
    if len(original) > MAX_SOURCE_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(original)) as image:
                width, height = image.size
                if width * height > MAX_SOURCE_PIXELS:
                    return None
                # JPEGs decode directly at a reduced scale.
                image.draft(None, (VARIANT_SIZE * 2, VARIANT_SIZE * 2))
                image.thumbnail((VARIANT_SIZE, VARIANT_SIZE), Image.Resampling.LANCZOS)
                small = ImageOps.exif_transpose(image)
                transparent = (
                    small.mode in {"RGBA", "LA", "PA"} or "transparency" in small.info
                )
                small = small.convert("RGBA" if transparent else "RGB")
                encoded = BytesIO()
                small.save(encoded, "WEBP", quality=_WEBP_QUALITY, method=4)
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return encoded.getvalue()


def image_variant(storage: ImageVariantStorage, key: str) -> bytes | None:
    """Return the WebP copy of a stored image, creating it on first use.

    Returns None when the original is not a readable image within the limits,
    so callers can serve the original instead.

    Raises:
        FileNotFoundError: The original is missing or was deleted meanwhile; a
            copy never outlives its original.
        VariantBusyError: Another request is creating the copy right now.

    """
    if not storage.exists(key):
        raise FileNotFoundError(key)
    variant = variant_key(key)
    try:
        return storage.read(variant)
    except FileNotFoundError:
        pass
    if not storage.claim(variant):
        raise VariantBusyError(key)
    try:
        resized = _resized(storage.read(key))
        if resized is None:
            return None
        storage.write(variant, resized)
        # Deletion removes the original before its copy. A copy written after
        # that would otherwise keep withdrawn bytes reachable.
        if not storage.exists(key):
            storage.delete(variant)
            raise FileNotFoundError(key)
        return resized
    finally:
        storage.release(variant)
