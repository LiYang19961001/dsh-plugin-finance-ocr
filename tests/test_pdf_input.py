# -*- coding: utf-8 -*-
"""PDF 输入层单元测试（图片型 PDF → 逐页 PNG）。"""

import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import pdf_input  # noqa: E402

try:
    import pymupdf
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        pymupdf = None


def make_pdf(path: Path, pages: int = 2) -> None:
    document = pymupdf.open()
    try:
        for index in range(1, pages + 1):
            page = document.new_page(width=300, height=400)
            page.insert_text((40, 60), f"test page {index}", fontsize=20)
        document.save(str(path))
    finally:
        document.close()


@unittest.skipUnless(pymupdf is not None, "需要 PyMuPDF")
class PdfInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.pdf = self.root / "sample.pdf"
        make_pdf(self.pdf, pages=3)

    def tearDown(self):
        self.temp.cleanup()

    def test_render_pages_in_order(self):
        pages = pdf_input.render_pdf(self.pdf, dpi=150, root=self.cache)
        self.assertEqual(len(pages), 3)
        self.assertEqual([page.name for page in pages], ["page-001.png", "page-002.png", "page-003.png"])
        for page in pages:
            self.assertGreater(page.stat().st_size, 500)

    def test_render_cache_reused(self):
        first = pdf_input.render_pdf(self.pdf, dpi=150, root=self.cache)
        second = pdf_input.render_pdf(self.pdf, dpi=150, root=self.cache)
        self.assertEqual(first, second)

    def test_expand_inputs_mixed_image_pdf(self):
        image = self.root / "one.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # 占位文件，不做 OCR，只验证展开顺序
        pages, originals = pdf_input.expand_inputs([image, self.pdf], dpi=120, root=self.cache)
        self.assertEqual(len(originals), 2)
        self.assertEqual(pages[0], image)
        self.assertEqual(len(pages), 4)

    def test_unsupported_suffix(self):
        bad = self.root / "note.txt"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaises(pdf_input.PdfInputError):
            pdf_input.expand_material(bad)

    def test_is_pdf(self):
        self.assertTrue(pdf_input.is_pdf(self.pdf))
        self.assertFalse(pdf_input.is_pdf(self.root / "a.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
