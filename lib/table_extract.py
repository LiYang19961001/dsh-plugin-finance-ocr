# -*- coding: utf-8 -*-
"""表格结构还原：RapidOCR 文本行 → extraction 文档（lib/CONTRACT.md 第 3 节）。

输入：ocr_engine.run_ocr() 的结果（lines 含 bbox/confidence/line_id）与 source_meta。
输出：严格符合契约第 3 节字段名的 extraction dict（columns / header_line / rows /
total_rows / skipped_lines / warnings ...），可 json.dumps(ensure_ascii=False)。

规则（不依赖表格线检测，表格线/倾斜照片是已知限制）：
1. 先按 y 中心把 OCR 行聚成"视觉行带"（同一行文本 y 中心差 < 行高一半）。
2. 表头识别：某一行带里命中 >= 2 个表头关键词 → header_line；按表头文字的 x 区间
   做列带（首列从 0、末列到图宽，列间取相邻表头框中点），列带覆盖整图宽度。
3. 每个 OCR 行按 x 中心/重叠率归列；一行跨多列时按 token 字符位置插值拆分到多个单元格
   （契约允许"一个 OCR 行被合并进多个单元格"，line_id 覆盖集合只算一次）。
4. 明细行带里按 y 聚类成行；以 合计/总计/小计 开头且带金额框的行进 total_rows。
5. 行数守恒按 line_id 覆盖集合校验：
      rows/total_rows 里所有单元格的 line_id ∪ skipped_lines 的 line_id == 全部 OCR 行。
   表头行本身进 skipped_lines（reason=header），表头上方文字进 reason=title/above_header。
6. 金额类 value 清洗成纯十进制字符串（去 ￥/¥/$/千分位/空格，负数保留 -），不做舍入；
   编号类保留原始字符（仅去空白）；文本类去空格。raw 永远保留 OCR 原文。

已知限制（写进完成说明）：
- 不检测表格线；无表头（<2 个关键词）或无坐标信息的图片无法还原；
- 表头整行被 OCR 合并成一个框时，列带按字符比例估算，可能偏移；
- 倾斜/透视拍照、跨页表格、多栏嵌套表未处理。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = "finance-extract/1"
DEFAULT_PLUGIN_VERSION = "0.1.0"
TEMPLATE_AUTO = "auto"

# ---------------------------------------------------------------------------
# 字段词典（契约第 1 节 / 任务描述）；dict 的插入顺序即字段优先级。
# 关键词匹配按"长关键词优先"，避免「价税合计」被「金额」抢走。
# ---------------------------------------------------------------------------
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "seq": ("序号", "序"),
    "date": ("开票日期", "记账日期", "日期"),
    "invoice_code": ("发票代码",),
    "invoice_no": ("发票号码", "票据号码"),
    "voucher_no": ("凭证编号", "凭证号"),
    "summary": ("费用项目", "摘要", "项目", "内容", "用途", "事由", "品名"),
    "spec": ("规格型号", "规格", "型号"),
    "unit": ("单位",),
    "qty": ("数量",),
    "unit_price": ("单价",),
    "amount": ("金额",),
    "tax_rate": ("税率",),
    "tax": ("税额",),
    "total": ("价税合计", "合计金额", "金额合计"),
    "debit": ("借方金额", "借方"),
    "credit": ("贷方金额", "贷方"),
    "account": ("会计科目", "科目"),
    "person": ("经办人", "报销人", "制单"),
    "remark": ("备注",),
}

AMOUNT_FIELDS = frozenset({"amount", "tax", "total", "debit", "credit", "unit_price"})
ID_FIELDS = frozenset({"invoice_code", "invoice_no", "voucher_no", "account"})
RIGHT_ALIGN_FIELDS = frozenset(AMOUNT_FIELDS | {"seq", "qty", "tax_rate"})

TOTAL_LABEL_PREFIXES = ("合计", "总计", "小计")
TOTAL_DEFAULT_LABEL = "合计"

# 一行内"整行落在一列里"的判定阈值；低于它说明这行跨了多列，需要 token 级拆分。
WHOLE_CELL_RATIO = 0.75

DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
TOTAL_ROW_RE = re.compile(r"^(?:合计|总计|小计)")


def _build_match_terms() -> tuple[tuple[str, str], ...]:
    """(关键词, 字段) 列表，按关键词长度降序（同长按字段优先级）。"""
    terms = []
    for field_index, field in enumerate(FIELD_KEYWORDS):
        for keyword in FIELD_KEYWORDS[field]:
            terms.append((keyword, field, field_index))
    terms.sort(key=lambda item: (-len(item[0]), item[2], item[0]))
    return tuple((keyword, field) for keyword, field, _ in terms)


MATCH_TERMS = _build_match_terms()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _compress(text: str) -> str:
    """去掉所有空白，便于中文关键词/合计行匹配。"""
    return re.sub(r"\s+", "", str(text))


def _yc(line: dict) -> float:
    return (line["bbox"][1] + line["bbox"][3]) / 2.0


def _to_int(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_lines(ocr_result: dict) -> list[dict]:
    """校验并规范化 OCR 行；丢掉空文本/无 bbox 的行，保证 line_id 唯一。"""
    raw_lines = ocr_result.get("lines")
    if not isinstance(raw_lines, list):
        raise ValueError("ocr_result['lines'] 必须是 list")
    lines: list[dict] = []
    used_ids: set[int] = set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if text is None:
            continue
        text = str(text).strip()
        if not text:
            continue
        bbox = raw.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            x1, y1, x2, y2 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        line_id = raw.get("line_id")
        if not isinstance(line_id, int) or isinstance(line_id, bool) or line_id in used_ids:
            line_id = 0
            while line_id in used_ids:
                line_id += 1
        used_ids.add(line_id)
        lines.append({
            "line_id": line_id,
            "text": text,
            "confidence": round(confidence, 4),
            "bbox": [int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))],
        })
    return lines


def _normalize_source(source_meta, ocr_result: dict, lines: list[dict]) -> tuple[dict, str]:
    """规范化 source 元数据；缺失的 sha256/bytes/mtime 尝试从 path 补齐（只读）。"""
    source_meta = dict(source_meta or {})
    plugin_version = str(source_meta.get("plugin_version") or DEFAULT_PLUGIN_VERSION)
    if isinstance(source_meta.get("source"), dict):
        # 容忍 source_meta 直接传 {"source": {...}} 的形态
        inner = dict(source_meta["source"])
        inner.setdefault("plugin_version", plugin_version)
        source_meta = inner

    width = _to_int(source_meta.get("width")) or _to_int(ocr_result.get("width"))
    height = _to_int(source_meta.get("height")) or _to_int(ocr_result.get("height"))
    if not width and lines:
        width = max((line["bbox"][2] for line in lines), default=1)
    if not height and lines:
        height = max((line["bbox"][3] for line in lines), default=1)
    width = max(1, int(width or 1))
    height = max(1, int(height or 1))

    path = source_meta.get("path")
    sha256 = source_meta.get("sha256")
    size = _to_int(source_meta.get("bytes"))
    mtime = source_meta.get("mtime")

    if path and (sha256 is None or size is None or mtime is None):
        try:
            file_path = Path(str(path))
            if sha256 is None:
                sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
            stat = file_path.stat()
            if size is None:
                size = int(stat.st_size)
            if mtime is None:
                mtime = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
                    timespec="seconds"
                )
        except OSError:
            pass

    source = {
        "path": str(path) if path is not None else None,
        "sha256": sha256,
        "bytes": size,
        "width": width,
        "height": height,
        "mtime": mtime,
    }
    return source, plugin_version


def _cluster_bands(lines: list[dict]) -> list[dict]:
    """按 y 中心把 OCR 行聚成视觉行带；容差 = 行高中位数的一半。"""
    if not lines:
        return []
    heights = [max(1.0, line["bbox"][3] - line["bbox"][1]) for line in lines]
    median_height = statistics.median(heights)
    tolerance = max(0.5 * median_height, 4.0)
    ordered = sorted(lines, key=lambda line: (_yc(line), line["bbox"][0], line["line_id"]))
    bands: list[dict] = []
    for line in ordered:
        y_center = _yc(line)
        if bands and abs(y_center - bands[-1]["_sum_y"] / len(bands[-1]["lines"])) <= tolerance:
            bands[-1]["lines"].append(line)
            bands[-1]["_sum_y"] += y_center
        else:
            bands.append({"lines": [line], "_sum_y": y_center})
    for band in bands:
        ys = [_yc(line) for line in band["lines"]]
        band["y_center"] = sum(ys) / len(ys)
        band["top"] = min(line["bbox"][1] for line in band["lines"])
        band["bottom"] = max(line["bbox"][3] for line in band["lines"])
        del band["_sum_y"]
    return bands


# ---------------------------------------------------------------------------
# 关键词匹配 / 表头 / 列带
# ---------------------------------------------------------------------------
def _match_terms(text: str) -> tuple[str, list[tuple[int, int, str, str]]]:
    """返回 (压缩文本, [(start, end, field, keyword), ...])；长关键词优先且互不重叠。"""
    compressed = _compress(text)
    if not compressed:
        return compressed, []
    matches: list[tuple[int, int, str, str]] = []
    occupied: set[int] = set()
    for keyword, field in MATCH_TERMS:
        start = 0
        while True:
            index = compressed.find(keyword, start)
            if index < 0:
                break
            end = index + len(keyword)
            if not any(position in occupied for position in range(index, end)):
                matches.append((index, end, field, keyword))
                occupied.update(range(index, end))
            start = index + 1
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    return compressed, matches


def _detect_header_band(bands: list[dict]) -> dict | None:
    """命中 >= 2 个不同表头关键词的行带里，取命中字段最多、位置最靠上的一个。"""
    best = None
    best_key = None
    for index, band in enumerate(bands):
        fields: set[str] = set()
        for line in band["lines"]:
            _, matches = _match_terms(line["text"])
            fields.update(match[2] for match in matches)
        if len(fields) >= 2:
            key = (len(fields), -index)
            if best_key is None or key > best_key:
                best = band
                best_key = key
    return best


def _build_columns(header_band: dict, image_width: int) -> list[dict]:
    """由表头文字框的 x 区间生成列带；列带首尾覆盖 [0, image_width]。"""
    entries: list[dict] = []
    for line in sorted(header_band["lines"], key=lambda item: (item["bbox"][0], item["line_id"])):
        compressed, matches = _match_terms(line["text"])
        if not matches:
            continue
        x1, _, x2, _ = line["bbox"]
        text_length = len(compressed) or 1
        for start, end, field, keyword in matches:
            if start == 0 and end == text_length:
                # 整个 OCR 框就是一个表头单元格
                cell_x1, cell_x2 = float(x1), float(x2)
                header_text = line["text"].strip()
            else:
                # 多个表头被 OCR 合并进一个框：按字符比例估算各自 x 区间
                span = float(x2 - x1)
                cell_x1 = x1 + span * start / text_length
                cell_x2 = x1 + span * end / text_length
                header_text = keyword
            entries.append({
                "field": field,
                "header": header_text,
                "x1": cell_x1,
                "x2": cell_x2,
            })

    by_field: dict[str, dict] = {}
    for entry in entries:
        current = by_field.get(entry["field"])
        if current is None:
            by_field[entry["field"]] = dict(entry)
        else:
            current["x1"] = min(current["x1"], entry["x1"])
            current["x2"] = max(current["x2"], entry["x2"])

    cells = sorted(
        by_field.values(),
        key=lambda item: ((item["x1"] + item["x2"]) / 2.0, item["field"]),
    )
    columns: list[dict] = []
    count = len(cells)
    for index, cell in enumerate(cells):
        if index == 0:
            left = 0.0
        else:
            left = (cells[index - 1]["x2"] + cell["x1"]) / 2.0
        if index == count - 1:
            right = float(image_width)
        else:
            right = (cell["x2"] + cells[index + 1]["x1"]) / 2.0
        x_min = int(round(left))
        x_max = int(round(right))
        if x_max <= x_min:
            x_max = x_min + 1
        columns.append({
            "field": cell["field"],
            "header": cell["header"],
            "x_min": x_min,
            "x_max": x_max,
            "align": "right" if cell["field"] in RIGHT_ALIGN_FIELDS else "left",
        })
    return columns


# ---------------------------------------------------------------------------
# 归列 / 单元格清洗
# ---------------------------------------------------------------------------
def _column_index_at(x: float, columns: list[dict]) -> int:
    for index, column in enumerate(columns):
        if float(column["x_min"]) <= x < float(column["x_max"]):
            return index
    return min(
        range(len(columns)),
        key=lambda index: abs(
            x - (columns[index]["x_min"] + columns[index]["x_max"]) / 2.0
        ),
    )


def _tokenize_with_spans(text: str) -> list[tuple[str, int, int]]:
    """按空白切 token，保留在原文中的 [start, end) 位置，用于 x 插值。"""
    tokens = []
    start = None
    for index, char in enumerate(text):
        if char.isspace():
            if start is not None:
                tokens.append((text[start:index], start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        tokens.append((text[start:], start, len(text)))
    return tokens


def _assign_line(line: dict, columns: list[dict]) -> list[tuple[str, str]]:
    """OCR 行 → [(field, text), ...]。整行落在一列时直接归列；跨列时按 token 拆分。"""
    if not columns:
        return []
    x1, _, x2, _ = line["bbox"]
    line_width = max(1.0, float(x2 - x1))
    ratios = []
    for column in columns:
        overlap = max(
            0.0,
            min(float(x2), float(column["x_max"])) - max(float(x1), float(column["x_min"])),
        )
        ratios.append(overlap / line_width)
    best_index = max(range(len(columns)), key=lambda index: (ratios[index], -index))
    if ratios[best_index] >= WHOLE_CELL_RATIO:
        return [(columns[best_index]["field"], line["text"])]

    tokens = _tokenize_with_spans(line["text"])
    if len(tokens) < 2:
        # 跨列但 OCR 没留空格，无法可靠拆分：整行归重叠最大列（由 warnings 提示）
        return [(columns[best_index]["field"], line["text"])]

    groups: dict[str, list[str]] = {}
    text_length = len(line["text"])
    for token, start, end in tokens:
        fraction = ((start + end) / 2.0) / text_length if text_length else 0.0
        center_x = x1 + fraction * (x2 - x1)
        field = columns[_column_index_at(center_x, columns)]["field"]
        groups.setdefault(field, []).append(token)
    return [(field, " ".join(parts)) for field, parts in groups.items()]


def _clean_amount(raw: str) -> str:
    """金额清洗：全角→半角、去货币符号/千分位/空格/元，负数保留 '-'。不做舍入。"""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    for symbol in ("￥", "¥", "$", "＄"):
        text = text.replace(symbol, "")
    text = text.replace("人民币", "").replace("元", "")
    text = re.sub(r"[\s,，]", "", text)
    text = text.replace("'", "").replace("’", "").replace("`", "")
    negative = False
    if len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("+"):
        text = text[1:]
    if negative and text and not text.startswith("-"):
        text = "-" + text
    return text.strip()


def _clean_value(field: str, raw: str) -> tuple[str, bool]:
    """按字段类型清洗 value：(规范文本, 金额类是否仍可解析)。"""
    if field in AMOUNT_FIELDS:
        cleaned = _clean_amount(raw)
        return cleaned, DECIMAL_RE.match(cleaned) is not None
    # 编号类保留原始字符（前导 0、长数字、'-' 分隔），只去空白；
    # 文本类同样去空白（契约：文本类去空格）。
    return re.sub(r"\s+", "", str(raw)), True


def _merge_chunks(chunks: list[dict], columns: list[dict]) -> tuple[dict, list[dict]]:
    """同一行带里落入同一字段的 token/行合并成一个单元格（按列顺序输出）。"""
    cells: dict[str, dict] = {}
    pending_warnings: list[dict] = []
    for column in columns:
        field = column["field"]
        group = [chunk for chunk in chunks if chunk["field"] == field]
        if not group:
            continue
        if field in AMOUNT_FIELDS:
            raw = " ".join(chunk["text"] for chunk in group).strip()
            if not any(char.isdigit() for char in raw):
                pending_warnings.append({
                    "code": "AMOUNT_CELL_WITHOUT_DIGITS",
                    "message": f"金额类字段 {field} 的候选文本不含数字，已跳过该单元格",
                    "field": field,
                    "raw": raw,
                    "line_id": group[0]["line"]["line_id"],
                })
                continue
        elif field in ID_FIELDS:
            raw = "".join(chunk["text"] for chunk in group).strip()
        else:
            raw = " ".join(chunk["text"] for chunk in group).strip()
        if not raw:
            continue

        value, parseable = _clean_value(field, raw)
        if field in AMOUNT_FIELDS and not parseable:
            pending_warnings.append({
                "code": "UNPARSEABLE_AMOUNT",
                "message": f"金额类字段 {field} 清洗后仍不是十进制数：{raw!r} -> {value!r}",
                "field": field,
                "raw": raw,
                "line_id": group[0]["line"]["line_id"],
            })

        bboxes = [chunk["line"]["bbox"] for chunk in group]
        confidence = min(chunk["line"]["confidence"] for chunk in group)
        cells[field] = {
            "raw": raw,
            "value": value,
            "confidence": round(confidence, 4),
            "bbox": [
                min(bbox[0] for bbox in bboxes),
                min(bbox[1] for bbox in bboxes),
                max(bbox[2] for bbox in bboxes),
                max(bbox[3] for bbox in bboxes),
            ],
            "line_id": group[0]["line"]["line_id"],
        }
    return cells, pending_warnings


def _low_confidence_warnings(cells: dict, min_confidence: float,
                             row_index: int | None) -> list[dict]:
    warnings = []
    for field, cell in cells.items():
        if cell["confidence"] < min_confidence:
            warnings.append({
                "code": "LOW_CONFIDENCE",
                "message": (
                    f"单元格置信度 {cell['confidence']:.4f} 低于阈值 {min_confidence}"
                ),
                "row_index": row_index,
                "field": field,
                "raw": cell["raw"],
                "confidence": cell["confidence"],
                "line_id": cell["line_id"],
            })
    return warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_extraction(ocr_result: dict, source_meta, template: str = TEMPLATE_AUTO,
                     min_confidence: float = 0.5) -> dict:
    """OCR 结果 + source 元数据 → extraction 文档（契约第 3 节）。"""
    if not isinstance(ocr_result, dict):
        raise ValueError("ocr_result 必须是 dict")
    if template not in (None, TEMPLATE_AUTO):
        raise ValueError(f"未知模板 {template!r}：当前仅支持 'auto'（规则化表头匹配）")
    template_name = TEMPLATE_AUTO
    try:
        min_confidence = float(min_confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_confidence 必须是 0~1 的数字") from exc

    lines = _normalize_lines(ocr_result)
    source, plugin_version = _normalize_source(source_meta, ocr_result, lines)
    image_width = int(source["width"])

    warnings: list[dict] = []
    bands = _cluster_bands(lines)
    header_band = _detect_header_band(bands)

    columns: list[dict] = []
    header_line = None
    rows: list[dict] = []
    total_rows: list[dict] = []
    header_ids: set[int] = set()
    total_band_ids: set[int] = set()

    above_bands: list[dict] = []
    below_bands: list[dict] = []
    if header_band is None:
        warnings.append({
            "code": "NO_HEADER",
            "message": "未识别到表头：没有任何文本行包含 >=2 个表头关键词",
            "line_id": None,
        })
        above_bands = list(bands)
    else:
        header_index = bands.index(header_band)
        above_bands = bands[:header_index]
        below_bands = bands[header_index + 1:]
        header_ids = {line["line_id"] for line in header_band["lines"]}
        header_line = min(header_ids) if header_ids else None
        columns = _build_columns(header_band, image_width)
        if not columns:
            warnings.append({
                "code": "EMPTY_COLUMNS",
                "message": "表头行命中关键词但无法生成列带",
                "line_id": header_line,
            })

    if columns:
        for band in below_bands:
            ordered_lines = sorted(
                band["lines"], key=lambda line: (line["bbox"][0], line["line_id"])
            )
            chunks: list[dict] = []
            for line in ordered_lines:
                for field, text in _assign_line(line, columns):
                    chunks.append({"field": field, "text": text, "line": line})
            row_text = _compress("".join(line["text"] for line in ordered_lines))
            is_total_label = bool(row_text) and TOTAL_ROW_RE.match(row_text) is not None
            cells, pending_warnings = _merge_chunks(chunks, columns)

            if is_total_label and cells:
                amount_cells = {
                    field: cell for field, cell in cells.items() if field in AMOUNT_FIELDS
                }
                label_parts = [
                    chunk["text"] for chunk in chunks if chunk["field"] not in AMOUNT_FIELDS
                ]
                label = " ".join(part for part in label_parts if part).strip()
                if amount_cells:
                    total_rows.append({
                        "label": label or TOTAL_DEFAULT_LABEL,
                        "y_center": int(round(band["y_center"])),
                        "cells": amount_cells,
                    })
                    total_band_ids.update(line["line_id"] for line in band["lines"])
                    for warning in pending_warnings:
                        warning["row_index"] = None
                        warnings.append(warning)
                    warnings.extend(_low_confidence_warnings(amount_cells, min_confidence, None))
                    continue
                warnings.append({
                    "code": "TOTAL_ROW_WITHOUT_AMOUNT",
                    "message": f"合计行没有可用的金额单元格：{row_text!r}",
                    "line_id": ordered_lines[0]["line_id"] if ordered_lines else None,
                })

            if cells:
                row_index = len(rows) + 1
                rows.append({
                    "row_index": row_index,
                    "y_center": int(round(band["y_center"])),
                    "cells": cells,
                })
                for warning in pending_warnings:
                    warning["row_index"] = row_index
                    warnings.append(warning)
                warnings.extend(_low_confidence_warnings(cells, min_confidence, row_index))
            elif is_total_label:
                warnings.append({
                    "code": "TOTAL_ROW_WITHOUT_AMOUNT",
                    "message": f"合计行没有可用的金额单元格：{row_text!r}",
                    "line_id": ordered_lines[0]["line_id"] if ordered_lines else None,
                })

    # ---- 覆盖率：rows/total_rows 单元格 line_id ∪ skipped_lines line_id == 全部 OCR 行
    used_ids: set[int] = set()
    for row in rows:
        for cell in row["cells"].values():
            used_ids.add(cell["line_id"])
    for total_row in total_rows:
        for cell in total_row["cells"].values():
            used_ids.add(cell["line_id"])

    above_reason: dict[int, str] = {}
    if header_band is None:
        for band_index, band in enumerate(above_bands):
            reason = "title" if band_index == 0 else "unmapped"
            for line in band["lines"]:
                above_reason[line["line_id"]] = reason
    else:
        for band_index, band in enumerate(above_bands):
            reason = "title" if band_index == 0 else "above_header"
            for line in band["lines"]:
                above_reason[line["line_id"]] = reason

    heights = [max(1.0, line["bbox"][3] - line["bbox"][1]) for line in lines]
    median_height = statistics.median(heights) if heights else 1.0

    skipped_lines: list[dict] = []
    for line in sorted(lines, key=lambda item: item["line_id"]):
        line_id = line["line_id"]
        if line_id in used_ids:
            continue
        if line_id in above_reason:
            reason = above_reason[line_id]
        elif line_id in header_ids:
            reason = "header"
        elif line_id in total_band_ids:
            reason = "total"
        else:
            height = line["bbox"][3] - line["bbox"][1]
            if height < 0.5 * median_height:
                reason = "noise"
            elif line["confidence"] < min_confidence:
                reason = "low_confidence"
            else:
                reason = "unmapped"
        skipped_lines.append({
            "text": line["text"],
            "bbox": line["bbox"],
            "line_id": line_id,
            "reason": reason,
        })

    # 兜底：理论上上面的循环已覆盖全部行；若仍有漏网（代码 bug），补进 skipped 并告警，绝不静默丢行。
    covered_ids = used_ids | {item["line_id"] for item in skipped_lines}
    all_ids = {line["line_id"] for line in lines}
    missing_ids = sorted(all_ids - covered_ids)
    if missing_ids:
        line_by_id = {line["line_id"]: line for line in lines}
        for line_id in missing_ids:
            line = line_by_id[line_id]
            skipped_lines.append({
                "text": line["text"],
                "bbox": line["bbox"],
                "line_id": line_id,
                "reason": "unmapped",
            })
        warnings.append({
            "code": "LINE_COVERAGE_PATCHED",
            "message": f"覆盖率兜底补录 {len(missing_ids)} 行：{missing_ids}",
            "line_id": missing_ids[0],
        })
        skipped_lines.sort(key=lambda item: item["line_id"])

    confidences = [line["confidence"] for line in lines]
    mean_confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    extraction = {
        "schema_version": SCHEMA_VERSION,
        "plugin_version": plugin_version,
        "source": source,
        "template": template_name,
        "ocr": {
            "engine": str(ocr_result.get("engine") or "unknown"),
            "engine_version": str(ocr_result.get("engine_version") or "unknown"),
            "n_lines": len(lines),
            "mean_confidence": mean_confidence,
        },
        "columns": columns,
        "header_line": header_line,
        "rows": rows,
        "total_rows": total_rows,
        "skipped_lines": skipped_lines,
        "warnings": warnings,
    }
    return extraction


def summarize(extraction: dict) -> dict:
    """自测/日志用的摘要：列、明细合计 vs 合计行、覆盖率。"""
    detail_sums: dict[str, str] = {}
    for field in AMOUNT_FIELDS:
        total = None
        for row in extraction["rows"]:
            cell = row["cells"].get(field)
            if cell is None:
                continue
            value = cell["value"]
            try:
                number = Decimal(value)
            except Exception:
                continue
            total = number if total is None else total + number
        if total is not None:
            detail_sums[field] = str(total)
    total_cells: dict[str, str] = {}
    for total_row in extraction["total_rows"]:
        for field, cell in total_row["cells"].items():
            total_cells[field] = cell["value"]
    comparison = {}
    for field in set(detail_sums) | set(total_cells):
        comparison[field] = {
            "detail_sum": detail_sums.get(field),
            "total_row": total_cells.get(field),
            "equal": detail_sums.get(field) == total_cells.get(field),
        }
    covered = set()
    for row in extraction["rows"]:
        covered.update(cell["line_id"] for cell in row["cells"].values())
    for total_row in extraction["total_rows"]:
        covered.update(cell["line_id"] for cell in total_row["cells"].values())
    covered.update(item["line_id"] for item in extraction["skipped_lines"])
    return {
        "n_lines": extraction["ocr"]["n_lines"],
        "n_covered_line_ids": len(covered),
        "coverage_ok": len(covered) == extraction["ocr"]["n_lines"],
        "columns": [column["field"] for column in extraction["columns"]],
        "n_rows": len(extraction["rows"]),
        "n_total_rows": len(extraction["total_rows"]),
        "n_skipped_lines": len(extraction["skipped_lines"]),
        "amount_compare": comparison,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "用法: python lib/table_extract.py <ocr_result.json> <source_meta.json> [out.json]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    ocr_doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    meta_doc = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    extraction_doc = build_extraction(ocr_doc, meta_doc)
    text = json.dumps(extraction_doc, ensure_ascii=False, indent=2)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(text, encoding="utf-8")
        print(json.dumps(summarize(extraction_doc), ensure_ascii=False, indent=2))
    else:
        print(text)
