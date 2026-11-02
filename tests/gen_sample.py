# -*- coding: utf-8 -*-
"""生成合成中文财务报销单样图（端到端测试用，见 lib/CONTRACT.md 第 1 节）。

用法：
    python tests/gen_sample.py [输出路径]

默认输出 samples/sample_reimbursement.png。
数据固定、无随机行为（仍然固定 seed 以便将来扩展时保持可复现），渲染前用 Decimal
断言「明细金额之和 == 合计行」，保证样图本身是一张合理的财务单据。

打印 JSON 报表（含每行真实值），便于测试断言 OCR 识别结果。
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "samples" / "sample_reimbursement.png"

# ---------------------------------------------------------------------------
# 版面尺寸（像素）。宽度 1900 在需求建议的 1600~2200 区间内；字号 36px 保证 OCR 可识别。
# 网格线用很浅的灰（230），实测太深的网格线会让单字符检测框混入线像素、拉低识别置信度。
# ---------------------------------------------------------------------------
WIDTH = 1900
MARGIN_X = 70
BACKGROUND = (255, 255, 255)
GRID_COLOR = (230, 230, 230)
TEXT_COLOR = (0, 0, 0)

TITLE = "差旅费报销单"
SUBTITLE = "报销部门：综合办公室        经办：张三        2026年10月"
TITLE_SIZE = 48
SUBTITLE_SIZE = 26
HEADER_FONT_SIZE = 36
CELL_FONT_SIZE = 36

TITLE_Y = 60
SUBTITLE_Y = 132
TABLE_TOP = 208
HEADER_ROW_HEIGHT = 70
DETAIL_ROW_HEIGHT = 68
TOTAL_ROW_HEIGHT = 72

# 列宽之和必须等于表格宽度（WIDTH - 2 * MARGIN_X = 1760）。
COLUMNS = (
    {"field": "seq", "header": "序号", "width": 120, "align": "center"},
    {"field": "date", "header": "日期", "width": 300, "align": "center"},
    {"field": "summary", "header": "摘要", "width": 520, "align": "left"},
    {"field": "amount", "header": "金额", "width": 320, "align": "right"},
    {"field": "tax", "header": "税额", "width": 220, "align": "right"},
    {"field": "total", "header": "价税合计", "width": 280, "align": "right"},
)
assert sum(col["width"] for col in COLUMNS) == WIDTH - 2 * MARGIN_X, "列宽与图宽不一致"

# 固定数据：金额、税额都是两位 Decimal 字符串；每行 total = amount + tax，
# 且 5 行 amount / tax / total 各自求和 == 合计行（渲染前会断言）。
SAMPLE_ROWS = (
    {"seq": "1", "date": "2026-10-08", "summary": "高铁票",
     "amount": "1180.00", "tax": "70.80", "total": "1250.80"},
    {"seq": "2", "date": "2026-10-10", "summary": "办公用品",
     "amount": "356.50", "tax": "21.39", "total": "377.89"},
    {"seq": "3", "date": "2026-10-11", "summary": "住宿费",
     "amount": "880.00", "tax": "52.80", "total": "932.80"},
    {"seq": "4", "date": "2026-10-12", "summary": "市内交通费",
     "amount": "126.40", "tax": "7.58", "total": "133.98"},
    {"seq": "5", "date": "2026-10-13", "summary": "餐费",
     "amount": "240.00", "tax": "14.40", "total": "254.40"},
)
SAMPLE_TOTALS = {"label": "合计", "amount": "2782.90", "tax": "166.97", "total": "2949.87"}
MONEY_FIELDS = ("amount", "tax", "total", "unit_price", "debit", "credit")
SUM_FIELDS = ("amount", "tax", "total")

# Windows 自带中文字体；按顺序找第一个存在的。
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)
BOLD_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
) + FONT_CANDIDATES


def format_money(value) -> str:
    """Decimal 两位小写 + 千分位：1180.00 -> '1,180.00'。"""
    return f"{Decimal(str(value)):,.2f}"


def _font_path(bold: bool = False) -> str:
    for candidate in (BOLD_FONT_CANDIDATES if bold else FONT_CANDIDATES):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "找不到可用的 Windows 中文字体（尝试过 msyh.ttc / msyhbd.ttc / simhei.ttf / simsun.ttc）"
    )


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(bold), size)


def layout() -> dict:
    """返回版面几何信息（供渲染与报表复用）。"""
    column_edges = [MARGIN_X]
    for column in COLUMNS:
        column_edges.append(column_edges[-1] + column["width"])
    header_top = TABLE_TOP
    header_bottom = header_top + HEADER_ROW_HEIGHT
    detail_tops = [header_bottom + i * DETAIL_ROW_HEIGHT for i in range(len(SAMPLE_ROWS))]
    total_top = header_bottom + len(SAMPLE_ROWS) * DETAIL_ROW_HEIGHT
    total_bottom = total_top + TOTAL_ROW_HEIGHT
    return {
        "width": WIDTH,
        "height": total_bottom + 80,
        "column_edges": column_edges,
        "header_top": header_top,
        "header_bottom": header_bottom,
        "detail_tops": detail_tops,
        "total_top": total_top,
        "total_bottom": total_bottom,
    }


def _draw_cell_text(draw: ImageDraw.ImageDraw, box, text: str, font, align: str) -> None:
    if not text:
        return
    x1, y1, x2, y2 = box
    pad = 22
    mid_y = (y1 + y2) / 2.0
    if align == "center":
        draw.text(((x1 + x2) / 2.0, mid_y), text, font=font, fill=TEXT_COLOR, anchor="mm")
    elif align == "right":
        draw.text((x2 - pad, mid_y), text, font=font, fill=TEXT_COLOR, anchor="rm")
    else:
        draw.text((x1 + pad, mid_y), text, font=font, fill=TEXT_COLOR, anchor="lm")


def build_image() -> Image.Image:
    """渲染样图（纯函数，不写盘）。"""
    geom = layout()
    image = Image.new("RGB", (geom["width"], geom["height"]), BACKGROUND)
    draw = ImageDraw.Draw(image)

    font_title = _load_font(TITLE_SIZE, bold=True)
    font_subtitle = _load_font(SUBTITLE_SIZE)
    font_header = _load_font(HEADER_FONT_SIZE, bold=True)
    font_cell = _load_font(CELL_FONT_SIZE, bold=True)

    center_x = (MARGIN_X + geom["column_edges"][-1]) / 2.0
    draw.text((center_x, TITLE_Y), TITLE, font=font_title, fill=TEXT_COLOR, anchor="mm")
    draw.text((center_x, SUBTITLE_Y), SUBTITLE, font=font_subtitle,
              fill=TEXT_COLOR, anchor="mm")

    edges = geom["column_edges"]
    # 表格网格线（浅灰 1px，模拟真实表单，同时尽量避免干扰文字检测）
    for x in edges:
        draw.line([(x, geom["header_top"]), (x, geom["total_bottom"])],
                  fill=GRID_COLOR, width=1)
    row_boundaries = [geom["header_top"], geom["header_bottom"], *geom["detail_tops"],
                      geom["total_bottom"]]
    for y in sorted(set(row_boundaries)):
        draw.line([(edges[0], y), (edges[-1], y)], fill=GRID_COLOR, width=1)

    # 表头
    for i, column in enumerate(COLUMNS):
        _draw_cell_text(
            draw,
            (edges[i], geom["header_top"], edges[i + 1], geom["header_bottom"]),
            column["header"], font_header, column["align"],
        )

    # 明细行
    for r, row in enumerate(SAMPLE_ROWS):
        top = geom["detail_tops"][r]
        bottom = top + DETAIL_ROW_HEIGHT
        for i, column in enumerate(COLUMNS):
            field = column["field"]
            value = row.get(field, "")
            if field in MONEY_FIELDS:
                value = format_money(value)
            _draw_cell_text(draw, (edges[i], top, edges[i + 1], bottom),
                            value, font_cell, column["align"])

    # 合计行
    top = geom["total_top"]
    bottom = geom["total_bottom"]
    for i, column in enumerate(COLUMNS):
        field = column["field"]
        value = ""
        if field == "summary":
            value = SAMPLE_TOTALS["label"]
        elif field in MONEY_FIELDS:
            value = format_money(SAMPLE_TOTALS[field])
        _draw_cell_text(draw, (edges[i], top, edges[i + 1], bottom),
                        value, font_header, column["align"])
    return image


def _dec(value) -> Decimal:
    return Decimal(str(value))


def sum_check() -> dict:
    """校验并返回明细 vs 合计行/逐行勾稽结果（用 Decimal，绝不 float）。"""
    checks = {}
    for field in SUM_FIELDS:
        detail_sum = sum((_dec(row[field]) for row in SAMPLE_ROWS), Decimal("0"))
        total_row = _dec(SAMPLE_TOTALS[field])
        checks[field] = {
            "detail_sum": str(detail_sum.quantize(Decimal("0.01"))),
            "total_row": str(total_row.quantize(Decimal("0.01"))),
            "equal": detail_sum == total_row,
        }
    row_checks = []
    for row in SAMPLE_ROWS:
        row_checks.append({
            "seq": row["seq"],
            "amount_plus_tax": str((_dec(row["amount"]) + _dec(row["tax"])).quantize(Decimal("0.01"))),
            "total": str(_dec(row["total"]).quantize(Decimal("0.01"))),
            "equal": _dec(row["amount"]) + _dec(row["tax"]) == _dec(row["total"]),
        })
    return {"detail_vs_total": checks, "row_amount_plus_tax": row_checks}


def build_report(output_path: Path) -> dict:
    """生成写盘后的 JSON 报表（含真实值，供 OCR 结果断言）。"""
    geom = layout()
    sums = sum_check()
    report = {
        "output": str(output_path),
        "width": geom["width"],
        "height": geom["height"],
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "bytes": output_path.stat().st_size,
        "columns": [column["header"] for column in COLUMNS],
        "rows": [
            {
                "seq": row["seq"],
                "date": row["date"],
                "summary": row["summary"],
                "amount": format_money(row["amount"]),
                "tax": format_money(row["tax"]),
                "total": format_money(row["total"]),
            }
            for row in SAMPLE_ROWS
        ],
        "totals": {
            "label": SAMPLE_TOTALS["label"],
            "amount": format_money(SAMPLE_TOTALS["amount"]),
            "tax": format_money(SAMPLE_TOTALS["tax"]),
            "total": format_money(SAMPLE_TOTALS["total"]),
        },
        "sum_check": sums,
    }
    # 样图必须自洽，否则不要拿去当测试基准
    for field, check in sums["detail_vs_total"].items():
        if not check["equal"]:
            raise RuntimeError(f"样图数据不自洽：{field} 明细合计 {check['detail_sum']} != 合计行 {check['total_row']}")
    for check in sums["row_amount_plus_tax"]:
        if not check["equal"]:
            raise RuntimeError(f"样图数据不自洽：第 {check['seq']} 行 金额+税额 != 价税合计")
    return report


def main(output_path=None) -> dict:
    """生成样图并返回报表。默认输出 samples/sample_reimbursement.png。"""
    random.seed(20261102)  # 本脚本无随机逻辑，固定 seed 仅为可复现性兜底
    path = Path(output_path) if output_path else DEFAULT_OUTPUT
    path.parent.mkdir(parents=True, exist_ok=True)
    sums = sum_check()
    # 先校验再画，避免产出错误基准图
    for field, check in sums["detail_vs_total"].items():
        if not check["equal"]:
            raise RuntimeError(f"样图数据不自洽：{field} 明细合计 {check['detail_sum']} != 合计行 {check['total_row']}")
    image = build_image()
    image.save(path, format="PNG")
    return build_report(path)


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(main(output), ensure_ascii=False, indent=2))
