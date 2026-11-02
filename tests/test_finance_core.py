#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验层 + xlsx 导出层单元测试（纯 unittest，无第三方测试框架）。

运行方式（项目根目录下任选其一）::

    python -m unittest -v tests.test_finance_core
    python -m unittest discover -s tests -p "test_*.py" -v
    python tests/test_finance_core.py -v

覆盖的最硬验收项：

* 金额清洗 10+ 例（全角/货币符号/千分位/括号负数/科学计数法/空串……）；
* 明细合计 == 票面合计 时 pass；
* **人为把合计改坏 1 分钱 -> status=fail 且 hard_failures 含 TOTAL_MISMATCH**；
* 行数不守恒 -> ROW_ACCOUNTING；科学计数法/编号丢前导 0 -> DANGEROUS_NUMBER；
* 同编号同金额重复 -> DUPLICATE_ROW；
* xlsx：拒绝覆盖源文件/extraction 文件、4 个 sheet、编号列文本、金额列数值、
  两次导出逐字节一致、created/modified 取 source.mtime。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = PROJECT_ROOT / "lib"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures-core"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import finance_core  # noqa: E402
import xlsx_export  # noqa: E402
from finance_core import (  # noqa: E402
    FinanceError,
    format_amount,
    load_extraction,
    parse_amount,
    parse_rate,
    quantize_amount,
    verify,
)
from openpyxl import load_workbook  # noqa: E402


# ------------------------------------------------------------------ 测试辅助


def load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def hard_codes(document: dict) -> list[str]:
    return [item.get("code") for item in document.get("hard_failures") or []]


def review_codes(document: dict) -> list[str]:
    return [item.get("code") for item in document.get("review_items") or []]


def check_status(document: dict) -> dict[str, str]:
    return {check["id"]: check["status"] for check in document.get("checks") or []}


def hard_failures_with_code(document: dict, code: str) -> list[dict]:
    return [item for item in document.get("hard_failures") or [] if item.get("code") == code]


def review_items_with_code(document: dict, code: str) -> list[dict]:
    return [item for item in document.get("review_items") or [] if item.get("code") == code]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ------------------------------------------------------------------ 金额清洗


class ParseAmountTests(unittest.TestCase):
    def test_currency_symbol_and_thousands(self):
        self.assertEqual(parse_amount("￥1,234.50"), Decimal("1234.50"))
        self.assertEqual(parse_amount("¥1,234.50"), Decimal("1234.50"))
        self.assertEqual(parse_amount("$1,234.50"), Decimal("1234.50"))

    def test_negative_plain(self):
        self.assertEqual(parse_amount("-12.5"), Decimal("-12.5"))

    def test_parentheses_negative_fullwidth(self):
        self.assertEqual(parse_amount("（1,000.00）"), Decimal("-1000.00"))
        self.assertEqual(parse_amount("(1,000.00)"), Decimal("-1000.00"))

    def test_fullwidth_digits(self):
        self.assertEqual(parse_amount("１２３"), Decimal("123"))
        self.assertEqual(parse_amount("１２３．４５"), Decimal("123.45"))

    def test_empty_and_none(self):
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount("   "))
        self.assertIsNone(parse_amount(None))

    def test_scientific_notation_rejected(self):
        self.assertIsNone(parse_amount("1.23E+15"))
        self.assertIsNone(parse_amount("1.23e15"))
        self.assertIsNone(parse_amount("1E5"))

    def test_positive_sign(self):
        self.assertEqual(parse_amount("+88.00"), Decimal("88.00"))

    def test_spaces_removed(self):
        self.assertEqual(parse_amount(" 1 234.50 "), Decimal("1234.50"))
        self.assertEqual(parse_amount("\u3000￥ 1,234.50\u3000"), Decimal("1234.50"))

    def test_invalid_thousand_grouping(self):
        self.assertIsNone(parse_amount("1,23.45"))
        self.assertIsNone(parse_amount("12,34"))

    def test_leading_dot(self):
        self.assertEqual(parse_amount(".5"), Decimal("0.5"))

    def test_trailing_cny_unit(self):
        self.assertEqual(parse_amount("1,000.00元"), Decimal("1000.00"))
        self.assertEqual(parse_amount("1,000.00元整"), Decimal("1000.00"))
        self.assertEqual(parse_amount("人民币 1,000.00"), Decimal("1000.00"))

    def test_accounting_trailing_minus(self):
        self.assertEqual(parse_amount("5-"), Decimal("-5"))
        self.assertEqual(parse_amount("1,234.50-"), Decimal("-1234.50"))

    def test_garbage_rejected(self):
        self.assertIsNone(parse_amount("abc"))
        self.assertIsNone(parse_amount("12.3.4"))
        self.assertIsNone(parse_amount("--5"))
        self.assertIsNone(parse_amount("("))
        self.assertIsNone(parse_amount("1,234.50 元 元"))

    def test_zero_not_negative_zero(self):
        self.assertEqual(parse_amount("0"), Decimal("0"))
        self.assertEqual(parse_amount("(0.00)"), Decimal("0"))
        self.assertEqual(format_amount(parse_amount("-0.00")), "0.00")

    def test_float_input_rejected(self):
        self.assertIsNone(parse_amount(12.5))
        self.assertIsNone(parse_amount(True))

    def test_int_and_decimal_input(self):
        self.assertEqual(parse_amount(7), Decimal("7"))
        self.assertEqual(parse_amount(Decimal("8.80")), Decimal("8.80"))

    def test_e_marker_only_invalid(self):
        # 不是科学计数法，但也不是合法金额
        self.assertIsNone(parse_amount("12E"))
        self.assertIsNone(parse_amount("E12"))


class QuantizeTests(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(quantize_amount("2.345"), Decimal("2.35"))
        self.assertEqual(quantize_amount("-2.345"), Decimal("-2.35"))
        self.assertEqual(quantize_amount(Decimal("1.005")), Decimal("1.01"))

    def test_default_two_places(self):
        self.assertEqual(format_amount("1234.5"), "1234.50")
        self.assertEqual(format_amount("0"), "0.00")
        self.assertEqual(format_amount("-12.5"), "-12.50")

    def test_float_rejected(self):
        with self.assertRaises(FinanceError) as raised:
            quantize_amount(0.1 + 0.2)
        self.assertEqual(raised.exception.code, "BAD_INPUT")

    def test_parse_rate(self):
        self.assertEqual(parse_rate("6%"), Decimal("0.06"))
        self.assertEqual(parse_rate("13％"), Decimal("0.13"))
        self.assertEqual(parse_rate("0.06"), Decimal("0.06"))
        self.assertIsNone(parse_rate(""))
        self.assertIsNone(parse_rate("税率"))


# ------------------------------------------------------------------ load_extraction


class LoadExtractionTests(unittest.TestCase):
    def test_loads_valid_fixture(self):
        document = load_extraction(FIXTURES_DIR / "valid_extraction.json")
        self.assertEqual(document["schema_version"], "finance-extract/1")
        self.assertEqual(len(document["rows"]), 3)

    def test_missing_file(self):
        with self.assertRaises(FinanceError) as raised:
            load_extraction(FIXTURES_DIR / "does_not_exist.json")
        self.assertEqual(raised.exception.code, "EXTRACTION_NOT_FOUND")

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(FinanceError) as raised:
                load_extraction(path)
            self.assertEqual(raised.exception.code, "BAD_INPUT")

    def test_schema_version_mismatch(self):
        document = load_fixture("valid_extraction.json")
        document["schema_version"] = "finance-extract/2"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong_schema.json"
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(FinanceError) as raised:
                load_extraction(path)
            self.assertEqual(raised.exception.code, "SCHEMA_MISMATCH")

    def test_rows_wrong_type(self):
        document = load_fixture("valid_extraction.json")
        document["rows"] = {"not": "a list"}
        with self.assertRaises(FinanceError) as raised:
            finance_core._validate_extraction(document)
        self.assertEqual(raised.exception.code, "BAD_INPUT")

    def test_column_without_field(self):
        document = load_fixture("valid_extraction.json")
        document["columns"][0] = {"header": "序号"}
        with self.assertRaises(FinanceError) as raised:
            finance_core._validate_extraction(document)
        self.assertEqual(raised.exception.code, "BAD_INPUT")


# ------------------------------------------------------------------ verify：通过


class VerifyPassTests(unittest.TestCase):
    def setUp(self):
        self.document = load_fixture("valid_extraction.json")

    def test_status_pass(self):
        report = verify(self.document)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["hard_failures"], [])
        self.assertEqual(report["review_items"], [])
        self.assertEqual(report["schema_version"], "finance-verify/1")
        summary = report["summary"]
        self.assertEqual(summary["n_rows"], 3)
        self.assertEqual(summary["n_total_rows"], 1)
        self.assertEqual(summary["n_amount_fields"], 1)

    def test_checks_are_complete_and_stable(self):
        report = verify(self.document)
        statuses = check_status(report)
        expected_ids = [
            "no_amount_column",
            "row_accounting",
            "dangerous_number",
            "source_hash",
            "unparseable_amount",
            "total_row_present",
            "detail_sum_equals_total",
            "duplicate_row",
            "low_confidence",
            "tax_mismatch",
            "qty_price_mismatch",
            "missing_required_field",
            "date_format",
        ]
        self.assertEqual([check["id"] for check in report["checks"]], expected_ids)
        self.assertEqual(statuses["detail_sum_equals_total"], "pass")
        self.assertEqual(statuses["detail_sum_equals_total"], "pass")
        self.assertIn("2500.50", report["checks"][6]["detail"])
        self.assertEqual(statuses["row_accounting"], "pass")
        self.assertEqual(statuses["source_hash"], "skipped")
        # 每个 check 都有可读 detail，且状态合法
        for check in report["checks"]:
            self.assertIn(check["status"], ("pass", "fail", "review", "skipped"))
            self.assertTrue(check["detail"])

    def test_verify_json_roundtrip_and_deterministic(self):
        first = verify(self.document)
        second = verify(copy.deepcopy(self.document))
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=False),
            json.dumps(second, ensure_ascii=False, sort_keys=False),
        )
        json.dumps(first, ensure_ascii=False)  # 必须可 JSON 往返

    def test_zero_tolerance_requires_exact_match(self):
        report = verify(self.document, tolerance="0.00")
        self.assertEqual(report["status"], "pass")


# ------------------------------------------------------------------ verify：1 分钱篡改（最硬验收）


class VerifyTamperedTotalTests(unittest.TestCase):
    def setUp(self):
        self.tampered = load_fixture("tampered_total_extraction.json")

    def test_one_cent_tamper_must_fail(self):
        report = verify(self.tampered)
        self.assertEqual(report["status"], "fail")
        mismatches = hard_failures_with_code(report, "TOTAL_MISMATCH")
        self.assertEqual(len(mismatches), 1)
        failure = mismatches[0]
        self.assertEqual(failure["field"], "amount")
        self.assertEqual(failure["expected"], "2500.49")
        self.assertEqual(failure["actual"], "2500.50")
        self.assertEqual(failure["delta"], "0.01")
        self.assertEqual(failure["rows"], [1, 2, 3])
        # 金额字段必须是字符串（不许 float 输出）
        for key in ("expected", "actual", "delta"):
            self.assertIsInstance(failure[key], str)
        self.assertEqual(report["checks"][6]["status"], "fail")

    def test_tolerance_larger_than_delta_passes(self):
        # 0.01 差额在 tolerance=0.02（严格小于才放行）下不超差，用于证明容差语义
        report = verify(self.tampered, tolerance="0.02")
        self.assertEqual(report["status"], "pass")

    def test_one_cent_tamper_caught_by_tolerance_zero(self):
        report = verify(self.tampered, tolerance="0.00")
        self.assertEqual(report["status"], "fail")


# ------------------------------------------------------------------ verify：行数守恒 / 危险数字 / 重复行


class VerifyRowAccountingTests(unittest.TestCase):
    def setUp(self):
        self.document = load_fixture("valid_extraction.json")

    def test_missing_ocr_line_fails(self):
        document = copy.deepcopy(self.document)
        document["ocr"]["n_lines"] = 20  # 0..19，实际只覆盖 0..17
        report = verify(document)
        failures = hard_failures_with_code(report, "ROW_ACCOUNTING")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["missing_lines"], [18, 19])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(check_status(report)["row_accounting"], "fail")

    def test_removed_skipped_lines_break_coverage(self):
        document = copy.deepcopy(self.document)
        document["skipped_lines"] = []
        report = verify(document)
        failures = hard_failures_with_code(report, "ROW_ACCOUNTING")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["missing_lines"], [0, 1])
        self.assertEqual(report["status"], "fail")

    def test_line_double_counted_fails(self):
        document = copy.deepcopy(self.document)
        # 让第二行的 summary 单元格借用第一行 amount 的 line_id=6，构成重复计入
        document["rows"][1]["cells"]["summary"]["line_id"] = 6
        report = verify(document)
        failures = hard_failures_with_code(report, "ROW_ACCOUNTING")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["duplicate_lines"], [6])
        self.assertIn(10, failures[0]["missing_lines"])

    def test_duplicate_row_index_fails(self):
        report = verify(load_fixture("dirty_extraction.json"))
        failures = hard_failures_with_code(report, "ROW_ACCOUNTING")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["duplicate_row_indexes"], [3])
        self.assertEqual(report["status"], "fail")


class VerifyDangerousNumberTests(unittest.TestCase):
    def test_dirty_fixture_flags_scientific_and_leading_zero(self):
        report = verify(load_fixture("dirty_extraction.json"))
        failures = hard_failures_with_code(report, "DANGEROUS_NUMBER")
        self.assertGreaterEqual(len(failures), 2)
        fields = [failure.get("field") for failure in failures]
        self.assertIn("invoice_no", fields)
        self.assertIn("amount", fields)
        leading_zero = [f for f in failures if f.get("field") == "invoice_no"][0]
        self.assertEqual(leading_zero["raw"], "00123456")
        self.assertEqual(leading_zero["value"], "123456")
        self.assertEqual(report["status"], "fail")

    def test_scientific_amount_is_unparseable_too(self):
        report = verify(load_fixture("dirty_extraction.json"))
        self.assertIn("UNPARSEABLE_AMOUNT", hard_codes(report))

    def test_valid_extraction_has_no_dangerous_number(self):
        report = verify(load_fixture("valid_extraction.json"))
        self.assertEqual(check_status(report)["dangerous_number"], "pass")


class VerifyDuplicateRowTests(unittest.TestCase):
    def test_duplicate_id_and_amount_fails(self):
        report = verify(load_fixture("dirty_extraction.json"))
        failures = hard_failures_with_code(report, "DUPLICATE_ROW")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["rows"], [3, 3])
        self.assertIn("invoice_no=00123458", failures[0]["key"])
        self.assertEqual(report["status"], "fail")

    def test_valid_extraction_has_no_duplicate(self):
        report = verify(load_fixture("valid_extraction.json"))
        self.assertEqual(check_status(report)["duplicate_row"], "pass")


class VerifyNoAmountColumnTests(unittest.TestCase):
    def test_no_amount_column_fails(self):
        document = load_fixture("valid_extraction.json")
        document["columns"] = [
            {"field": "seq", "header": "序号"},
            {"field": "summary", "header": "摘要"},
        ]
        for row in document["rows"]:
            row["cells"].pop("amount", None)
        document["total_rows"] = []
        report = verify(document)
        self.assertIn("NO_AMOUNT_COLUMN", hard_codes(report))
        self.assertEqual(report["status"], "fail")


# ------------------------------------------------------------------ verify：待确认项


class VerifyReviewItemTests(unittest.TestCase):
    def _build_review_document(self) -> dict:
        document = load_fixture("valid_extraction.json")
        columns = document["columns"]
        for field, header in (
            ("tax_rate", "税率"),
            ("tax", "税额"),
            ("qty", "数量"),
            ("unit_price", "单价"),
        ):
            columns.append({"field": field, "header": header, "x_min": 0, "x_max": 0, "align": "right"})
        # 新增单元格复用本行已有 line_id（同一 OCR 行被拆进多个单元格），
        # 避免破坏 n_lines 的行数守恒。
        extra_cells = {
            0: {"tax_rate": "6%", "tax": "60.00", "qty": "2", "unit_price": "700.00"},
            1: {"tax_rate": "6%", "tax": "48.00", "qty": "1", "unit_price": "800.00"},
            2: {"tax_rate": "6%", "tax": "30.03", "qty": "1", "unit_price": "500.50"},
        }
        for index, row in enumerate(document["rows"]):
            amount_line_id = row["cells"]["amount"]["line_id"]
            for field, value in extra_cells[index].items():
                row["cells"][field] = {
                    "raw": value,
                    "value": value,
                    "confidence": 0.95,
                    "bbox": row["cells"]["amount"]["bbox"],
                    "line_id": amount_line_id,
                }
        # 金额仍守恒：新增的税列为 60.00+48.00+30.03=138.03，合计行补上
        document["total_rows"][0]["cells"]["tax"] = {
            "raw": "138.03",
            "value": "138.03",
            "confidence": 0.98,
            "bbox": document["total_rows"][0]["cells"]["amount"]["bbox"],
            "line_id": document["total_rows"][0]["cells"]["amount"]["line_id"],
        }
        # 低置信度
        document["rows"][2]["cells"]["amount"]["confidence"] = 0.50
        # 日期格式可疑
        document["rows"][1]["cells"]["date"]["value"] = "2026/99/01"
        # 必填字段缺失
        document["rows"][1]["cells"]["summary"]["value"] = ""
        return document

    def test_review_items_and_needs_review_status(self):
        report = verify(self._build_review_document())
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["hard_failures"], [])
        codes = review_codes(report)
        for code in (
            "LOW_CONFIDENCE",
            "TAX_MISMATCH",
            "QTY_PRICE_MISMATCH",
            "MISSING_REQUIRED_FIELD",
            "DATE_FORMAT",
        ):
            self.assertIn(code, codes)
        low = review_items_with_code(report, "LOW_CONFIDENCE")[0]
        self.assertAlmostEqual(low["confidence"], 0.50)
        tax = review_items_with_code(report, "TAX_MISMATCH")[0]
        self.assertEqual(tax["actual"], "60.00")
        self.assertEqual(tax["expected"], "72.00")
        statuses = check_status(report)
        self.assertEqual(statuses["tax_mismatch"], "review")
        self.assertEqual(statuses["qty_price_mismatch"], "review")
        self.assertEqual(statuses["low_confidence"], "review")
        self.assertEqual(statuses["date_format"], "review")
        self.assertEqual(statuses["missing_required_field"], "review")
        # 金额合计仍与票面一致：10 分钱级别的税额差异只是待确认，不污染硬校验
        self.assertEqual(statuses["detail_sum_equals_total"], "pass")

    def test_total_row_missing_is_review_not_fail(self):
        document = load_fixture("valid_extraction.json")
        document["total_rows"] = []
        # 合计行的 OCR 行必须仍有明确去向（契约行数守恒），否则会先报 ROW_ACCOUNTING；
        # 这里把它记进 skipped_lines（reason=total）后再断言 TOTAL_ROW_MISSING 只进 review。
        document["skipped_lines"].append(
            {
                "text": "合计 2,500.50",
                "bbox": [900, 678, 1080, 702],
                "line_id": 17,
                "reason": "total",
            }
        )
        report = verify(document)
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["hard_failures"], [])
        self.assertIn("TOTAL_ROW_MISSING", review_codes(report))
        self.assertEqual(check_status(report)["total_row_present"], "review")
        self.assertEqual(check_status(report)["detail_sum_equals_total"], "skipped")


class VerifySourceHashTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source_path = Path(self.directory.name) / "source.png"
        self.source_bytes = b"\x89PNG\r\n\x1a\n fake source bytes"
        self.source_path.write_bytes(self.source_bytes)
        self.digest = sha256_bytes(self.source_bytes)

    def _document_with_source(self, sha256: str) -> dict:
        document = load_fixture("valid_extraction.json")
        document["source"] = {
            "path": str(self.source_path),
            "sha256": sha256,
            "bytes": len(self.source_bytes),
            "width": 1600,
            "height": 1200,
            "mtime": "2026-11-02T09:00:00+08:00",
        }
        return document

    def test_matching_hash_passes(self):
        report = verify(self._document_with_source(self.digest))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(check_status(report)["source_hash"], "pass")
        self.assertEqual(report["source"]["sha256_now"], self.digest)

    def test_changed_hash_fails(self):
        report = verify(self._document_with_source("0" * 64))
        self.assertIn("SOURCE_HASH_CHANGED", hard_codes(report))
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["source"]["sha256_now"], self.digest)

    def test_missing_source_file_is_review(self):
        document = self._document_with_source(self.digest)
        document["source"]["path"] = str(self.source_path) + ".gone"
        report = verify(document)
        self.assertEqual(report["status"], "needs_review")
        self.assertIn("SOURCE_NOT_FOUND", review_codes(report))

    def test_missing_hash_record_is_review(self):
        report = verify(self._document_with_source(""))
        self.assertEqual(report["status"], "needs_review")
        self.assertIn("SOURCE_HASH_MISSING", review_codes(report))


# ------------------------------------------------------------------ xlsx 导出


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

        self.source_path = self.root / "source.png"
        self.source_bytes = b"\x89PNG\r\n\x1a\n fake source image bytes for export"
        self.source_path.write_bytes(self.source_bytes)
        self.source_digest = sha256_bytes(self.source_bytes)
        self.source_mtime = datetime.fromtimestamp(self.source_path.stat().st_mtime).astimezone()
        self.extraction = load_fixture("valid_extraction.json")
        self.extraction["source"] = {
            "path": str(self.source_path),
            "sha256": self.source_digest,
            "bytes": len(self.source_bytes),
            "width": 1600,
            "height": 1200,
            "mtime": self.source_mtime.isoformat(timespec="seconds"),
        }
        self.verification = verify(self.extraction)
        self.run_meta = {
            "plugin_version": "0.1.0",
            "engine": "rapidocr_onnxruntime 1.4.4",
            "tolerance": "0.01",
        }

    def _export(self, extraction=None, verification=None, filename="out.xlsx", run_meta=None, **kwargs):
        extraction = extraction if extraction is not None else self.extraction
        verification = verification if verification is not None else self.verification
        meta = dict(self.run_meta if run_meta is None else run_meta)
        output_path = self.root / filename
        result = xlsx_export.export_xlsx(
            extraction, verification, str(output_path), meta, **kwargs
        )
        return result, output_path

    # ---- 基本结构与格式 ----

    def test_export_document_and_four_sheets(self):
        result, output_path = self._export()
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(output_path))
        self.assertEqual(
            [sheet["name"] for sheet in result["sheets"]],
            ["数据", "校验报告", "问题清单", "运行清单"],
        )
        self.assertTrue(output_path.is_file())
        self.assertEqual(result["sha256"], sha256_bytes(output_path.read_bytes()))
        self.assertEqual(result["verification"]["status"], "pass")

        workbook = load_workbook(output_path)
        self.assertEqual(workbook.sheetnames, ["数据", "校验报告", "问题清单", "运行清单"])
        for worksheet in workbook.worksheets:
            self.assertEqual(worksheet.freeze_panes, "A2")

    def test_data_sheet_columns_trace_and_total_row(self):
        result, output_path = self._export()
        workbook = load_workbook(output_path)
        worksheet = workbook["数据"]
        header = [cell.value for cell in worksheet[1]]
        for expected in ("行类型", "行号", "原图", "bbox", "OCR原文", "置信度"):
            self.assertIn(expected, header)
        self.assertIn("发票号码", header)
        self.assertIn("金额", header)

        # 明细行数 == len(rows)，另加合计行
        row_type_column = header.index("行类型") + 1
        row_types = [
            worksheet.cell(row=row_number, column=row_type_column).value
            for row_number in range(2, worksheet.max_row + 1)
        ]
        self.assertEqual(row_types, ["明细", "明细", "明细", "合计"])
        self.assertEqual(result["sheets"][0]["rows"], len(self.extraction["rows"]) + 1)

        # 合计行的“行号”列写 label
        行号列 = header.index("行号") + 1
        self.assertEqual(worksheet.cell(row=5, column=行号列).value, "合计")
        self.assertEqual(worksheet.cell(row=2, column=行号列).value, 1)

    def test_number_columns_are_text_money_columns_are_numeric(self):
        _, output_path = self._export()
        workbook = load_workbook(output_path)
        worksheet = workbook["数据"]
        header = [cell.value for cell in worksheet[1]]
        invoice_column = header.index("发票号码") + 1
        amount_column = header.index("金额") + 1

        invoice_cell = worksheet.cell(row=2, column=invoice_column)
        self.assertEqual(invoice_cell.value, "00123456")
        self.assertEqual(invoice_cell.number_format, "@")
        self.assertIsInstance(invoice_cell.value, str)

        amount_cell = worksheet.cell(row=2, column=amount_column)
        self.assertNotIsInstance(amount_cell.value, str)
        self.assertEqual(amount_cell.number_format, "#,##0.00")
        self.assertEqual(Decimal(str(amount_cell.value)), Decimal("1200.00"))

        # 合计单元格也是数值
        total_amount_cell = worksheet.cell(row=5, column=amount_column)
        self.assertNotIsInstance(total_amount_cell.value, str)
        self.assertEqual(Decimal(str(total_amount_cell.value)), Decimal("2500.50"))

    def test_report_and_run_sheets(self):
        result, output_path = self._export()
        workbook = load_workbook(output_path)
        report = workbook["校验报告"]
        self.assertEqual(report.cell(row=1, column=1).value, "检查ID")
        self.assertEqual(report.max_row - 1, len(self.verification["checks"]))
        self.assertEqual(result["sheets"][1]["rows"], len(self.verification["checks"]))

        run_sheet = workbook["运行清单"]
        run_keys = [run_sheet.cell(row=row, column=1).value for row in range(2, run_sheet.max_row + 1)]
        self.assertEqual(
            run_keys,
            ["plugin_version", "engine", "input_path", "input_sha256", "output_sha256", "tolerance"],
        )
        self.assertEqual(result["run"]["plugin_version"], "0.1.0")
        self.assertEqual(result["run"]["tolerance"], "0.01")
        self.assertEqual(result["run"]["input_sha256"], self.source_digest)
        self.assertEqual(result["run"]["output_sha256"], result["sha256"])

    def test_workbook_properties_use_source_mtime(self):
        _, output_path = self._export()
        workbook = load_workbook(output_path)
        properties = workbook.properties
        expected = self.source_mtime.astimezone(timezone.utc).replace(tzinfo=None)
        expected_pair = (expected.year, expected.month, expected.day, expected.hour, expected.minute)
        for value in (properties.created, properties.modified):
            self.assertIsNotNone(value)
            self.assertEqual(
                (value.year, value.month, value.day, value.hour, value.minute),
                expected_pair,
                "created/modified 必须取 source.mtime，不能取 now()",
            )

    # ---- 确定性 ----

    def test_two_exports_are_byte_identical(self):
        result_one, path_one = self._export(filename="out1.xlsx")
        result_two, path_two = self._export(filename="out2.xlsx")
        self.assertEqual(result_one["sha256"], result_two["sha256"])
        self.assertEqual(path_one.read_bytes(), path_two.read_bytes())

    # ---- 拒绝条件 ----

    def test_refuses_output_equal_source_path(self):
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                self.extraction, self.verification, str(self.source_path), self.run_meta
            )
        self.assertEqual(raised.exception.code, "OUTPUT_REFUSED")
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)

    def test_refuses_output_equal_extraction_json(self):
        extraction_json = self.root / "extract.json"
        extraction_json.write_text(json.dumps(self.extraction, ensure_ascii=False), encoding="utf-8")
        run_meta = dict(self.run_meta, extraction_path=str(extraction_json))
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                self.extraction, self.verification, str(extraction_json), run_meta
            )
        self.assertEqual(raised.exception.code, "OUTPUT_REFUSED")

    def test_refuses_failed_verification_by_default(self):
        tampered = load_fixture("tampered_total_extraction.json")
        tampered["source"] = copy.deepcopy(self.extraction["source"])
        tampered_verification = verify(tampered)
        self.assertEqual(tampered_verification["status"], "fail")
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                tampered, tampered_verification, str(self.root / "fail.xlsx"), self.run_meta
            )
        self.assertEqual(raised.exception.code, "OUTPUT_REFUSED")
        self.assertFalse((self.root / "fail.xlsx").exists())

    def test_allow_unverified_permits_failed_export(self):
        tampered = load_fixture("tampered_total_extraction.json")
        tampered["source"] = copy.deepcopy(self.extraction["source"])
        tampered_verification = verify(tampered)
        result = xlsx_export.export_xlsx(
            tampered,
            tampered_verification,
            str(self.root / "fail_allowed.xlsx"),
            self.run_meta,
            allow_unverified=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["verification"]["status"], "fail")
        self.assertEqual(result["verification"]["n_hard_failures"], 1)
        self.assertGreater(result["sheets"][2]["rows"], 0)

    def test_issue_sheet_contains_total_mismatch_evidence(self):
        tampered = load_fixture("tampered_total_extraction.json")
        tampered["source"] = copy.deepcopy(self.extraction["source"])
        tampered_verification = verify(tampered)
        result = xlsx_export.export_xlsx(
            tampered,
            tampered_verification,
            str(self.root / "fail_evidence.xlsx"),
            self.run_meta,
            allow_unverified=True,
        )
        workbook = load_workbook(self.root / "fail_evidence.xlsx")
        worksheet = workbook["问题清单"]
        header = [cell.value for cell in worksheet[1]]
        rows = [dict(zip(header, [cell.value for cell in row])) for row in worksheet.iter_rows(min_row=2)]
        total_rows = [row for row in rows if row["代码"] == "TOTAL_MISMATCH"]
        self.assertEqual(len(total_rows), 1)
        evidence = total_rows[0]
        self.assertEqual(evidence["类型"], "硬失败")
        self.assertEqual(evidence["行号"], "1,2,3")
        self.assertEqual(evidence["字段"], "amount")
        self.assertEqual(evidence["期望"], "2500.49")
        self.assertEqual(evidence["实际"], "2500.50")
        self.assertEqual(evidence["差额"], "0.01")
        self.assertEqual(result["sheets"][2]["rows"], len(rows))

    def test_refuses_missing_output_directory(self):
        output_path = self.root / "no_such_dir" / "out.xlsx"
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                self.extraction, self.verification, str(output_path), self.run_meta
            )
        self.assertEqual(raised.exception.code, "IO_ERROR")

    def test_refuses_output_that_is_directory(self):
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                self.extraction, self.verification, str(self.root), self.run_meta
            )
        self.assertEqual(raised.exception.code, "IO_ERROR")

    def test_rejects_non_decimal_tolerance(self):
        run_meta = dict(self.run_meta, tolerance="abc")
        with self.assertRaises(FinanceError) as raised:
            xlsx_export.export_xlsx(
                self.extraction, self.verification, str(self.root / "bad_tol.xlsx"), run_meta
            )
        self.assertEqual(raised.exception.code, "BAD_INPUT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
