# -*- coding: utf-8 -*-
"""PDF（图片型/扫描件）逐页渲染成 PNG，供 OCR 与版式识别层使用。

- 只处理本机文件，不上传；
- 渲染缓存目录默认 `%USERPROFILE%\\.dsh\\finance-ocr\\pdf-cache`，按"PDF 内容哈希 + DPI"分目录，重复使用不重复渲染；
- 默认 250 DPI（扫描件 OCR 足够；过高只会变慢，过低会损失小字）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_DPI = 250
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


class PdfInputError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_pdf(path) -> bool:
    return Path(str(path)).suffix.lower() in PDF_SUFFIXES


def is_supported(path) -> bool:
    return Path(str(path)).suffix.lower() in SUPPORTED_SUFFIXES


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_root() -> Path:
    return Path.home() / ".dsh" / "finance-ocr" / "pdf-cache"


def render_pdf(path, dpi: int = DEFAULT_DPI, root=None) -> list[Path]:
    """把 PDF 每一页渲染成 PNG，返回按页序排列的图片路径列表（带缓存）。"""
    path = Path(path).expanduser()
    if not path.is_file():
        raise PdfInputError("EXTRACTION_NOT_FOUND", f"PDF 不存在：{path}")
    digest = sha256_file(path)[:12]
    out_dir = Path(root).expanduser() if root else cache_root()
    pages_dir = out_dir / f"{digest}-{int(dpi)}"
    existing = sorted(pages_dir.glob("page-*.png"))
    if existing:
        return existing
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 兼容旧版 PyMuPDF
        except ImportError:
            raise PdfInputError(
                "PDF_ENGINE_MISSING",
                "本机缺少 PDF 渲染库，请安装：python -m pip install pymupdf",
            )
    pages_dir.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(str(path))
    try:
        if getattr(document, "needs_pass", False):
            raise PdfInputError("PDF_ENCRYPTED", f"PDF 已加密，需要先解密：{path}")
        if document.page_count == 0:
            raise PdfInputError("PDF_EMPTY", f"PDF 没有页面：{path}")
        requested_zoom = float(dpi) / 72.0
        for index, page in enumerate(document, start=1):
            # 纯图片页按"嵌入图原生分辨率"封顶：高于原生分辨率的渲染不会增加信息，只会变慢
            page_zoom = requested_zoom
            if not (page.get_text() or "").strip():
                native_dpi = 0.0
                page_width_inch = page.rect.width / 72.0
                if page_width_inch > 0:
                    for image in page.get_images(full=True):
                        try:
                            info = document.extract_image(image[0])
                        except Exception:
                            continue
                        native_dpi = max(native_dpi, float(info.get("width") or 0) / page_width_inch)
                if native_dpi > 0:
                    page_zoom = min(requested_zoom, max(1.0, native_dpi * 1.02 / 72.0))
            matrix = pymupdf.Matrix(page_zoom, page_zoom)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=pymupdf.csRGB)
            pixmap.save(str(pages_dir / f"page-{index:03d}.png"))
    finally:
        document.close()
    pages = sorted(pages_dir.glob("page-*.png"))
    if not pages:
        raise PdfInputError("PDF_EMPTY", f"PDF 渲染失败：{path}")
    return pages


def expand_material(path, dpi: int = DEFAULT_DPI, root=None) -> list[Path]:
    """一张图片/一个 PDF（可能多页）→ 按顺序的页面图片列表。"""
    path = Path(path).expanduser()
    if not path.is_file():
        raise PdfInputError("EXTRACTION_NOT_FOUND", f"文件不存在：{path}")
    if not is_supported(path):
        raise PdfInputError(
            "BAD_INPUT",
            f"只支持图片（{'/'.join(sorted(IMAGE_SUFFIXES))}）或 PDF：{path}",
        )
    if is_pdf(path):
        return render_pdf(path, dpi=dpi, root=root)
    return [path]


def expand_inputs(paths: list, dpi: int = DEFAULT_DPI, root=None) -> tuple[list[Path], list[Path]]:
    """多组输入（可能混合图片/PDF）→ (展开后的页面图片列表, 原始输入列表)。"""
    pages: list[Path] = []
    originals: list[Path] = []
    for item in paths:
        path = Path(item).expanduser()
        originals.append(path)
        pages.extend(expand_material(path, dpi=dpi, root=root))
    return pages, originals
