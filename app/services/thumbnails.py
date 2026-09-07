import base64
import binascii
import io
import os
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SOURCE_BYTES = int(os.environ.get("THUMBNAIL_MAX_SOURCE_BYTES", 50 * 1024**2))
MAX_SOURCE_PIXELS = int(os.environ.get("THUMBNAIL_MAX_SOURCE_PIXELS", 50_000_000))
MAX_ENCODED_CHARACTERS = 4 * ((MAX_SOURCE_BYTES + 2) // 3)
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF")


class InvalidThumbnailImage(ValueError):
    pass


class ThumbnailTooLarge(ValueError):
    pass


def normalize_thumbnail(thumbnail_b64str: str) -> str:
    if len(thumbnail_b64str) > MAX_ENCODED_CHARACTERS:
        raise ThumbnailTooLarge

    try:
        raw = base64.b64decode(thumbnail_b64str, validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise InvalidThumbnailImage from exc

    if len(raw) > MAX_SOURCE_BYTES:
        raise ThumbnailTooLarge

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=ALLOWED_FORMATS) as source_image:
                if source_image.width * source_image.height > MAX_SOURCE_PIXELS:
                    raise ThumbnailTooLarge
                source_image.seek(0)
                oriented = ImageOps.exif_transpose(source_image)
                if oriented.mode in ("RGBA", "LA") or (
                    oriented.mode == "P" and "transparency" in oriented.info
                ):
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    normalized = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    normalized = oriented.convert("RGB")
                normalized.thumbnail((512, 512), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                normalized.save(buffer, format="JPEG", quality=85)
    except ThumbnailTooLarge:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ThumbnailTooLarge from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidThumbnailImage from exc

    return base64.b64encode(buffer.getvalue()).decode("ascii")
