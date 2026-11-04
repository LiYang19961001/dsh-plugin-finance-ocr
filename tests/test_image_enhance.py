# -*- coding: utf-8 -*-
"""影像增强模块单元测试（不跑 OCR，只测算法/缓存/降级）。"""

import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import image_enhance  # noqa: E402


class ImageEnhanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temp.cleanup()

    def make_gradient_image(self, path: Path) -> None:
        height, width = 600, 800
        gradient = np.tile(np.linspace(120, 240, width, dtype=np.uint8), (height, 1))
        image = cv2.cvtColor(gradient, cv2.COLOR_GRAY2BGR)
        cv2.putText(image, "TEST", (100, 300), cv2.FONT_HERSHEY_SIMPLEX, 3, (20, 20, 20), 8)
        cv2.imencode(".png", image)[1].tofile(str(path))

    def test_enhance_array_shape_and_dtype(self):
        image = np.full((400, 300, 3), 200, dtype=np.uint8)
        enhanced = image_enhance.enhance_array(image)
        self.assertEqual(enhanced.shape, image.shape)
        self.assertEqual(enhanced.dtype, np.uint8)

    def test_enhance_file_cache_reused(self):
        source = self.root / "scan.png"
        self.make_gradient_image(source)
        first = image_enhance.enhance_file(source, self.cache)
        second = image_enhance.enhance_file(source, self.cache)
        self.assertEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertIn("enhanced-", first.name)

    def test_enhance_file_missing_returns_original(self):
        missing = self.root / "missing.png"
        resolved = image_enhance.enhance_file(missing, self.cache)
        self.assertEqual(resolved, missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
