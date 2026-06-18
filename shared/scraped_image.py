"""Normalize scraped product gallery bytes to JPEG on disk (WebP/PNG → .jpg)."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

SCRAPED_IMAGE_EXT = ".jpg"
SCRAPED_IMAGE_MIME = "image/jpeg"


def sniff_image_format(content: bytes) -> str | None:
    """Return a coarse format hint: jpeg, png, webp, gif."""
    if len(content) >= 3 and content[0:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(content) >= 8 and content[0:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if len(content) >= 6 and content[0:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def normalize_scraped_image_to_jpeg(
    content: bytes,
    *,
    content_type: str | None = None,
) -> bytes:
    """
    Convert downloaded gallery bytes to JPEG.

    Always returns JPEG bytes suitable for products.json paths ending in .jpg.
    Falls back to raw bytes only when Pillow cannot decode (caller should still use .jpg name
    only when conversion succeeds).
    """
    if not content:
        raise ValueError("empty image content")

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to normalize scraped images") from exc

    with Image.open(BytesIO(content)) as raw:
        if raw.mode in ("RGBA", "LA"):
            rgba = raw.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
            rgb.paste(rgba, mask=rgba.split()[-1])
            im = rgb
        elif raw.mode == "P" and "transparency" in raw.info:
            rgba = raw.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
            rgb.paste(rgba, mask=rgba.split()[3])
            im = rgb
        else:
            im = raw.convert("RGB")

        buf = BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()


def save_scraped_gallery_image(
    content: bytes,
    images_dir: Path,
    base_prefix: str,
    index: int,
    *,
    content_type: str | None = None,
) -> str | None:
    """
    Write one normalized JPEG under images_dir.

    Returns relative catalog path, e.g. images/foo_01.jpg, or None on failure.
    """
    try:
        jpeg = normalize_scraped_image_to_jpeg(content, content_type=content_type)
    except Exception:
        return None

    fname = f"{base_prefix}_{index:02d}{SCRAPED_IMAGE_EXT}"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / fname).write_bytes(jpeg)
    return f"images/{fname}"
