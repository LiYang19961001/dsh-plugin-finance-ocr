#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""财务图片核对插件 —— 校验层核心（finance_core）。

实现 ``lib/CONTRACT.md`` 第 3/4/6 节：

* 金额清洗 ``parse_amount``：全角数字、货币符号、千分位、括号负数、正负号、空格；
* 金额量化 ``quantize_amount``：Decimal + ``ROUND_HALF_UP``，默认 2 位；
* extraction 读取与基础 schema 校验 ``load_extraction``；
* 校验 ``verify``：产出 ``finance-verify/1`` 文档（三态 status / 硬失败 /
  待确认项 / checks 明细）。

铁律（见 CONTRACT.md 抬头与 REQUIREMENTS.md 第 4 节）：

1. 金额一律 ``decimal.Decimal``。本模块没有任何 float 参与金额运算或输出，
   所有金额一律以**字符串**写出；OCR 置信度不是金额，按契约保持 float。
2. 不猜。``parse_amount`` 对任何不确定的写法返回 ``None``；
   verify 遇到无法解析的金额是**硬失败**，不是静默跳过。
3. 严格。``TOTAL_MISMATCH`` 在 ``|差额| >= tolerance`` 时判失败
   （默认 ``0.01``，即“改坏 1 分钱”必被抓；``tolerance="0.00"`` 要求完全相等）。
4. 确定性。同一 extraction 反复 verify，输出逐字节一致（顺序全部稳定）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Iterator

# ------------------------------------------------------------------ 常量

EXTRACT_SCHEMA_VERSION = "finance-extract/1"
VERIFY_SCHEMA_VERSION = "finance-verify/1"

DEFAULT_TOLERANCE = "0.01"
DEFAULT_LOW_CONFIDENCE = 0.90

#: 金额类字段（CONTRACT.md 第 3 节 + table_extract 字段词典）
AMOUNT_FIELDS = ("amount", "tax", "total", "debit", "credit", "unit_price")
#: 编号类字段：以文本保存，禁止被 Excel 数值化/科学计数法化
ID_FIELDS = ("invoice_code", "invoice_no", "voucher_no", "account")
#: 日期类字段
DATE_FIELDS = ("date",)
#: 默认必填字段（只在这些字段确实出现在 extraction 里时才检查）
DEFAULT_REQUIRED_FIELDS = ("summary",)

#: 规范字段顺序（数据 sheet 列顺序、单元格遍历顺序都基于它，保证稳定）
CANONICAL_FIELD_ORDER = (
    "seq",
    "date",
    "invoice_code",
    "invoice_no",
    "voucher_no",
    "summary",
    "spec",
    "unit",
    "qty",
    "unit_price",
    "amount",
    "tax_rate",
    "tax",
    "total",
    "debit",
    "credit",
    "account",
    "person",
    "remark",
)

ZERO = Decimal("0")
_HUNDRED = Decimal("100")

#: 科学计数法（DANGEROUS_NUMBER）
_SCI_RE = re.compile(r"[eE][+-]?\d+")
#: 纯十进制字面量（parse_amount 的最终形态）
_DIGIT_RE = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
#: 合法千分位分组（先于去逗号校验）
_COMMA_GROUP_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")

#: 全角 -> 半角（数字、符号、括号、百分号、货币符号、空格由 split 处理）
_FULLWIDTH_TRANSLATION = str.maketrans(
    {
        **{chr(0xFF10 + index): str(index) for index in range(10)},
        "＋": "+",
        "－": "-",
        "−": "-",
        "–": "-",
        "—": "-",
        "，": ",",
        "．": ".",
        "。": ".",
        "（": "(",
        "）": ")",
        "％": "%",
        "＄": "$",
        "￡": "£",
        "\u3000": " ",
    }
)

_CURRENCY_CHARS = "¥￥$＄£"
_CURRENCY_WORDS = ("RMB", "CNY", "Rmb", "Cny", "rmb", "cny", "人民币")
_CNY_UNITS = ("元整", "圆整", "元", "圆")

#: 日期格式（DATE_FORMAT 待确认项）。顺序固定，先完整日期再紧凑 8 位。
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?$"),
)
_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


# ------------------------------------------------------------------ 错误


class FinanceError(Exception):
    """带契约错误码的异常。

    ``error.code`` 取值见 CONTRACT.md 第 6 节：
    ``BAD_INPUT`` / ``NO_OCR_ENGINE`` / ``OCR_FAILED`` / ``EXTRACTION_NOT_FOUND``
    / ``SCHEMA_MISMATCH`` / ``OUTPUT_REFUSED`` / ``IO_ERROR``。
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    # 便于日志/调试
    def __str__(self) -> str:  # pragma: no cover - 极简
        return f"[{self.code}] {self.message}"


# ------------------------------------------------------------------ 金额清洗


def _normalize_ocr_text(raw: str) -> str:
    """全角转半角 + 去掉所有空白（含全角空格）。"""
    text = raw.translate(_FULLWIDTH_TRANSLATION)
    return "".join(text.split())


def _strip_currency(text: str) -> str:
    """去掉首尾货币符号/货币名前缀。"""
    changed = True
    while changed and text:
        changed = False
        while text and text[0] in _CURRENCY_CHARS:
            text = text[1:]
            changed = True
        while text and text[-1] in _CURRENCY_CHARS:
            text = text[:-1]
            changed = True
        for word in _CURRENCY_WORDS:
            if text.startswith(word):
                text = text[len(word):]
                changed = True
                break
    return text


def _strip_cny_unit(text: str) -> str:
    """去掉金额尾部的中文单位（元/圆/元整/圆整）。"""
    for unit in _CNY_UNITS:
        if text.endswith(unit):
            return text[: -len(unit)]
    return text


def _normalize_zero(value: Decimal) -> Decimal:
    """把 -0 归一成 0，避免输出 "-0.00"。"""
    if value == 0:
        return value.copy_abs()
    return value


def parse_amount(raw: Any) -> Decimal | None:
    """把 OCR 原始金额清洗成 ``Decimal``；不合法返回 ``None``（绝不猜）。

    支持：全角数字、``￥/$/¥``、千分位逗号、括号负数、正负号、空格、
    尾部 ``元/圆/元整``、会计写法的尾部负号、``.5`` 这类省略整数位写法。

    明确拒绝（返回 None）：

    * 科学计数法（``1.23E+15``）——宁可报 DANGEROUS_NUMBER，也不猜；
    * 分组非法的千分位（``1,23.45``）；
    * 多个小数点、``+``/``-``/括号混用等有歧义的写法；
    * Python float（金额不允许是 float）。

    传入 ``Decimal`` 时原样返回（仅归一 -0），便于内部量化链路复用。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return _normalize_zero(raw)
    if isinstance(raw, int):
        return _normalize_zero(Decimal(raw))
    if isinstance(raw, float):
        # 金额一律 Decimal；出现 float 说明上游已经破坏了精度，不接。
        return None
    if not isinstance(raw, str):
        return None

    text = _normalize_ocr_text(raw)
    if not text:
        return None

    text = _strip_currency(text)
    if not text:
        return None

    negative = False
    if text.startswith("("):
        if not text.endswith(")"):
            return None
        negative = True
        text = text[1:-1]
        text = _strip_currency(text)
    elif "(" in text or ")" in text:
        return None

    text = _strip_cny_unit(text)
    if not text:
        return None

    sign_negative = False
    if text[0] in "+-":
        sign_negative = text[0] == "-"
        text = text[1:]
        text = _strip_currency(text)
        text = _strip_cny_unit(text)

    if text.endswith("-"):
        # 会计写法：1234.50- 表示负数
        sign_negative = True
        text = text[:-1]

    if not text:
        return None
    if negative and sign_negative:
        # "(-100)" 这种双重负号有歧义，不猜
        return None
    if any(char in text for char in "()+-"):
        return None

    if "," in text:
        if not _COMMA_GROUP_RE.match(text):
            return None
        text = text.replace(",", "")

    if not _DIGIT_RE.match(text):
        return None
    if text.endswith("."):
        text = text[:-1]
    if not text or text == ".":
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if negative or sign_negative:
        value = -value
    return _normalize_zero(value)


def parse_rate(raw: Any) -> Decimal | None:
    """把税率清洗成 ``Decimal`` 比例（``6%`` -> 0.06，``0.06`` -> 0.06）。

    不合法返回 ``None``。只用于 TAX_MISMATCH 的待确认项，不影响金额主链路。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return None
    if not isinstance(raw, str):
        return None

    text = _normalize_ocr_text(raw)
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    value = parse_amount(text)
    if value is None:
        return None
    if is_percent:
        value = value / _HUNDRED
    return value


def quantize_amount(value: Any, places: int = 2) -> Decimal:
    """按 ``places`` 位小数做 ``ROUND_HALF_UP`` 量化；失败抛 ``BAD_INPUT``。

    ``value`` 允许 ``Decimal`` / 数字字符串（经 parse_amount 清洗）/ int；
    float 会因 parse_amount 拒绝而抛 ``BAD_INPUT``。
    """
    decimal_value = value if isinstance(value, Decimal) else parse_amount(value)
    if decimal_value is None:
        raise FinanceError("BAD_INPUT", f"金额无法解析为 Decimal：{value!r}")
    try:
        decimal_places = int(places)
    except (TypeError, ValueError) as error:
        raise FinanceError("BAD_INPUT", f"小数位数不合法：{places!r}") from error
    quantum = Decimal(1).scaleb(-decimal_places)
    digits = len(decimal_value.as_tuple().digits)
    try:
        with localcontext() as context:
            context.prec = max(28, digits + abs(decimal_places) + 8)
            return decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise FinanceError("BAD_INPUT", f"金额量化失败：{value!r}") from error


def format_amount(value: Any, places: int = 2) -> str:
    """金额 -> 固定小数位字符串（HALF_UP），金额输出的唯一出口。"""
    return format(quantize_amount(value, places), "f")


#: 内部简写
_fmt = format_amount


# ------------------------------------------------------------------ 参数转换


def _coerce_tolerance(value: Any) -> Decimal:
    """容差参数转 Decimal；接受 str/int/Decimal/float（仅此入参放宽）。"""
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, bool) or value is None:
        raise FinanceError("BAD_INPUT", f"tolerance 不合法：{value!r}")
    elif isinstance(value, float):
        decimal_value = Decimal(str(value))
    else:
        decimal_value = parse_amount(value)
    if decimal_value is None or decimal_value < 0:
        raise FinanceError("BAD_INPUT", f"tolerance 不合法（必须 >= 0 的金额字符串）：{value!r}")
    return decimal_value


def _coerce_confidence_threshold(value: Any) -> float:
    """置信度阈值转 float（置信度不是金额，契约里本来就是 float）。"""
    if value is None or isinstance(value, bool):
        raise FinanceError("BAD_INPUT", f"low_confidence 不合法：{value!r}")
    try:
        threshold = float(value)
    except (TypeError, ValueError) as error:
        raise FinanceError("BAD_INPUT", f"low_confidence 不合法：{value!r}") from error
    if not (0.0 <= threshold <= 1.0):
        raise FinanceError("BAD_INPUT", f"low_confidence 必须在 [0, 1] 内：{value!r}")
    return threshold


# ------------------------------------------------------------------ extraction 读取


def _validate_extraction(document: Any) -> dict:
    """基础 schema 校验（schema_version / columns / rows），不合法抛错。"""
    if not isinstance(document, dict):
        raise FinanceError("BAD_INPUT", "extraction 文档必须是 JSON 对象")

    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise FinanceError("BAD_INPUT", "extraction 缺少 schema_version")
    if schema_version != EXTRACT_SCHEMA_VERSION:
        raise FinanceError(
            "SCHEMA_MISMATCH",
            f"extraction.schema_version 应为 {EXTRACT_SCHEMA_VERSION}，实际为 {schema_version!r}",
        )

    columns = document.get("columns")
    if not isinstance(columns, list):
        raise FinanceError("BAD_INPUT", "extraction.columns 必须是数组")
    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            raise FinanceError("BAD_INPUT", f"extraction.columns[{index}] 必须是对象")
        field = column.get("field")
        if not isinstance(field, str) or not field.strip():
            raise FinanceError("BAD_INPUT", f"extraction.columns[{index}].field 必须是非空字符串")

    rows = document.get("rows")
    if not isinstance(rows, list):
        raise FinanceError("BAD_INPUT", "extraction.rows 必须是数组")
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FinanceError("BAD_INPUT", f"extraction.rows[{row_index}] 必须是对象")
        cells = row.get("cells", {})
        if not isinstance(cells, dict):
            raise FinanceError("BAD_INPUT", f"extraction.rows[{row_index}].cells 必须是对象")

    for key in ("total_rows", "skipped_lines"):
        value = document.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise FinanceError("BAD_INPUT", f"extraction.{key} 必须是数组")
        for item_index, item in enumerate(value):
            if not isinstance(item, dict):
                raise FinanceError("BAD_INPUT", f"extraction.{key}[{item_index}] 必须是对象")
            if key == "total_rows":
                cells = item.get("cells")
                if cells is not None and not isinstance(cells, dict):
                    raise FinanceError(
                        "BAD_INPUT", f"extraction.total_rows[{item_index}].cells 必须是对象"
                    )

    if document.get("source") is not None and not isinstance(document.get("source"), dict):
        raise FinanceError("BAD_INPUT", "extraction.source 必须是对象")
    if document.get("ocr") is not None and not isinstance(document.get("ocr"), dict):
        raise FinanceError("BAD_INPUT", "extraction.ocr 必须是对象")
    return document


def load_extraction(path: Any) -> dict:
    """读取 extraction JSON 并做基础 schema 校验。

    错误码：文件不存在 -> ``EXTRACTION_NOT_FOUND``；JSON 损坏 -> ``BAD_INPUT``；
    schema_version 不符 -> ``SCHEMA_MISMATCH``；读失败 -> ``IO_ERROR``。
    """
    if path is None:
        raise FinanceError("EXTRACTION_NOT_FOUND", "未提供 extraction 文件路径")
    try:
        file_path = os.fspath(path)
    except TypeError as error:
        raise FinanceError("BAD_INPUT", f"extraction 路径不合法：{path!r}") from error
    try:
        with open(file_path, "r", encoding="utf-8-sig") as stream:
            document = json.load(stream)
    except FileNotFoundError as error:
        raise FinanceError("EXTRACTION_NOT_FOUND", f"extraction 文件不存在：{file_path}") from error
    except json.JSONDecodeError as error:
        raise FinanceError("BAD_INPUT", f"extraction 不是合法 JSON：{file_path}（{error}）") from error
    except OSError as error:
        raise FinanceError("IO_ERROR", f"读取 extraction 失败：{file_path}（{error}）") from error
    return _validate_extraction(document)


# ------------------------------------------------------------------ 通用小工具


def _row_index(row: dict, fallback: int) -> Any:
    value = row.get("row_index") if isinstance(row, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _value_text(value: Any) -> str:
    """把 extraction 里的 value 转成文本（金额仍应原本就是字符串）。"""
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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _field_sort_key(field: str) -> tuple[int, str]:
    try:
        return (CANONICAL_FIELD_ORDER.index(field), field)
    except ValueError:
        return (len(CANONICAL_FIELD_ORDER), field)


def _ordered_cells(cells: dict) -> list[tuple[str, dict]]:
    """按规范字段顺序稳定遍历单元格（与 JSON 里键的插入顺序无关）。"""
    return sorted(cells.items(), key=lambda item: _field_sort_key(item[0]))


def _iter_cells(
    rows: list[dict], total_rows: list[dict]
) -> Iterator[tuple[str, int, dict, Any, str, dict]]:
    """稳定遍历全部单元格。

    yield ``(行类型 kind, 序号 index, 行对象, 行号 row_index, 字段, 单元格)``，
    kind 为 ``"data"`` 或 ``"total"``；合计行的 ``row_index`` 为 ``None``。
    """
    for row_index, row in enumerate(rows):
        cells = row.get("cells") or {}
        display_index = _row_index(row, row_index)
        for field, cell in _ordered_cells(cells):
            yield "data", row_index, row, display_index, field, cell
    for total_index, total_row in enumerate(total_rows):
        cells = total_row.get("cells") or {}
        for field, cell in _ordered_cells(cells):
            yield "total", total_index, total_row, None, field, cell


def _preview(values: list[Any], limit: int = 10) -> str:
    """列表预览（硬失败 message 里避免刷屏，完整数据放结构化字段）。"""
    shown = [str(value) for value in values[:limit]]
    if len(values) > limit:
        shown.append(f"…共 {len(values)} 项")
    return ", ".join(shown)


def _fmt_float(value: float) -> str:
    """置信度等非金额 float 的稳定短格式（仅展示用）。"""
    text = format(value, ".6f").rstrip("0").rstrip(".")
    return text if text else "0"


def _diff_exceeds(actual: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    """金额差异判定：|actual-expected| >= tolerance 即超差。

    ``tolerance="0.01"``（默认）时，1 分钱差异必然判失败；
    ``tolerance="0.00"`` 时要求完全相等。这条是“校验层不能放宽”的核心。
    """
    difference = actual - expected
    if difference == 0:
        return False
    return abs(difference) >= tolerance


def _looks_valid_date(text: str) -> bool:
    """财务日期常见写法校验（仅用于 DATE_FORMAT 待确认项）。"""
    candidate = text.strip()
    if not candidate:
        return False
    for pattern in _DATE_PATTERNS:
        match = pattern.match(candidate)
        if match:
            year, month, day = (int(part) for part in match.groups()[:3])
            return _is_valid_ymd(year, month, day)
    match = _DATE_COMPACT_RE.match(candidate)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return _is_valid_ymd(year, month, day)
    return False


def _is_valid_ymd(year: int, month: int, day: int) -> bool:
    if not (1900 <= year <= 2200):
        return False
    if not (1 <= month <= 12):
        return False
    if not (1 <= day <= 31):
        return False
    return True


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ 校验主入口


def verify(
    extraction: dict,
    tolerance: Any = DEFAULT_TOLERANCE,
    low_confidence: Any = DEFAULT_LOW_CONFIDENCE,
    *,
    required_fields: Any = None,
) -> dict:
    """对 extraction 文档做契约第 4 节的全部校验，返回 verify 文档。

    参数
    ----
    extraction : dict
        ``finance-extract/1`` 文档（见 CONTRACT.md 第 3 节）。
    tolerance : str | Decimal
        金额容差，默认 ``"0.01"``。``|差额| >= tolerance`` 判超差；
        ``"0.00"`` 表示要求完全相等（**只用于金额，不做 float 运算**）。
    low_confidence : float
        低置信度阈值，默认 ``0.90``（置信度不是金额，按契约保持 float）。
    required_fields : tuple[str, ...] | None
        必填字段；默认 ``("summary",)``，且只在 extraction 里确实出现该字段时才检查。

    返回
    ----
    dict
        ``finance-verify/1`` 文档：``status`` / ``hard_failures`` / ``review_items``
        / ``checks`` / ``summary`` / ``source``。
    """
    if not isinstance(extraction, dict):
        raise FinanceError("BAD_INPUT", "extraction 必须是 JSON 对象")
    _validate_extraction(extraction)

    tolerance_decimal = _coerce_tolerance(tolerance)
    confidence_threshold = _coerce_confidence_threshold(low_confidence)

    rows: list[dict] = extraction.get("rows") or []
    total_rows: list[dict] = extraction.get("total_rows") or []
    skipped_lines: list[dict] = extraction.get("skipped_lines") or []
    columns: list[dict] = extraction.get("columns") or []
    ocr: dict = extraction.get("ocr") or {}
    source: dict = extraction.get("source") or {}

    hard_failures: list[dict] = []
    review_items: list[dict] = []
    checks: list[dict] = []

    def add_check(check_id: str, name: str, status: str, detail: str) -> None:
        checks.append({"id": check_id, "name": name, "status": status, "detail": detail})

    # ---- 收集字段（列声明 + 实际单元格，保持规范顺序）----
    seen_fields: set[str] = set()
    for column in columns:
        field = column.get("field")
        if isinstance(field, str) and field:
            seen_fields.add(field)
    for row in rows:
        for row_field in (row.get("cells") or {}):
            if isinstance(row_field, str):
                seen_fields.add(row_field)
    for total_row in total_rows:
        for total_field in (total_row.get("cells") or {}):
            if isinstance(total_field, str):
                seen_fields.add(total_field)

    amount_fields = [field for field in AMOUNT_FIELDS if field in seen_fields]
    id_fields = [field for field in ID_FIELDS if field in seen_fields]
    date_fields = [field for field in DATE_FIELDS if field in seen_fields]
    amount_field_set = set(amount_fields)

    # ================================================================
    # 检查 1：NO_AMOUNT_COLUMN —— 没有金额列就无法校验
    # ================================================================
    if amount_fields:
        add_check(
            "no_amount_column",
            "存在金额列",
            "pass",
            "金额类字段：" + ", ".join(amount_fields),
        )
    else:
        hard_failures.append(
            {
                "code": "NO_AMOUNT_COLUMN",
                "message": "未识别到任何金额类列（amount/tax/total/debit/credit/unit_price），无法做金额校验",
            }
        )
        add_check(
            "no_amount_column",
            "存在金额列",
            "fail",
            "未识别到金额类列，无法核对",
        )

    # ================================================================
    # 检查 2：ROW_ACCOUNTING —— 行数守恒 + row_index 不重复
    # ================================================================
    row_index_list = [_row_index(row, index) for index, row in enumerate(rows)]
    duplicate_row_indexes = sorted(
        index for index, count in Counter(row_index_list).items() if count > 1
    )

    n_lines_raw = ocr.get("n_lines")
    n_lines: int | None = None
    if _is_number(n_lines_raw):
        try:
            n_lines_candidate = int(n_lines_raw)
        except (TypeError, ValueError, OverflowError):
            n_lines_candidate = -1
        if n_lines_candidate >= 0 and Decimal(str(n_lines_raw)) == Decimal(n_lines_candidate):
            n_lines = n_lines_candidate

    accounting_issues: list[str] = []
    accounting_payload: dict[str, Any] = {}
    if duplicate_row_indexes:
        accounting_issues.append(f"rows 里出现重复 row_index：{_preview(duplicate_row_indexes)}")
        accounting_payload["duplicate_row_indexes"] = duplicate_row_indexes

    if n_lines is None:
        add_check(
            "row_accounting",
            "行数守恒",
            "fail" if accounting_issues else "skipped",
            (
                f"{accounting_issues[0]}；未提供 ocr.n_lines，无法核对行数守恒"
                if accounting_issues
                else "缺少 ocr.n_lines，跳过 OCR 行覆盖校验"
            ),
        )
    else:
        owners: dict[int, set[str]] = {}

        def add_owner(line_id: Any, owner: str) -> None:
            if isinstance(line_id, bool) or not isinstance(line_id, int):
                return
            owners.setdefault(line_id, set()).add(owner)

        for row_index, row in enumerate(rows):
            owner = f"row[{_row_index(row, row_index)}]"
            for _, cell in _ordered_cells(row.get("cells") or {}):
                add_owner(cell.get("line_id"), owner)
        for total_index, total_row in enumerate(total_rows):
            owner = f"total[{total_index}]"
            for _, cell in _ordered_cells(total_row.get("cells") or {}):
                add_owner(cell.get("line_id"), owner)
        for skipped_index, skipped_line in enumerate(skipped_lines):
            add_owner(skipped_line.get("line_id"), f"skipped[{skipped_index}]")

        expected = set(range(n_lines))
        covered = set(owners)
        missing_lines = sorted(expected - covered)
        unexpected_lines = sorted(covered - expected)
        multi_owner = sorted(line_id for line_id, owner_set in owners.items() if len(owner_set) > 1)

        if missing_lines:
            accounting_issues.append(
                f"未被 rows/total_rows/skipped_lines 覆盖的 OCR 行：{_preview(missing_lines)}"
            )
            accounting_payload["missing_lines"] = missing_lines
        if unexpected_lines:
            accounting_issues.append(
                f"line_id 超出 ocr.n_lines 范围：{_preview(unexpected_lines)}"
            )
            accounting_payload["unexpected_lines"] = unexpected_lines
        if multi_owner:
            accounting_issues.append(
                "OCR 行被重复计入多个去处："
                + _preview(
                    [
                        f"line_id={line_id} -> {'/'.join(sorted(owners[line_id]))}"
                        for line_id in multi_owner
                    ]
                )
            )
            accounting_payload["duplicate_lines"] = multi_owner

        if accounting_issues:
            add_check(
                "row_accounting",
                "行数守恒",
                "fail",
                f"OCR 共 {n_lines} 行，覆盖 {len(covered & expected)} 行；" + "；".join(accounting_issues),
            )
        else:
            add_check(
                "row_accounting",
                "行数守恒",
                "pass",
                f"覆盖 {len(covered)}/{n_lines} 个 OCR 行，无遗漏、无重复计入",
            )

    if accounting_issues:
        hard_failures.append(
            {
                "code": "ROW_ACCOUNTING",
                "message": "行数不守恒：" + "；".join(accounting_issues),
                **accounting_payload,
            }
        )

    # ================================================================
    # 检查 3：DANGEROUS_NUMBER —— 科学计数法 / 编号前导 0 丢失
    # ================================================================
    dangerous_findings: list[dict] = []
    for kind, cell_index, row, display_index, field, cell in _iter_cells(rows, total_rows):
        value = cell.get("value")
        raw = cell.get("raw")
        value_text = _value_text(value)
        finding: dict[str, Any] | None = None

        if isinstance(value, str) and _SCI_RE.search(value):
            finding = {
                "code": "DANGEROUS_NUMBER",
                "message": f"字段 {field} 的 value 出现科学计数法 {value!r}，可能已丢失精度，禁止直接采用",
                "field": field,
                "raw": raw,
                "value": value,
            }
        elif field in ID_FIELDS and isinstance(raw, str) and _SCI_RE.search(raw):
            finding = {
                "code": "DANGEROUS_NUMBER",
                "message": f"编号字段 {field} 的 raw 出现科学计数法 {raw!r}，编号有被数值化风险",
                "field": field,
                "raw": raw,
                "value": value,
            }
        elif field in ID_FIELDS and isinstance(raw, str):
            normalized_raw = _normalize_ocr_text(raw)
            if len(normalized_raw) > 1 and normalized_raw.startswith("0") and not value_text.startswith("0"):
                finding = {
                    "code": "DANGEROUS_NUMBER",
                    "message": (
                        f"编号字段 {field} 的 raw={raw!r} 以 0 开头，但 value={value!r} 丢了前导 0，"
                        "疑似被 Excel/数值化处理，禁止自动修复"
                    ),
                    "field": field,
                    "raw": raw,
                    "value": value,
                }

        if finding is None:
            continue
        if kind == "total":
            finding["row_index"] = None
            finding["row_label"] = row.get("label")
        else:
            finding["row_index"] = display_index
        dangerous_findings.append(finding)

    if dangerous_findings:
        hard_failures.extend(dangerous_findings)
        add_check(
            "dangerous_number",
            "危险数字",
            "fail",
            f"发现 {len(dangerous_findings)} 处危险数字（科学计数法/前导 0 丢失），禁止放行",
        )
    else:
        add_check(
            "dangerous_number",
            "危险数字",
            "pass",
            "未发现科学计数法或编号前导 0 丢失",
        )

    # ================================================================
    # 检查 4：SOURCE_HASH —— 源文件哈希复核（只读，不改原图）
    # ================================================================
    source_path = source.get("path") if isinstance(source.get("path"), str) else ""
    expected_sha = source.get("sha256") if isinstance(source.get("sha256"), str) else ""
    sha_now = ""
    if not source_path:
        add_check(
            "source_hash",
            "源文件哈希",
            "skipped",
            "extraction.source.path 为空，跳过源文件哈希复核",
        )
    elif not os.path.isfile(source_path):
        review_items.append(
            {
                "code": "SOURCE_NOT_FOUND",
                "message": f"源文件不存在，无法复核 SHA-256：{source_path}",
                "path": source_path,
                "expected": expected_sha,
            }
        )
        add_check(
            "source_hash",
            "源文件哈希",
            "review",
            f"源文件不存在，哈希未复核（记录值 {expected_sha or '缺失'}）：{source_path}",
        )
    else:
        try:
            sha_now = _sha256_file(source_path)
        except OSError as error:
            review_items.append(
                {
                    "code": "SOURCE_UNREADABLE",
                    "message": f"源文件读取失败，无法复核 SHA-256：{source_path}（{error}）",
                    "path": source_path,
                }
            )
            add_check("source_hash", "源文件哈希", "review", f"源文件读取失败：{error}")
        else:
            if not expected_sha:
                review_items.append(
                    {
                        "code": "SOURCE_HASH_MISSING",
                        "message": f"源文件存在但 extraction 未记录 SHA-256，无法证明未被改动：{source_path}",
                        "path": source_path,
                        "actual": sha_now,
                    }
                )
                add_check(
                    "source_hash",
                    "源文件哈希",
                    "review",
                    f"extraction 未记录 SHA-256（当前 {sha_now}）",
                )
            elif sha_now.lower() != expected_sha.lower():
                hard_failures.append(
                    {
                        "code": "SOURCE_HASH_CHANGED",
                        "message": f"源文件 SHA-256 与 extraction 记录不一致，文件可能在识别后被改动：{source_path}",
                        "field": "source.sha256",
                        "expected": expected_sha.lower(),
                        "actual": sha_now,
                        "path": source_path,
                    }
                )
                add_check(
                    "source_hash",
                    "源文件哈希",
                    "fail",
                    f"SHA-256 不一致：记录 {expected_sha.lower()}，当前 {sha_now}",
                )
            else:
                add_check(
                    "source_hash",
                    "源文件哈希",
                    "pass",
                    f"SHA-256 一致：{sha_now}",
                )

    # ================================================================
    # 检查 5：UNPARSEABLE_AMOUNT —— 金额必须可解析，绝不猜
    # ================================================================
    amount_values: dict[tuple[str, int, str], Decimal] = {}
    unparseable: list[dict] = []
    amount_cell_count = 0
    for kind, cell_index, row, display_index, field, cell in _iter_cells(rows, total_rows):
        if field not in amount_field_set:
            continue
        amount_cell_count += 1
        raw = cell.get("raw")
        value = cell.get("value")
        parsed = parse_amount(value)
        if parsed is None:
            item: dict[str, Any] = {
                "code": "UNPARSEABLE_AMOUNT",
                "message": f"金额字段 {field} 无法解析为 Decimal：value={value!r}（raw={raw!r}）",
                "field": field,
                "raw": raw,
                "value": value,
            }
            if kind == "total":
                item["row_index"] = None
                item["row_label"] = row.get("label")
            else:
                item["row_index"] = display_index
            unparseable.append(item)
        else:
            amount_values[(kind, cell_index, field)] = parsed

    if unparseable:
        hard_failures.extend(unparseable)
        add_check(
            "unparseable_amount",
            "金额可解析",
            "fail",
            f"{amount_cell_count} 个金额单元格中有 {len(unparseable)} 个无法解析，已按硬失败处理",
        )
    else:
        add_check(
            "unparseable_amount",
            "金额可解析",
            "pass",
            f"{amount_cell_count} 个金额单元格全部可解析为 Decimal",
        )

    # ================================================================
    # 检查 6：TOTAL_ROW_PRESENT —— 有没有合计行/合计值可对照
    # ================================================================
    total_row_missing_fields: list[str] = []
    if not total_rows:
        review_items.append(
            {
                "code": "TOTAL_ROW_MISSING",
                "message": "整表未识别到合计/小计行，无法用“明细=合计”做交叉校验，需人工确认",
            }
        )
        add_check(
            "total_row_present",
            "存在合计行",
            "review",
            "未识别到合计行，无法做明细=合计交叉校验",
        )
    else:
        for field in amount_fields:
            detail_has_value = any(
                field in (row.get("cells") or {})
                and parse_amount((row.get("cells") or {}).get(field, {}).get("value")) is not None
                for row in rows
            )
            total_has_value = any(
                field in (total_row.get("cells") or {})
                for total_row in total_rows
            )
            if detail_has_value and not total_has_value:
                total_row_missing_fields.append(field)
                review_items.append(
                    {
                        "code": "TOTAL_ROW_MISSING",
                        "message": f"金额列 {field} 有明细金额但没有合计行数值，无法做明细=合计校验，需人工确认",
                        "field": field,
                    }
                )
        if total_row_missing_fields:
            add_check(
                "total_row_present",
                "存在合计行",
                "review",
                "合计行缺少金额列：" + ", ".join(total_row_missing_fields),
            )
        else:
            add_check(
                "total_row_present",
                "存在合计行",
                "pass",
                f"合计行 {len(total_rows)} 行，金额列均有合计值可对照",
            )

    # ================================================================
    # 检查 7：DETAIL_SUM_EQUALS_TOTAL —— 明细合计 = 表内合计（最硬的一条）
    # ================================================================
    comparisons: list[str] = []
    total_mismatches: list[dict] = []
    for field in amount_fields:
        detail_values: list[Decimal] = []
        detail_row_indexes: list[Any] = []
        for row_index, row in enumerate(rows):
            cell = (row.get("cells") or {}).get(field)
            if not isinstance(cell, dict):
                continue
            parsed = parse_amount(cell.get("value"))
            if parsed is None:
                continue
            detail_values.append(parsed)
            detail_row_indexes.append(_row_index(row, row_index))

        candidates: list[tuple[int, Decimal]] = []
        for total_index, total_row in enumerate(total_rows):
            cell = (total_row.get("cells") or {}).get(field)
            if not isinstance(cell, dict):
                continue
            parsed = parse_amount(cell.get("value"))
            if parsed is not None:
                candidates.append((total_index, parsed))

        if not detail_values or not candidates:
            # 没有明细值或没有合计值：已在检查 6 记 review，这里不重复报
            continue

        detail_sum = sum(detail_values, ZERO)
        matched = [
            (total_index, candidate)
            for total_index, candidate in candidates
            if not _diff_exceeds(detail_sum, candidate, tolerance_decimal)
        ]
        if matched:
            comparisons.append(
                f"{field}: 明细 {_fmt(detail_sum)} == 票面 {_fmt(matched[0][1])}"
            )
            continue

        # 取差额最小的合计值作为对照，稳定（并列取第一个）
        nearest_total_index, nearest_value = min(
            candidates, key=lambda item: abs(detail_sum - item[1])
        )
        difference = detail_sum - nearest_value
        comparisons.append(
            f"{field}: 明细 {_fmt(detail_sum)} != 票面 {_fmt(nearest_value)}（差 {_fmt(abs(difference))}）"
        )
        total_mismatches.append(
            {
                "code": "TOTAL_MISMATCH",
                "message": (
                    f"{field} 明细合计 {_fmt(detail_sum)}，票面合计 {_fmt(nearest_value)}，"
                    f"差额 {_fmt(abs(difference))}，超出容差 {format(tolerance_decimal, 'f')}"
                ),
                "field": field,
                "expected": _fmt(nearest_value),
                "actual": _fmt(detail_sum),
                "delta": _fmt(difference),
                "rows": detail_row_indexes,
            }
        )

    if total_mismatches:
        hard_failures.extend(total_mismatches)
        add_check(
            "detail_sum_equals_total",
            "明细合计 = 表内合计",
            "fail",
            "；".join(comparisons),
        )
    elif comparisons:
        add_check(
            "detail_sum_equals_total",
            "明细合计 = 表内合计",
            "pass",
            "；".join(comparisons),
        )
    else:
        add_check(
            "detail_sum_equals_total",
            "明细合计 = 表内合计",
            "skipped",
            "没有“明细金额 + 合计行金额”可对照，跳过明细=合计校验",
        )

    # ================================================================
    # 检查 8：DUPLICATE_ROW —— 同编号同金额重复计入
    # ================================================================
    if not id_fields:
        add_check(
            "duplicate_row",
            "重复计入",
            "skipped",
            "未识别到编号类字段（invoice_code/invoice_no/voucher_no/account），跳过重复行检查",
        )
    else:
        groups: dict[tuple, list[Any]] = {}
        key_descriptions: dict[tuple, str] = {}
        for row_index, row in enumerate(rows):
            cells = row.get("cells") or {}
            id_parts: list[tuple[str, str]] = []
            for field in id_fields:
                cell = cells.get(field)
                if not isinstance(cell, dict):
                    continue
                text = _value_text(cell.get("value")).strip()
                if text:
                    id_parts.append((field, text))
            if not id_parts:
                # 没有编号值，无法判断“同编号”，不参与去重
                continue
            amount_parts: list[tuple[str, str]] = []
            for field in amount_fields:
                cell = cells.get(field)
                if not isinstance(cell, dict):
                    continue
                text = _value_text(cell.get("value")).strip()
                if text:
                    amount_parts.append((field, text))
            key = (tuple(id_parts), tuple(amount_parts))
            groups.setdefault(key, []).append(_row_index(row, row_index))
            if key not in key_descriptions:
                key_descriptions[key] = "、".join(
                    f"{field}={value}" for field, value in list(id_parts) + list(amount_parts)
                )

        duplicate_groups = [(key, indexes) for key, indexes in groups.items() if len(indexes) > 1]
        if duplicate_groups:
            for key, indexes in duplicate_groups:
                hard_failures.append(
                    {
                        "code": "DUPLICATE_ROW",
                        "message": (
                            f"重复计入：行 {', '.join(str(index) for index in indexes)} 的编号与金额完全相同"
                            f"（{key_descriptions[key]}）"
                        ),
                        "rows": indexes,
                        "key": key_descriptions[key],
                    }
                )
            add_check(
                "duplicate_row",
                "重复计入",
                "fail",
                f"发现 {len(duplicate_groups)} 组重复行（同编号同金额）",
            )
        else:
            add_check(
                "duplicate_row",
                "重复计入",
                "pass",
                f"{len(groups)} 行参与去重，未发现同编号同金额的重复行",
            )

    # ================================================================
    # 检查 9：LOW_CONFIDENCE —— 低置信度单元格（待人工确认）
    # ================================================================
    low_confidence_items: list[dict] = []
    confidence_cells = 0
    for kind, cell_index, row, display_index, field, cell in _iter_cells(rows, total_rows):
        confidence = cell.get("confidence")
        if not _is_number(confidence):
            continue
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError, OverflowError):
            continue
        confidence_cells += 1
        if confidence_value >= confidence_threshold:
            continue
        item: dict[str, Any] = {
            "code": "LOW_CONFIDENCE",
            "message": (
                f"字段 {field} 置信度 {_fmt_float(confidence_value)} "
                f"低于阈值 {_fmt_float(confidence_threshold)}，需人工复核"
            ),
            "field": field,
            "raw": cell.get("raw"),
            "confidence": confidence_value,
            "value": cell.get("value"),
        }
        if kind == "total":
            item["row_index"] = None
            item["row_label"] = row.get("label")
        else:
            item["row_index"] = display_index
        low_confidence_items.append(item)

    if low_confidence_items:
        review_items.extend(low_confidence_items)
        add_check(
            "low_confidence",
            "置信度",
            "review",
            f"{confidence_cells} 个带置信度的单元格中有 {len(low_confidence_items)} 个低于阈值 "
            f"{_fmt_float(confidence_threshold)}",
        )
    else:
        add_check(
            "low_confidence",
            "置信度",
            "pass",
            f"{confidence_cells} 个带置信度的单元格均不低于阈值 {_fmt_float(confidence_threshold)}",
        )

    # ================================================================
    # 检查 10：TAX_MISMATCH —— 金额 × 税率 = 税额（待确认）
    # ================================================================
    tax_checked = 0
    tax_mismatches: list[dict] = []
    if {"amount", "tax", "tax_rate"} <= seen_fields:
        for row_index, row in enumerate(rows):
            cells = row.get("cells") or {}
            amount_cell = cells.get("amount")
            tax_cell = cells.get("tax")
            rate_cell = cells.get("tax_rate")
            if not (isinstance(amount_cell, dict) and isinstance(tax_cell, dict) and isinstance(rate_cell, dict)):
                continue
            amount_value = parse_amount(amount_cell.get("value"))
            tax_value = parse_amount(tax_cell.get("value"))
            rate_value = parse_rate(rate_cell.get("value"))
            if amount_value is None or tax_value is None or rate_value is None:
                continue
            tax_checked += 1
            expected_tax = amount_value * rate_value
            if not _diff_exceeds(tax_value, expected_tax, tolerance_decimal):
                continue
            difference = tax_value - expected_tax
            tax_mismatches.append(
                {
                    "code": "TAX_MISMATCH",
                    "message": (
                        f"行 {_row_index(row, row_index)} 金额 {_fmt(amount_value)} × 税率 "
                        f"{format(rate_value, 'f')} = {_fmt(expected_tax)}，与税额 {_fmt(tax_value)} 相差 "
                        f"{_fmt(abs(difference))}，需人工确认"
                    ),
                    "row_index": _row_index(row, row_index),
                    "field": "tax",
                    "expected": _fmt(expected_tax),
                    "actual": _fmt(tax_value),
                    "delta": _fmt(difference),
                    "tax_rate": format(rate_value, "f"),
                    "raw": rate_cell.get("raw"),
                }
            )
        if tax_mismatches:
            review_items.extend(tax_mismatches)
            add_check(
                "tax_mismatch",
                "金额×税率 = 税额",
                "review",
                f"检查 {tax_checked} 行，{len(tax_mismatches)} 行金额×税率≠税额（待人工确认）",
            )
        elif tax_checked:
            add_check(
                "tax_mismatch",
                "金额×税率 = 税额",
                "pass",
                f"检查 {tax_checked} 行，金额×税率与税额一致",
            )
        else:
            add_check(
                "tax_mismatch",
                "金额×税率 = 税额",
                "skipped",
                "没有同时具备金额/税额/税率的行，跳过",
            )
    else:
        add_check(
            "tax_mismatch",
            "金额×税率 = 税额",
            "skipped",
            "extraction 未包含 金额/税额/税率 三者，跳过",
        )

    # ================================================================
    # 检查 11：QTY_PRICE_MISMATCH —— 数量 × 单价 = 金额（待确认）
    # ================================================================
    qty_checked = 0
    qty_mismatches: list[dict] = []
    if {"qty", "unit_price", "amount"} <= seen_fields:
        for row_index, row in enumerate(rows):
            cells = row.get("cells") or {}
            qty_cell = cells.get("qty")
            price_cell = cells.get("unit_price")
            amount_cell = cells.get("amount")
            if not (isinstance(qty_cell, dict) and isinstance(price_cell, dict) and isinstance(amount_cell, dict)):
                continue
            qty_value = parse_amount(qty_cell.get("value"))
            price_value = parse_amount(price_cell.get("value"))
            amount_value = parse_amount(amount_cell.get("value"))
            if qty_value is None or price_value is None or amount_value is None:
                continue
            qty_checked += 1
            expected_amount = qty_value * price_value
            if not _diff_exceeds(amount_value, expected_amount, tolerance_decimal):
                continue
            difference = amount_value - expected_amount
            qty_mismatches.append(
                {
                    "code": "QTY_PRICE_MISMATCH",
                    "message": (
                        f"行 {_row_index(row, row_index)} 数量 {format(qty_value, 'f')} × 单价 "
                        f"{_fmt(price_value)} = {_fmt(expected_amount)}，与金额 {_fmt(amount_value)} 相差 "
                        f"{_fmt(abs(difference))}，需人工确认"
                    ),
                    "row_index": _row_index(row, row_index),
                    "field": "amount",
                    "expected": _fmt(expected_amount),
                    "actual": _fmt(amount_value),
                    "delta": _fmt(difference),
                }
            )
        if qty_mismatches:
            review_items.extend(qty_mismatches)
            add_check(
                "qty_price_mismatch",
                "数量×单价 = 金额",
                "review",
                f"检查 {qty_checked} 行，{len(qty_mismatches)} 行数量×单价≠金额（待人工确认）",
            )
        elif qty_checked:
            add_check(
                "qty_price_mismatch",
                "数量×单价 = 金额",
                "pass",
                f"检查 {qty_checked} 行，数量×单价与金额一致",
            )
        else:
            add_check(
                "qty_price_mismatch",
                "数量×单价 = 金额",
                "skipped",
                "没有同时具备数量/单价/金额的行，跳过",
            )
    else:
        add_check(
            "qty_price_mismatch",
            "数量×单价 = 金额",
            "skipped",
            "extraction 未包含 数量/单价/金额 三者，跳过",
        )

    # ================================================================
    # 检查 12：MISSING_REQUIRED_FIELD —— 必填字段缺失（待确认）
    # ================================================================
    if required_fields is None:
        required = tuple(DEFAULT_REQUIRED_FIELDS)
    elif isinstance(required_fields, str):
        required = (required_fields,)
    else:
        required = tuple(str(field) for field in required_fields)
    active_required = [field for field in required if field in seen_fields]
    missing_fields: list[dict] = []
    if not active_required:
        add_check(
            "missing_required_field",
            "必填字段",
            "skipped",
            f"extraction 未出现必填字段（{', '.join(required) or '无'}），跳过",
        )
    else:
        for row_index, row in enumerate(rows):
            cells = row.get("cells") or {}
            for field in active_required:
                cell = cells.get(field)
                text = _value_text(cell.get("value")).strip() if isinstance(cell, dict) else ""
                if text:
                    continue
                missing_fields.append(
                    {
                        "code": "MISSING_REQUIRED_FIELD",
                        "message": f"行 {_row_index(row, row_index)} 缺少必填字段 {field}，需人工确认",
                        "row_index": _row_index(row, row_index),
                        "field": field,
                        "raw": cell.get("raw") if isinstance(cell, dict) else None,
                    }
                )
        if missing_fields:
            review_items.extend(missing_fields)
            add_check(
                "missing_required_field",
                "必填字段",
                "review",
                f"必填字段（{', '.join(active_required)}）共缺失 {len(missing_fields)} 处",
            )
        else:
            add_check(
                "missing_required_field",
                "必填字段",
                "pass",
                f"必填字段（{', '.join(active_required)}）全部存在",
            )

    # ================================================================
    # 检查 13：DATE_FORMAT —— 日期格式（待确认）
    # ================================================================
    date_items: list[dict] = []
    if not date_fields:
        add_check(
            "date_format",
            "日期格式",
            "skipped",
            "extraction 未包含日期字段，跳过",
        )
    else:
        date_cells = 0
        for row_index, row in enumerate(rows):
            cells = row.get("cells") or {}
            for field in date_fields:
                cell = cells.get(field)
                if not isinstance(cell, dict):
                    continue
                text = _value_text(cell.get("value")).strip()
                if not text:
                    continue
                date_cells += 1
                if _looks_valid_date(text):
                    continue
                date_items.append(
                    {
                        "code": "DATE_FORMAT",
                        "message": f"行 {_row_index(row, row_index)} 日期格式可疑：{text!r}，需人工确认",
                        "row_index": _row_index(row, row_index),
                        "field": field,
                        "raw": cell.get("raw"),
                        "value": text,
                    }
                )
        if date_items:
            review_items.extend(date_items)
            add_check(
                "date_format",
                "日期格式",
                "review",
                f"{date_cells} 个日期中有 {len(date_items)} 个格式可疑（待人工确认）",
            )
        else:
            add_check(
                "date_format",
                "日期格式",
                "pass",
                f"{date_cells} 个日期格式均可识别",
            )

    # ================================================================
    # 汇总
    # ================================================================
    if hard_failures:
        status = "fail"
    elif review_items:
        status = "needs_review"
    else:
        status = "pass"

    return {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "status": status,
        "hard_failures": hard_failures,
        "review_items": review_items,
        "checks": checks,
        "summary": {
            "n_rows": len(rows),
            "n_total_rows": len(total_rows),
            "n_amount_fields": len(amount_fields),
            "n_hard_failures": len(hard_failures),
            "n_review_items": len(review_items),
        },
        "source": {
            "path": source_path,
            "sha256": expected_sha,
            "sha256_now": sha_now,
        },
    }


__all__ = [
    "FinanceError",
    "parse_amount",
    "parse_rate",
    "quantize_amount",
    "format_amount",
    "load_extraction",
    "verify",
    "EXTRACT_SCHEMA_VERSION",
    "VERIFY_SCHEMA_VERSION",
    "DEFAULT_TOLERANCE",
    "DEFAULT_LOW_CONFIDENCE",
    "AMOUNT_FIELDS",
    "ID_FIELDS",
    "DATE_FIELDS",
    "DEFAULT_REQUIRED_FIELDS",
    "CANONICAL_FIELD_ORDER",
]
