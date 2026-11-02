#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""财务图片核对插件 —— xlsx 导出层（xlsx_export）。

实现 ``lib/CONTRACT.md`` 第 5 节：把 extraction 文档 + verify 文档导出成
**4 个 sheet** 的工作簿，并返回导出文档（path / sha256 / sheets / verification / run）。

设计约束（逐条对应验收项）：

* 金额一律 ``Decimal``，以 Excel 数值单元格 + ``#,##0.00`` 写入；
  **代码里没有任何 float 参与金额运算或输出**。
* 编号类字段（invoice_code/invoice_no/voucher_no/account）以文本写入，
  ``number_format='@'``，禁止被 Excel 转成科学计数法。
* 每个数据行都有溯源列：行类型 / 行号 / 原图 / bbox / OCR 原文 / 置信度。
* 拒绝覆盖：输出路径等于源图路径、等于 extraction 文件路径、或
  ``verification.status == "fail"`` 且未显式允许 ``allow_unverified=True``。
* 先写同目录临时文件，再 ``os.replace`` 原子替换；输出文件 SHA-256。
* 确定性：工作簿 created/modified 取 ``source.mtime``（解析失败回退固定值，
  **绝不用 now()**）；zip 条目时间戳固定，同一输入两次导出的 xlsx 逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:  # 作为 lib 包被导入
    from . import finance_core
    from .finance_core import (
        AMOUNT_FIELDS,
        CANONICAL_FIELD_ORDER,
        DEFAULT_TOLERANCE,
        FinanceError,
        format_amount,
        parse_amount,
        quantize_amount,
    )
except ImportError:  # cli.py 直接 import xlsx_export 时（sys.path 含 lib）
    import finance_core  # type: ignore[no-redef]
    from finance_core import (  # type: ignore[no-redef]
        AMOUNT_FIELDS,
        CANONICAL_FIELD_ORDER,
        DEFAULT_TOLERANCE,
        FinanceError,
        format_amount,
        parse_amount,
        quantize_amount,
    )


# ------------------------------------------------------------------ 常量

SHEET_DATA = "数据"
SHEET_REPORT = "校验报告"
SHEET_ISSUES = "问题清单"
SHEET_RUN = "运行清单"
SHEET_ORDER = (SHEET_DATA, SHEET_REPORT, SHEET_ISSUES, SHEET_RUN)

TRACE_HEADERS = ("行类型", "行号", "原图", "bbox", "OCR原文", "置信度")
ROW_TYPE_DETAIL = "明细"
ROW_TYPE_TOTAL = "合计"

MONEY_NUMBER_FORMAT = "#,##0.00"
QTY_NUMBER_FORMAT = "0.####"
TEXT_NUMBER_FORMAT = "@"
CONFIDENCE_NUMBER_FORMAT = "0.0000"

#: 规范字段 -> 中文表头（extraction.columns 没给 header 时兜底）
FIELD_HEADERS_CN = {
    "seq": "序号",
    "date": "日期",
    "invoice_code": "发票代码",
    "invoice_no": "发票号码",
    "voucher_no": "凭证号",
    "summary": "摘要",
    "spec": "规格型号",
    "unit": "单位",
    "qty": "数量",
    "unit_price": "单价",
    "amount": "金额",
    "tax_rate": "税率",
    "tax": "税额",
    "total": "价税合计",
    "debit": "借方金额",
    "credit": "贷方金额",
    "account": "会计科目",
    "person": "经办人",
    "remark": "备注",
}

MONEY_FIELDS = frozenset(AMOUNT_FIELDS)
TEXT_FIELDS = frozenset({"seq", "date", "invoice_code", "invoice_no", "voucher_no", "tax_rate"})
ID_FIELDS = frozenset({"invoice_code", "invoice_no", "voucher_no", "account"})

#: run_meta 里属于“导出策略”而不是“运行事实”的键，不写进运行清单
_RUN_META_STRATEGY_KEYS = frozenset({"allow_unverified", "allowUnverified"})
#: run_meta 里可能的 extraction 文件路径键（拒绝覆盖用）
_EXTRACTION_PATH_KEYS = (
    "extraction_path",
    "extract_path",
    "extraction_file",
    "extract_json",
    "extraction_json",
    "input_extract",
)

#: created/modified 解析失败时的固定回退值（确定性：不用 now()）
_FALLBACK_MTIME = datetime(1980, 1, 1, 0, 0, 0)

_HEADER_FILL = PatternFill(fill_type="solid", start_color="FF3A6EA5", end_color="FF3A6EA5")
_HEADER_FONT = Font(bold=True, color="FFFFFFFF")
_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)

#: openpyxl 在 save 时会把 dcterms:modified 强写成当前时间（writer/excel.py）。
#: 为了“创建/修改时间取 source.mtime，不取 now()”的确定性要求，
#: 保存后重写 docProps/core.xml 里的 modified 时间戳。
_CORE_MODIFIED_RE = re.compile(
    rb"(<(?:\w+:)?modified\b[^>]*>)([^<]*)(</(?:\w+:)?modified>)"
)


# ------------------------------------------------------------------ 小工具


def _cell_text(value: Any) -> str:
    """extraction 单元格值 -> 文本（不做任何金额数值化）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _field_sort_key(field: str) -> tuple[int, str]:
    try:
        return (CANONICAL_FIELD_ORDER.index(field), field)
    except ValueError:
        return (len(CANONICAL_FIELD_ORDER), field)


def _ordered_items(cells: dict) -> list[tuple[str, dict]]:
    return sorted(cells.items(), key=lambda item: _field_sort_key(item[0]))


def _row_identity(row: dict, fallback: int) -> Any:
    value = row.get("row_index")
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _min_confidence(cells: dict) -> float | None:
    values: list[float] = []
    for field, cell in _ordered_items(cells):
        confidence = cell.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        try:
            values.append(float(confidence))
        except (TypeError, ValueError, OverflowError):  # pragma: no cover - JSON 无法产生
            continue
    return min(values) if values else None


def _bbox_text(cells: dict) -> str:
    parts: list[str] = []
    for field, cell in _ordered_items(cells):
        bbox = cell.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            parts.append(f"{field}:[{','.join(_cell_text(item) for item in bbox)}]")
    return "; ".join(parts)


def _raw_text(cells: dict) -> str:
    parts: list[str] = []
    for field, cell in _ordered_items(cells):
        raw = cell.get("raw")
        if raw is None:
            continue
        text = _cell_text(raw)
        if text:
            parts.append(f"{field}={text}")
    return " | ".join(parts)


def _display_width(value: Any) -> int:
    text = _cell_text(value)
    width = 0
    for char in text:
        width += 2 if ord(char) > 127 else 1
    return width


def _autofit_columns(worksheet, minimum: int = 8, maximum: int = 60) -> None:
    """按内容宽度设置列宽（中文按 2 个字符宽度计），确定、可复现。"""
    widths: dict[int, int] = {}
    for row in worksheet.iter_rows():
        for cell in row:
            index = cell.column
            if index is None:
                continue
            width = _display_width(cell.value)
            if width > widths.get(index, 0):
                widths[index] = width
    for index, width in widths.items():
        worksheet.column_dimensions[get_column_letter(index)].width = min(
            maximum, max(minimum, width + 2)
        )


def _write_header(worksheet, headers: list[str]) -> None:
    for column_index, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=1, column=column_index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGNMENT
        cell.number_format = TEXT_NUMBER_FORMAT
    worksheet.freeze_panes = "A2"
    worksheet.row_dimensions[1].height = 22


def _parse_source_mtime(source: dict) -> datetime:
    """source.mtime（ISO 字符串）-> naive UTC datetime；失败回退固定值。

    绝不用 ``datetime.now()``：确定性验收要求两次导出工作簿属性一致。
    """
    raw = source.get("mtime")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
    return _FALLBACK_MTIME


def _json_safe(value: Any) -> Any:
    """run 值 -> 可 JSON 往返的普通类型。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _norm_path(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


# ------------------------------------------------------------------ 数据 sheet


def _collect_data_fields(extraction: dict) -> tuple[list[str], dict[str, str]]:
    """数据 sheet 的规范字段列（列声明顺序 + 单元格里多出来的规范字段）。"""
    fields: list[str] = []
    headers: dict[str, str] = {}
    for column in extraction.get("columns") or []:
        if not isinstance(column, dict):
            continue
        field = column.get("field")
        if not isinstance(field, str) or not field or field in fields:
            continue
        fields.append(field)
        header = column.get("header")
        headers[field] = header.strip() if isinstance(header, str) and header.strip() else ""

    extra: set[str] = set()
    for row in list(extraction.get("rows") or []) + list(extraction.get("total_rows") or []):
        for field in (row.get("cells") or {}):
            if isinstance(field, str) and field:
                extra.add(field)
    known_extra = [field for field in CANONICAL_FIELD_ORDER if field in extra and field not in fields]
    unknown_extra = sorted(field for field in extra if field not in CANONICAL_FIELD_ORDER and field not in fields)
    for field in known_extra + unknown_extra:
        fields.append(field)
        headers.setdefault(field, "")

    for field in fields:
        if not headers.get(field):
            headers[field] = FIELD_HEADERS_CN.get(field, field)
    return fields, headers


def _write_data_value(worksheet, row_number: int, column_number: int, field: str, cell: dict) -> None:
    """写一个规范字段单元格：金额 Decimal 数值 + 千分位格式；文本 @。

    重要：金额只认 extraction 的规范 ``value``（contract: raw 不在导出时“顺便修复”）；
    value 解析不了时按原文文本写入并标 ``@``，不会偷偷把 raw 洗成数字。
    """
    value = cell.get("value")

    if field in MONEY_FIELDS:
        parsed = parse_amount(value)
        if parsed is None:
            fallback = _cell_text(value) or _cell_text(cell.get("raw"))
            target = worksheet.cell(row=row_number, column=column_number, value=fallback)
            target.number_format = TEXT_NUMBER_FORMAT
        else:
            target = worksheet.cell(
                row=row_number, column=column_number, value=quantize_amount(parsed, 2)
            )
            target.number_format = MONEY_NUMBER_FORMAT
        return

    if field == "qty":
        parsed = parse_amount(value)
        if parsed is None:
            fallback = _cell_text(value) or _cell_text(cell.get("raw"))
            target = worksheet.cell(row=row_number, column=column_number, value=fallback)
            target.number_format = TEXT_NUMBER_FORMAT
        else:
            target = worksheet.cell(row=row_number, column=column_number, value=parsed)
            target.number_format = QTY_NUMBER_FORMAT
        return

    text = _cell_text(value)
    if text == "" and cell.get("raw") is not None:
        text = _cell_text(cell.get("raw"))
    target = worksheet.cell(row=row_number, column=column_number, value=text)
    target.number_format = TEXT_NUMBER_FORMAT


def _write_data_sheet(worksheet, extraction: dict) -> int:
    """写数据 sheet；明细行在前、合计行在后，返回数据行数（不含表头）。"""
    fields, headers = _collect_data_fields(extraction)
    header_row = [headers[field] for field in fields] + list(TRACE_HEADERS)
    _write_header(worksheet, header_row)

    source = extraction.get("source") or {}
    source_path = source.get("path") if isinstance(source.get("path"), str) else ""
    trace_start = len(fields) + 1
    rows_written = 0
    current_row = 2

    def write_trace(row_type: str, row_identity: Any, cells: dict, confidence: float | None) -> None:
        nonlocal current_row
        worksheet.cell(row=current_row, column=trace_start, value=row_type).number_format = TEXT_NUMBER_FORMAT
        identity_cell = worksheet.cell(row=current_row, column=trace_start + 1, value=row_identity)
        if isinstance(row_identity, str):
            identity_cell.number_format = TEXT_NUMBER_FORMAT
        worksheet.cell(row=current_row, column=trace_start + 2, value=source_path).number_format = TEXT_NUMBER_FORMAT
        worksheet.cell(row=current_row, column=trace_start + 3, value=_bbox_text(cells)).number_format = TEXT_NUMBER_FORMAT
        worksheet.cell(row=current_row, column=trace_start + 4, value=_raw_text(cells)).number_format = TEXT_NUMBER_FORMAT
        if confidence is None:
            worksheet.cell(row=current_row, column=trace_start + 5, value=None)
        else:
            confidence_cell = worksheet.cell(row=current_row, column=trace_start + 5, value=confidence)
            confidence_cell.number_format = CONFIDENCE_NUMBER_FORMAT

    for row_index, row in enumerate(extraction.get("rows") or []):
        cells = row.get("cells") or {}
        for field_index, field in enumerate(fields, start=1):
            cell = cells.get(field)
            if isinstance(cell, dict):
                _write_data_value(worksheet, current_row, field_index, field, cell)
        write_trace(
            ROW_TYPE_DETAIL,
            _row_identity(row, row_index),
            cells,
            _min_confidence(cells),
        )
        current_row += 1
        rows_written += 1

    for total_row in extraction.get("total_rows") or []:
        cells = total_row.get("cells") or {}
        for field_index, field in enumerate(fields, start=1):
            cell = cells.get(field)
            if isinstance(cell, dict):
                _write_data_value(worksheet, current_row, field_index, field, cell)
        label = total_row.get("label")
        identity = label.strip() if isinstance(label, str) and label.strip() else ROW_TYPE_TOTAL
        write_trace(ROW_TYPE_TOTAL, identity, cells, _min_confidence(cells))
        current_row += 1
        rows_written += 1

    _autofit_columns(worksheet)
    return rows_written


# ------------------------------------------------------------------ 校验报告 / 问题清单 / 运行清单


def _write_report_sheet(worksheet, verification: dict) -> int:
    headers = ["检查ID", "检查项", "状态", "说明"]
    _write_header(worksheet, headers)
    checks = verification.get("checks")
    if not isinstance(checks, list):
        checks = []
    for row_number, check in enumerate(checks, start=2):
        if not isinstance(check, dict):
            continue
        values = [
            _cell_text(check.get("id")),
            _cell_text(check.get("name")),
            _cell_text(check.get("status")),
            _cell_text(check.get("detail")),
        ]
        for column_number, value in enumerate(values, start=1):
            cell = worksheet.cell(row=row_number, column=column_number, value=value)
            cell.number_format = TEXT_NUMBER_FORMAT
            if column_number == 4:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    _autofit_columns(worksheet)
    return len(checks)


_ISSUE_HEADERS = ("序号", "类型", "代码", "说明", "行号", "字段", "期望", "实际", "差额", "置信度", "原值")


def _issue_identity(item: dict) -> Any:
    row_index = item.get("row_index")
    if row_index is not None:
        return row_index
    rows = item.get("rows")
    if isinstance(rows, (list, tuple)) and rows:
        return ",".join(_cell_text(value) for value in rows)
    row_label = item.get("row_label")
    if isinstance(row_label, str) and row_label:
        return row_label
    return ""


def _write_issues_sheet(worksheet, verification: dict) -> int:
    _write_header(worksheet, list(_ISSUE_HEADERS))
    hard_failures = verification.get("hard_failures")
    review_items = verification.get("review_items")
    if not isinstance(hard_failures, list):
        hard_failures = []
    if not isinstance(review_items, list):
        review_items = []

    current_row = 2
    sequence = 0
    count = 0
    for kind, items in (("硬失败", hard_failures), ("待确认", review_items)):
        for item in items:
            if not isinstance(item, dict):
                continue
            sequence += 1
            count += 1
            values: list[Any] = [
                sequence,
                kind,
                _cell_text(item.get("code")),
                _cell_text(item.get("message")),
                _cell_text(_issue_identity(item)),
                _cell_text(item.get("field")),
                _cell_text(item.get("expected")),
                _cell_text(item.get("actual")),
                _cell_text(item.get("delta")),
                None,
                _cell_text(item.get("raw")),
            ]
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                values[9] = _cell_text(confidence)
                confidence_format = TEXT_NUMBER_FORMAT
            else:
                values[9] = float(confidence)
                confidence_format = CONFIDENCE_NUMBER_FORMAT
            for column_number, value in enumerate(values, start=1):
                cell = worksheet.cell(row=current_row, column=column_number, value=value)
                if column_number == 10:
                    cell.number_format = confidence_format
                elif column_number == 1:
                    cell.number_format = "0"
                else:
                    cell.number_format = TEXT_NUMBER_FORMAT
                if column_number == 4:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
            current_row += 1
    _autofit_columns(worksheet)
    return count


def _write_run_sheet(worksheet, run: dict) -> int:
    _write_header(worksheet, ["项目", "值"])
    written = 0
    for row_number, (key, value) in enumerate(run.items(), start=2):
        if key == "output_sha256":
            display = "（写盘后计算，见导出结果 JSON）"
        elif isinstance(value, (dict, list)):
            display = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
        else:
            display = _cell_text(value)
        key_cell = worksheet.cell(row=row_number, column=1, value=str(key))
        key_cell.number_format = TEXT_NUMBER_FORMAT
        value_cell = worksheet.cell(row=row_number, column=2, value=display)
        value_cell.number_format = TEXT_NUMBER_FORMAT
        written += 1
    _autofit_columns(worksheet, maximum=100)
    return written


# ------------------------------------------------------------------ 导出主入口


def _check_refusals(
    extraction: dict,
    verification: dict,
    output_path: str,
    run_meta: dict,
    allow_unverified: bool,
) -> None:
    """契约第 5 节拒绝条件；命中抛 ``OUTPUT_REFUSED``（带 .code）。"""
    output_norm = _norm_path(output_path)

    source = extraction.get("source") or {}
    source_path = source.get("path")
    if isinstance(source_path, str) and source_path.strip():
        if output_norm == _norm_path(source_path):
            raise FinanceError(
                "OUTPUT_REFUSED",
                f"拒绝覆盖源文件：输出路径与 extraction.source.path 相同（{source_path}）",
            )

    extraction_candidates: list[str] = []
    for key in _EXTRACTION_PATH_KEYS:
        candidate = run_meta.get(key)
        if isinstance(candidate, str) and candidate.strip():
            extraction_candidates.append(candidate)
    for candidate in extraction_candidates:
        if output_norm == _norm_path(candidate):
            raise FinanceError(
                "OUTPUT_REFUSED",
                f"拒绝覆盖 extraction 文件：输出路径与 extraction JSON 相同（{candidate}）",
            )

    status = verification.get("status")
    if not isinstance(status, str) or status not in ("pass", "needs_review", "fail"):
        raise FinanceError(
            "BAD_INPUT",
            f"verification.status 缺失或不合法：{status!r}（应为 pass / needs_review / fail）",
        )
    if status == "fail" and not allow_unverified:
        raise FinanceError(
            "OUTPUT_REFUSED",
            "校验状态为 fail，拒绝导出（如确认要导出未通过校验的台账，请显式传 allow_unverified=true）",
        )


def _build_run_dict(
    extraction: dict, run_meta: dict, tolerance: str
) -> dict:
    ocr = extraction.get("ocr") or {}
    source = extraction.get("source") or {}

    plugin_version = run_meta.get("plugin_version")
    if not isinstance(plugin_version, str) or not plugin_version:
        plugin_version = extraction.get("plugin_version")
    if not isinstance(plugin_version, str) or not plugin_version:
        plugin_version = "0.1.0"

    engine = run_meta.get("engine")
    if not isinstance(engine, str) or not engine:
        engine_name = ocr.get("engine") if isinstance(ocr.get("engine"), str) else "unknown"
        engine_version = ocr.get("engine_version") if isinstance(ocr.get("engine_version"), str) else ""
        engine = f"{engine_name} {engine_version}".strip()

    input_path = run_meta.get("input_path")
    if not isinstance(input_path, str) or not input_path:
        input_path = source.get("path") if isinstance(source.get("path"), str) else ""

    input_sha256 = run_meta.get("input_sha256")
    if not isinstance(input_sha256, str) or not input_sha256:
        input_sha256 = source.get("sha256") if isinstance(source.get("sha256"), str) else ""

    run: dict[str, Any] = {
        "plugin_version": plugin_version,
        "engine": engine,
        "input_path": input_path,
        "input_sha256": input_sha256,
        "output_sha256": "",
        "tolerance": tolerance,
    }
    for key in sorted(run_meta):
        if key in run or key in _RUN_META_STRATEGY_KEYS:
            continue
        run[key] = _json_safe(run_meta[key])
    return run


def _save_workbook_deterministic(workbook, output_path: str, modified: datetime) -> None:
    """先写同目录临时文件，固定 zip 时间戳 + 改写 modified，再 os.replace。"""
    directory = os.path.dirname(output_path) or "."
    if not os.path.isdir(directory):
        raise FinanceError("IO_ERROR", f"输出目录不存在：{directory}")
    if os.path.isdir(output_path):
        raise FinanceError("IO_ERROR", f"输出路径是一个目录，不能当文件写：{output_path}")

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=".dsh-finance-ocr-", suffix=".xlsx", dir=directory
    )
    os.close(file_descriptor)
    normalized_name = temp_name + ".zip"
    try:
        workbook.save(temp_name)
        _rebuild_zip_deterministic(temp_name, normalized_name, modified)
        os.replace(normalized_name, output_path)
    except OSError as error:
        raise FinanceError("IO_ERROR", f"写出 xlsx 失败：{output_path}（{error}）") from error
    finally:
        for leftover in (temp_name, normalized_name):
            if os.path.exists(leftover):
                try:
                    os.unlink(leftover)
                except OSError:  # pragma: no cover - 清理失败不影响主流程
                    pass


def _stamp_core_modified(core_xml: bytes, modified: datetime) -> bytes:
    """把 docProps/core.xml 的 dcterms:modified 改写成 source.mtime（UTC, 秒精度）。"""
    stamp = modified.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")

    def replace(match: re.Match) -> bytes:
        return match.group(1) + stamp + match.group(3)

    return _CORE_MODIFIED_RE.sub(replace, core_xml, count=1)


def _rebuild_zip_deterministic(
    source_path: str, destination_path: str, modified: datetime
) -> None:
    """重写 xlsx 的 zip：时间戳固定 1980-01-01，modified 取 source.mtime。

    这样同一 extraction 两次导出的 xlsx 逐字节一致（验收项：结果完全一致）。
    """
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(source_path, "r") as source_zip:
        for info in source_zip.infolist():
            payload = source_zip.read(info.filename)
            if info.filename == "docProps/core.xml":
                payload = _stamp_core_modified(payload, modified)
            entries.append((info.filename, payload))
    with zipfile.ZipFile(
        destination_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as target_zip:
        for name, data in entries:
            zip_info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zip_info.compress_type = zipfile.ZIP_DEFLATED
            zip_info.external_attr = 0o600 << 16
            target_zip.writestr(zip_info, data)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_xlsx(
    extraction: dict,
    verification: dict,
    output_path: Any,
    run_meta: dict | None = None,
    allow_unverified: bool = False,
) -> dict:
    """导出 4 sheet xlsx，返回契约第 5 节的 export 文档。

    参数
    ----
    extraction : dict
        ``finance-extract/1`` 文档。
    verification : dict
        ``finance-verify/1`` 文档（``verify()`` 的产物）。
    output_path : str | os.PathLike
        目标 xlsx 绝对/相对路径。
    run_meta : dict | None
        运行元数据（plugin_version / engine / input_path / input_sha256 /
        tolerance / extraction_path / allow_unverified 等）。
    allow_unverified : bool
        ``verification.status == "fail"`` 时是否仍允许导出（默认拒绝）。

    返回
    ----
    dict
        ``{ok, path, sha256, sheets, verification, run}``。

    拒绝条件抛 ``FinanceError``，``code`` 为 ``OUTPUT_REFUSED`` / ``IO_ERROR`` /
    ``BAD_INPUT``。
    """
    if not isinstance(extraction, dict):
        raise FinanceError("BAD_INPUT", "extraction 必须是 JSON 对象")
    if not isinstance(verification, dict):
        raise FinanceError("BAD_INPUT", "verification 必须是 JSON 对象")
    if output_path is None or str(output_path).strip() == "":
        raise FinanceError("BAD_INPUT", "output_path 不能为空")
    if run_meta is None:
        run_meta = {}
    if not isinstance(run_meta, dict):
        raise FinanceError("BAD_INPUT", "run_meta 必须是 JSON 对象或 None")

    output_path = os.path.abspath(os.path.expanduser(str(output_path)))
    tolerance = run_meta.get("tolerance")
    if not isinstance(tolerance, str) or not tolerance.strip():
        tolerance = DEFAULT_TOLERANCE
    else:
        tolerance = tolerance.strip()
    # 容差必须是合法 Decimal 字符串（金额不允许 float）
    try:
        finance_core._coerce_tolerance(tolerance)  # noqa: SLF001 - 同包内部复用
    except FinanceError as error:
        raise FinanceError("BAD_INPUT", f"run_meta.tolerance 不合法：{tolerance!r}") from error

    allow = bool(allow_unverified) or bool(
        run_meta.get("allow_unverified") or run_meta.get("allowUnverified")
    )
    _check_refusals(extraction, verification, output_path, run_meta, allow)

    run = _build_run_dict(extraction, run_meta, tolerance)

    workbook = Workbook()
    data_sheet = workbook.active
    data_sheet.title = SHEET_DATA
    report_sheet = workbook.create_sheet(SHEET_REPORT)
    issues_sheet = workbook.create_sheet(SHEET_ISSUES)
    run_sheet = workbook.create_sheet(SHEET_RUN)

    mtime = _parse_source_mtime(extraction.get("source") or {})
    properties = workbook.properties
    properties.creator = "dsh-finance-ocr"
    properties.lastModifiedBy = "dsh-finance-ocr"
    properties.title = "财务图片核对结果"
    properties.subject = "finance-ocr verify export"
    properties.description = "由 dsh-plugin-finance-ocr 生成；金额列为 Decimal 数值，编号列为文本。"
    properties.created = mtime
    properties.modified = mtime

    data_rows = _write_data_sheet(data_sheet, extraction)
    report_rows = _write_report_sheet(report_sheet, verification)
    issue_rows = _write_issues_sheet(issues_sheet, verification)
    run_rows = _write_run_sheet(run_sheet, run)

    _save_workbook_deterministic(workbook, output_path, mtime)
    output_sha256 = _sha256_file(output_path)
    run["output_sha256"] = output_sha256

    hard_failures = verification.get("hard_failures")
    review_items = verification.get("review_items")
    return {
        "ok": True,
        "path": output_path,
        "sha256": output_sha256,
        "sheets": [
            {"name": SHEET_DATA, "rows": data_rows},
            {"name": SHEET_REPORT, "rows": report_rows},
            {"name": SHEET_ISSUES, "rows": issue_rows},
            {"name": SHEET_RUN, "rows": run_rows},
        ],
        "verification": {
            "status": verification.get("status"),
            "n_hard_failures": len(hard_failures) if isinstance(hard_failures, list) else 0,
            "n_review_items": len(review_items) if isinstance(review_items, list) else 0,
        },
        "run": run,
    }


#: 兼容别名：CONTRACT.md 里有时写作 export
export = export_xlsx


__all__ = ["export_xlsx", "export", "SHEET_ORDER"]
