import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from shared.scraped_image import (
    SCRAPED_IMAGE_EXT,
    normalize_scraped_image_to_jpeg,
    save_scraped_gallery_image,
    sniff_image_format,
)


class ScrapedImageTests(unittest.TestCase):
    def test_sniff_jpeg_magic(self):
        buf = BytesIO()
        Image.new("RGB", (8, 8), color=(255, 0, 0)).save(buf, format="JPEG")
        self.assertEqual(sniff_image_format(buf.getvalue()), "jpeg")

    def test_sniff_webp_magic(self):
        buf = BytesIO()
        Image.new("RGB", (8, 8), color=(0, 255, 0)).save(buf, format="WEBP")
        self.assertEqual(sniff_image_format(buf.getvalue()), "webp")

    def test_normalize_webp_bytes_to_jpeg(self):
        src = BytesIO()
        Image.new("RGB", (12, 12), color=(0, 0, 255)).save(src, format="WEBP")
        out = normalize_scraped_image_to_jpeg(src.getvalue(), content_type="image/webp")
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        with Image.open(BytesIO(out)) as im:
            self.assertEqual(im.format, "JPEG")

    def test_save_scraped_gallery_image_writes_jpg(self):
        src = BytesIO()
        Image.new("RGBA", (10, 10), color=(255, 0, 0, 128)).save(src, format="WEBP")
        with TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images"
            rel = save_scraped_gallery_image(
                src.getvalue(),
                images_dir,
                "bulk_uv_re_60433488",
                1,
                content_type="image/webp",
            )
            self.assertEqual(rel, f"images/bulk_uv_re_60433488_01{SCRAPED_IMAGE_EXT}")
            path = images_dir / "bulk_uv_re_60433488_01.jpg"
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
