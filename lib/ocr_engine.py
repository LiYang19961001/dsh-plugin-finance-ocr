# -*- coding: utf-8 -*-
"""RapidOCR 封装：图片 → 文本行（含原图 bbox / 置信度），全程本地离线推理。

对外接口（lib/CONTRACT.md 第 1、3 节）：
- run_ocr(image_path) -> {
      "lines": [{"line_id": int, "text": str, "confidence": float, "bbox": [x1,y1,x2,y2]}],
      "engine": "rapidocr_onnxruntime", "engine_version": "x.y.z",
      "mean_confidence": float,
      "n_lines": int, "width": int, "height": int,   # 后三项为便利字段，CLI 可直接用
  }
- is_available() -> bool
- engine_info() -> {"name", "version", "available", "package_dir", ...}
- get_engine() / reset_engine()：惰性初始化并复用 RapidOCR 实例。

设计要点：
1. 自适应解析不同版本的返回形态：
   - 旧版：engine(img) -> (result, elapse)，result = [[box, text, score], ...]（rapidocr_onnxruntime 1.x）
   - 新版：engine(img) -> 带 .boxes / .txts / .scores 的对象（rapidocr v2 的 RapidOCROutput）
   - 以及 {"boxes": ..., "txts": ..., "scores": ...} 的 dict 形态
2. OCR 前把长边缩放到 <= MAX_LONG_EDGE 提速，但 bbox 会按缩放比映射回**原图像素坐标**。
3. 绝不把整段文字拼成一个字符串再切：每一行保留检测框 bbox 与置信度。
4. 行按 (y_center, x1) 稳定排序后编号 line_id（0 起），同一输入结果确定。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import tempfile
import time
from pathlib import Path

from PIL import Image

ENGINE_PACKAGES = ("rapidocr_onnxruntime", "rapidocr")
MAX_LONG_EDGE = 2400


# ---------------------------------------------------------------------------
# 异常：CLI 侧按 CONTRACT.md 第 6 节映射 error.code
# ---------------------------------------------------------------------------
class OcrError(RuntimeError):
    """OCR 相关错误的基类。"""

    code = "OCR_FAILED"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NoOcrEngine(OcrError):
    """本地 OCR 引擎未安装 / 不可用（NO_OCR_ENGINE）。"""

    code = "NO_OCR_ENGINE"


# ---------------------------------------------------------------------------
# 引擎加载（惰性、单例）
# ---------------------------------------------------------------------------
_engine = None
_engine_module = None


def _load_engine_module():
    """按优先级加载本地 OCR 包；失败抛 NoOcrEngine。"""
    global _engine_module
    if _engine_module is not None:
        return _engine_module
    errors = []
    for name in ENGINE_PACKAGES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # ImportError / DLL load failed 等
            errors.append(f"{name}: {exc}")
            continue
        if not hasattr(module, "RapidOCR"):
            errors.append(f"{name}: 缺少 RapidOCR 类")
            continue
        _engine_module = module
        return module
    raise NoOcrEngine(
        "本地 OCR 引擎不可用（离线要求，禁止改用云 OCR）。"
        "请先安装 rapidocr_onnxruntime。详细错误：" + "; ".join(errors)
    )


def _current_engine_meta() -> tuple[str, str]:
    """返回 (包名, 版本号)，不触发引擎实例化。"""
    try:
        module = _engine_module if _engine_module is not None else _load_engine_module()
    except NoOcrEngine:
        return "unknown", "unknown"
    name = str(getattr(module, "__name__", "rapidocr_onnxruntime")).split(".")[0]
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = str(getattr(module, "__version__", "unknown"))
    return name, version


def is_available() -> bool:
    try:
        _load_engine_module()
        return True
    except NoOcrEngine:
        return False


def engine_info() -> dict:
    """selftest 用：引擎是否存在、版本、包目录。"""
    info = {
        "name": None,
        "version": "unknown",
        "available": False,
        "package_dir": None,
        "max_long_edge": MAX_LONG_EDGE,
    }
    try:
        module = _load_engine_module()
    except NoOcrEngine as exc:
        info["error"] = str(exc)
        return info
    name, version = _current_engine_meta()
    info["name"] = name
    info["engine"] = name  # cli.py selftest 读取 info.get("engine") 展示
    info["version"] = version
    info["available"] = True
    file_path = getattr(module, "__file__", None)
    info["package_dir"] = str(Path(file_path).resolve().parent) if file_path else None
    return info


def get_engine():
    """惰性创建并复用 RapidOCR 实例（模型随包自带，初始化后离线可跑）。"""
    global _engine
    if _engine is None:
        module = _load_engine_module()
        engine_class = getattr(module, "RapidOCR")
        try:
            _engine = engine_class()
        except Exception as exc:
            raise OcrError(f"初始化本地 OCR 引擎失败: {exc}") from exc
    return _engine


def reset_engine() -> None:
    """丢弃缓存的引擎实例（测试用）。"""
    global _engine
    _engine = None


# ---------------------------------------------------------------------------
# 图片读取 / 缩放 / 坐标映射
# ---------------------------------------------------------------------------
def _open_image(image_path) -> Image.Image:
    path = os.fspath(image_path)
    if not os.path.isfile(path):
        raise OcrError(f"图片不存在或不可读: {path}")
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:
        raise OcrError(f"无法读取图片: {path}: {exc}") from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _coerce_points(value) -> list[tuple[float, float]]:
    """把 box 统一成 [(x, y), ...]；兼容 numpy 数组 / 嵌套 list / 扁平 list。"""
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise ValueError("bbox 不是序列")
    if value and all(isinstance(item, (int, float)) for item in value):
        if len(value) % 2 != 0:
            raise ValueError("扁平 bbox 长度不是偶数")
        return [(float(value[i]), float(value[i + 1])) for i in range(0, len(value), 2)]
    points = []
    for item in value:
        if hasattr(item, "tolist"):
            item = item.tolist()
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append((float(item[0]), float(item[1])))
    if not points:
        raise ValueError("bbox 没有有效点")
    return points


def _clamp_int(value: float, upper: int) -> int:
    result = int(round(value))
    if result < 0:
        return 0
    if result > upper:
        return upper
    return result


def _bbox_to_original(points, scale_x: float, scale_y: float,
                      width: int, height: int) -> list[int] | None:
    """检测框（缩放图坐标）→ 原图像素坐标 [x1,y1,x2,y2]（整数）。"""
    try:
        pts = _coerce_points(points)
    except (TypeError, ValueError):
        return None
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x1 = _clamp_int(min(xs) * scale_x, width)
    x2 = _clamp_int(max(xs) * scale_x, width)
    y1 = _clamp_int(min(ys) * scale_y, height)
    y2 = _clamp_int(max(ys) * scale_y, height)
    if x2 <= x1:
        x2 = min(width, x1 + 1)
    if y2 <= y1:
        y2 = min(height, y1 + 1)
    return [x1, y1, x2, y2]


# ---------------------------------------------------------------------------
# 返回形态自适应解析
# ---------------------------------------------------------------------------
def _is_item(value) -> bool:
    """[box, text, score] 形态？"""
    if hasattr(value, "tolist"):
        value = value.tolist()
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 3
        and isinstance(value[1], str)
    )


def _looks_like_items(value) -> bool:
    if hasattr(value, "tolist") and not isinstance(value, dict):
        return False
    if not isinstance(value, (list, tuple)) or not value:
        return False
    return _is_item(value[0])


def _is_number_seq(value) -> bool:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, (int, float)) for item in value)
    )


def _looks_like_elapse(value) -> bool:
    if value is None:
        return True
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        return False
    if not value:
        return True
    # rapidocr_onnxruntime 1.4.4 的 elapse 是扁平 [det, cls, rec] 浮点列表；
    # 某些版本是 [[det, cls, rec, all], ...] 的嵌套列表，两种都接受。
    if all(isinstance(item, (int, float)) for item in value):
        return True
    return all(_is_number_seq(item) for item in value)


def _is_output_object(value) -> bool:
    return (
        not isinstance(value, (dict, list, tuple))
        and any(hasattr(value, attr) for attr in ("boxes", "txts", "scores"))
    )


def _to_plain(value):
    if hasattr(value, "tolist") and not isinstance(value, dict):
        try:
            return value.tolist()
        except Exception:
            return value
    return value


def _zip_items(boxes, txts, scores) -> list[tuple]:
    boxes = _to_plain(boxes)
    txts = _to_plain(txts)
    scores = _to_plain(scores)
    if boxes is None:
        return []
    boxes = list(boxes) if not isinstance(boxes, list) else boxes
    txts = list(txts) if txts is not None else []
    scores = list(scores) if scores is not None else []
    if not txts:
        return []
    items = []
    for i in range(min(len(boxes), len(txts))):
        score = scores[i] if i < len(scores) else 0.0
        items.append((boxes[i], txts[i], score))
    return items


def _extract_items(raw) -> list[tuple]:
    """把任意版本的 OCR 返回统一成 [(box, text, score), ...]。"""
    if raw is None:
        return []

    # 形态 0：dict {"boxes": ..., "txts": ..., "scores": ...}
    if isinstance(raw, dict):
        return _zip_items(raw.get("boxes"), raw.get("txts"), raw.get("scores"))

    # 形态 1：旧版引擎返回 (result, elapse)（tuple/list，第一项是结果集合）
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and not _looks_like_items(raw):
        first, second = raw
        if _looks_like_items(first) and (
            _looks_like_elapse(second) or not _looks_like_items(second)
        ):
            return _extract_items(first)
        if first is None and _looks_like_elapse(second):
            return []

    # 形态 2：新版对象（RapidOCROutput，带 .boxes/.txts/.scores）
    if _is_output_object(raw):
        return _zip_items(
            getattr(raw, "boxes", None),
            getattr(raw, "txts", None),
            getattr(raw, "scores", None),
        )

    # 形态 3：对象自带的 to_dict()/to_json()
    if not isinstance(raw, (dict, list, tuple)):
        to_dict = getattr(raw, "to_dict", None)
        if callable(to_dict):
            try:
                converted = to_dict()
            except Exception:
                converted = None
            if converted is not None:
                return _extract_items(converted)
        return []

    raw = _to_plain(raw)

    # 形态 4：[[box, text, score], ...]
    if _looks_like_items(raw):
        items = []
        for item in raw:
            if hasattr(item, "tolist"):
                item = item.tolist()
            if isinstance(item, (list, tuple)) and len(item) >= 3 and item[1] is not None:
                items.append((item[0], item[1], item[2]))
        return items

    # 兜底：逐项尝试
    items = []
    if isinstance(raw, list):
        for item in raw:
            if hasattr(item, "tolist"):
                item = item.tolist()
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                items.append((item[0], item[1], item[2]))
    return items


def _clean_text(text) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            text = text.decode("gbk", "replace")
    return str(text).strip()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _yc(line: dict) -> float:
    x1, y1, x2, y2 = line["bbox"]
    return (y1 + y2) / 2.0


def run_ocr(image_path, max_long_edge: int = MAX_LONG_EDGE) -> dict:
    """图片 → OCR 文本行（含原图坐标 bbox、置信度、稳定 line_id）。"""
    started_at = time.perf_counter()
    image = _open_image(image_path)
    width, height = image.size

    original_path = os.fspath(image_path)
    ocr_input = original_path
    scale_x = scale_y = 1.0
    temp_path = None
    long_edge = max(width, height)
    if max_long_edge and long_edge > max_long_edge:
        ratio = float(max_long_edge) / float(long_edge)
        new_width = max(1, int(round(width * ratio)))
        new_height = max(1, int(round(height * ratio)))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        resized = image.resize((new_width, new_height), resampling)
        fd, temp_path = tempfile.mkstemp(prefix="dsh_finance_ocr_", suffix=".png")
        os.close(fd)
        try:
            resized.save(temp_path, format="PNG")
        except Exception as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise OcrError(f"OCR 前缩放图保存失败: {exc}") from exc
        ocr_input = temp_path
        scale_x = width / float(new_width)
        scale_y = height / float(new_height)

    try:
        engine = get_engine()
        try:
            raw = engine(ocr_input)
        except Exception as exc:
            raise OcrError(f"OCR 推理失败: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    items = _extract_items(raw)
    collected = []
    for points, text, score in items:
        clean = _clean_text(text)
        if not clean:
            continue
        bbox = _bbox_to_original(points, scale_x, scale_y, width, height)
        if bbox is None:
            continue
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        collected.append({
            "text": clean,
            "confidence": round(confidence, 4),
            "bbox": bbox,
        })

    # 稳定排序：先按 y 中心，再按左边界、上边界、文本
    collected.sort(key=lambda line: (_yc(line), line["bbox"][0], line["bbox"][1], line["text"]))
    lines = [
        {
            "line_id": index,
            "text": line["text"],
            "confidence": line["confidence"],
            "bbox": line["bbox"],
        }
        for index, line in enumerate(collected)
    ]

    mean_confidence = (
        round(sum(line["confidence"] for line in lines) / len(lines), 4) if lines else 0.0
    )
    engine_name, engine_version = _current_engine_meta()
    return {
        "lines": lines,
        "engine": engine_name,
        "engine_version": engine_version,
        "mean_confidence": mean_confidence,
        "n_lines": len(lines),
        "width": width,
        "height": height,
        # 耗时只用于命令行信封/日志，不写入 extraction（保证 extraction 确定性）
        "elapsed_ms": int(round((time.perf_counter() - started_at) * 1000)),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(json.dumps(engine_info(), ensure_ascii=False, indent=2))
        raise SystemExit(2)
    print(json.dumps(run_ocr(sys.argv[1]), ensure_ascii=False, indent=2))
