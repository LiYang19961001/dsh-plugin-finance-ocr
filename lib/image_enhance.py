# -*- coding: utf-8 -*-
"""影像增强：拍照式扫描件去阴影 + 对比度增强 + 去噪，供 OCR/表格识别前预处理。

设计原则：只在自动识别失败时作为"重试路径"使用，不改变正常清晰扫描件的识别结果。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np


def _read_image(path) -> "np.ndarray | None":
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _write_image(path: Path, image: "np.ndarray") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"图片编码失败：{path}")
    buffer.tofile(str(path))


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enhance_array(image: "np.ndarray") -> "np.ndarray":
    """去阴影（背景估计除法）+ CLAHE 对比度增强，返回 BGR 图。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, _ = gray.shape
    kernel_size = max(15, (int(height * 0.04) | 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    background = np.maximum(background, 1)
    normalized = cv2.divide(gray, background, scale=255.0)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(normalized)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def enhance_file(path, cache_root) -> Path:
    """增强一张图片并缓存；返回增强后的图片路径（失败时返回原图）。"""
    path = Path(path)
    if not path.is_file():
        return path
    cache_dir = Path(cache_root)
    target = cache_dir / f"enhanced-{sha256_file(path)[:12]}.png"
    if target.is_file():
        return target
    image = _read_image(path)
    if image is None:
        return path
    enhanced = enhance_array(image)
    try:
        _write_image(target, enhanced)
    except Exception:
        return path
    return target
