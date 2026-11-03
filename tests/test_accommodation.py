# -*- coding: utf-8 -*-
"""住宿费核对规则单元测试（纯 unittest，不依赖 OCR / 图片）。

覆盖用户 2026-11-02 确认的 6 条规则，以及"印章遮挡 / 手写签字"这些识别不确定性
如何降级成待人工确认而不是硬报错。
"""

import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import accommodation  # noqa: E402
from accommodation import check, cn_capital  # noqa: E402


def base_dataset():
    """一间双人房、一间单人房（当天人数 3 人为奇数，允许单住）。"""
    return {
        "schema_version": "finance-accommodation/1",
        "year": 2026,
        "department": "某段B车间",
        "policy": {
            "room_capacity": 2,
            "rate": "128",
            "directors": ["林七"],
        },
        "detail_sheet": {
            "subtotal": {"nights": 7, "amount": "896"},
            "lowercase_amount": "896.00",
            "capital_amount": "捌佰玖拾陆元整",
            "rows": [
                {
                    "seq": 1,
                    "room": "301",
                    "names": ["张三", "李四"],
                    "periods": [
                        {"start": "10月10日", "end": "10月13日", "nights": 3, "price": "128", "amount": "384"}
                    ],
                    "signature": ["张三", "李四"],
                },
                {
                    "seq": 2,
                    "room": "302",
                    "names": ["王五"],
                    "periods": [
                        {"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}
                    ],
                    "signature": ["王五"],
                },
            ],
        },
        "hotel_statement": {
            "printed_date": "2026年10月14日",
            "payment_amounts": ["896", "896"],
            "rows": [
                {"room": "301", "start": "10月10日", "end": "10月13日", "nights": 3, "price": "128", "amount": "384"},
                {"room": "302", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            ],
        },
    }


class CapitalTests(unittest.TestCase):
    def test_cn_capital(self):
        cases = {
            "20608": "贰万零陆佰零捌元整",
            "1664": "壹仟陆佰陆拾肆元整",
            "896": "捌佰玖拾陆元整",
            "1005": "壹仟零伍元整",
            "10005": "壹万零伍元整",
            "20000": "贰万元整",
            "0.5": "伍角整",
        }
        for amount, expected in cases.items():
            self.assertEqual(cn_capital(Decimal(amount)), expected, amount)


class PassTests(unittest.TestCase):
    def test_base_pass(self):
        report = check(base_dataset())
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["summary"]["total_nights"], 7)
        self.assertEqual(report["summary"]["total_amount"], "896")

    def test_odd_headcount_single_room_allowed(self):
        report = check(base_dataset())
        singles = report["single_rooms"]
        self.assertEqual(len(singles), 4)  # 302 单人住 4 晚，当天 3 人（奇数）
        self.assertEqual(report["status"], "pass")

    def test_director_single_room_allowed_even_if_even(self):
        data = base_dataset()
        # 当天 4 人（偶数），但单住的两间都是车间主任 → 允许
        data["detail_sheet"]["rows"] = [
            {
                "seq": 1,
                "room": "301",
                "names": ["张三", "李四"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["张三", "李四"],
            },
            {
                "seq": 2,
                "room": "302",
                "names": ["林七"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["林七"],
            },
            {
                "seq": 3,
                "room": "303",
                "names": ["何八"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["何八"],
            },
        ]
        data["policy"]["directors"] = ["林七", "何八"]
        data["detail_sheet"]["subtotal"] = {"nights": 12, "amount": "1536"}
        data["detail_sheet"]["lowercase_amount"] = "1536.00"
        data["detail_sheet"]["capital_amount"] = "壹仟伍佰叁拾陆元整"
        data["hotel_statement"]["rows"] = [
            {"room": "301", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "302", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "303", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
        ]
        data["hotel_statement"]["payment_amounts"] = ["1536", "1536"]
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["errors"], [])


class HardFailureTests(unittest.TestCase):
    def assert_rule(self, data, rule, status="fail"):
        report = check(data)
        codes = [item["rule"] for item in report["errors"]]
        self.assertIn(rule, codes, report["errors"])
        if status == "fail":
            self.assertEqual(report["status"], "fail")

    def test_nights_mismatch(self):
        data = base_dataset()
        data["detail_sheet"]["rows"][0]["periods"][0]["nights"] = 4
        self.assert_rule(data, "NIGHTS_MISMATCH")

    def test_amount_mismatch(self):
        data = base_dataset()
        data["detail_sheet"]["rows"][0]["periods"][0]["amount"] = "385"
        self.assert_rule(data, "AMOUNT_MISMATCH")

    def test_subtotal_nights(self):
        data = base_dataset()
        data["detail_sheet"]["subtotal"]["nights"] = 8
        self.assert_rule(data, "SUBTOTAL_NIGHTS")

    def test_subtotal_amount(self):
        data = base_dataset()
        data["detail_sheet"]["subtotal"]["amount"] = "900"
        self.assert_rule(data, "SUBTOTAL_AMOUNT")

    def test_capital_amount(self):
        data = base_dataset()
        data["detail_sheet"]["capital_amount"] = "玖佰玖拾陆元整"
        self.assert_rule(data, "TOTAL_CAPITAL")

    def test_duplicate_person_same_night(self):
        data = base_dataset()
        data["detail_sheet"]["rows"].append(
            {
                "seq": 3,
                "room": "303",
                "names": ["张三"],
                "periods": [
                    {"start": "10月11日", "end": "10月12日", "nights": 1, "price": "128", "amount": "128"}
                ],
                "signature": ["张三"],
            }
        )
        # 同步小计与流水，避免被其他规则先拦住
        data["detail_sheet"]["subtotal"] = {"nights": 8, "amount": "1024"}
        data["detail_sheet"]["lowercase_amount"] = "1024.00"
        data["detail_sheet"]["capital_amount"] = "壹仟零贰拾肆元整"
        data["hotel_statement"]["rows"].append(
            {"room": "303", "start": "10月11日", "end": "10月12日", "nights": 1, "price": "128", "amount": "128"}
        )
        data["hotel_statement"]["payment_amounts"] = ["1024", "1024"]
        self.assert_rule(data, "DUPLICATE_PERSON")

    def test_duplicate_room_same_night(self):
        data = base_dataset()
        data["detail_sheet"]["rows"].append(
            {
                "seq": 3,
                "room": "301",
                "names": ["赵六"],
                "periods": [
                    {"start": "10月11日", "end": "10月12日", "nights": 1, "price": "128", "amount": "128"}
                ],
                "signature": ["赵六"],
            }
        )
        data["detail_sheet"]["subtotal"] = {"nights": 8, "amount": "1024"}
        data["detail_sheet"]["lowercase_amount"] = "1024.00"
        data["detail_sheet"]["capital_amount"] = "壹仟零贰拾肆元整"
        data["hotel_statement"]["rows"].append(
            {"room": "301", "start": "10月11日", "end": "10月12日", "nights": 1, "price": "128", "amount": "128"}
        )
        data["hotel_statement"]["payment_amounts"] = ["1024", "1024"]
        self.assert_rule(data, "DUPLICATE_ROOM")

    def test_single_room_only_one_non_director_allowed(self):
        """当天 4 人（偶数），但除主任外只有 1 间单住 → 属于凑不够，允许。"""
        data = base_dataset()
        data["detail_sheet"]["rows"] = [
            {
                "seq": 1,
                "room": "301",
                "names": ["张三", "李四"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["张三", "李四"],
            },
            {
                "seq": 2,
                "room": "302",
                "names": ["王五"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["王五"],
            },
            {
                "seq": 3,
                "room": "303",
                "names": ["林七"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["林七"],
            },
        ]
        data["detail_sheet"]["subtotal"] = {"nights": 12, "amount": "1536"}
        data["detail_sheet"]["lowercase_amount"] = "1536.00"
        data["detail_sheet"]["capital_amount"] = "壹仟伍佰叁拾陆元整"
        data["hotel_statement"]["rows"] = [
            {"room": "301", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "302", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "303", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
        ]
        data["hotel_statement"]["payment_amounts"] = ["1536", "1536"]
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])

    def test_two_non_director_single_rooms_fail(self):
        """同一天除主任外出现 2 间单住 → 可以互相拼房，不允许。"""
        data = base_dataset()
        data["detail_sheet"]["rows"] = [
            {
                "seq": 1,
                "room": "301",
                "names": ["张三", "李四"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["张三", "李四"],
            },
            {
                "seq": 2,
                "room": "302",
                "names": ["王五"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["王五"],
            },
            {
                "seq": 3,
                "room": "303",
                "names": ["赵六"],
                "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
                "signature": ["赵六"],
            },
        ]
        data["detail_sheet"]["subtotal"] = {"nights": 12, "amount": "1536"}
        data["detail_sheet"]["lowercase_amount"] = "1536.00"
        data["detail_sheet"]["capital_amount"] = "壹仟伍佰叁拾陆元整"
        data["hotel_statement"]["rows"] = [
            {"room": "301", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "302", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            {"room": "303", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
        ]
        data["hotel_statement"]["payment_amounts"] = ["1536", "1536"]
        self.assert_rule(data, "MULTIPLE_SINGLE_ROOMS")

    def test_signature_mismatch(self):
        data = base_dataset()
        data["detail_sheet"]["rows"][0]["signature"] = ["张三", "王五"]
        self.assert_rule(data, "SIGNATURE_MISMATCH")

    def test_statement_missing_segment(self):
        data = base_dataset()
        data["hotel_statement"]["rows"] = data["hotel_statement"]["rows"][:1]
        data["hotel_statement"]["payment_amounts"] = ["384", "384"]
        self.assert_rule(data, "MISSING_IN_STATEMENT")

    def test_statement_value_diff(self):
        data = base_dataset()
        data["hotel_statement"]["rows"][0]["amount"] = "374"
        self.assert_rule(data, "STATEMENT_VALUE_DIFF")

    def test_payment_amount(self):
        data = base_dataset()
        data["hotel_statement"]["payment_amounts"] = ["890", "890"]
        self.assert_rule(data, "PAYMENT_AMOUNT")


class SoftFailureTests(unittest.TestCase):
    def test_signature_uncertain_is_review_not_error(self):
        data = base_dataset()
        data["detail_sheet"]["rows"][0]["signature_uncertain"] = True
        report = check(data)
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["errors"], [])
        self.assertIn("SIGNATURE_UNVERIFIED", [item["rule"] for item in report["warnings"]])

    def test_stamp_obscured_statement_room_matched_by_values(self):
        data = base_dataset()
        data["hotel_statement"]["rows"][0]["room"] = ""  # 被印章遮挡
        report = check(data)
        # 日期段+天数+金额唯一匹配到明细房间，按该房间核对，记提示不改变结论
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertIn("STATEMENT_ROOM_MATCHED", [item["rule"] for item in report["notes"]])

    def test_room_gap_note(self):
        data = base_dataset()
        data["detail_sheet"]["rows"][1]["periods"] = [
            {"start": "10月10日", "end": "10月12日", "nights": 2, "price": "128", "amount": "256"},
            {"start": "10月13日", "end": "10月14日", "nights": 1, "price": "128", "amount": "128"},
        ]
        data["detail_sheet"]["subtotal"] = {"nights": 6, "amount": "768"}
        data["detail_sheet"]["lowercase_amount"] = "768.00"
        data["detail_sheet"]["capital_amount"] = "柒佰陆拾捌元整"
        data["hotel_statement"]["rows"] = [
            {"room": "301", "start": "10月10日", "end": "10月13日", "nights": 3, "price": "128", "amount": "384"},
            {"room": "302", "start": "10月10日", "end": "10月12日", "nights": 2, "price": "128", "amount": "256"},
            {"room": "302", "start": "10月13日", "end": "10月14日", "nights": 1, "price": "128", "amount": "128"},
        ]
        data["hotel_statement"]["payment_amounts"] = ["768", "768"]
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertIn("ROOM_GAP", [item["rule"] for item in report["notes"]])


def detail_only_dataset():
    """机关待遇/无流水：两张明细表（无房间号），同设计室真实数据。"""
    return {
        "schema_version": "finance-accommodation/1",
        "year": 2026,
        "department": "某段设计室",
        "detail_only": True,
        "no_statement": True,
        "no_room": True,
        "signature_verified": True,
        "policy": {"room_capacity": 2, "rate": "128"},
        "detail_sheets": [
            {
                "subtotal": {"nights": 12, "amount": "1856"},
                "lowercase_amount": "1856.00",
                "capital_amount": "壹仟捌佰伍拾陆元整",
                "rows": [
                    {"seq": 1, "names": ["张三"], "periods": [
                        {"start": "10月10日", "end": "10月14日", "nights": 4, "price": "142", "amount": "568"}],
                     "signature": ["张三"]},
                    {"seq": 2, "names": ["李四"], "periods": [
                        {"start": "10月10日", "end": "10月14日", "nights": 4, "price": "142", "amount": "568"}],
                     "signature": ["李四"]},
                    {"seq": 3, "names": ["张三"], "periods": [
                        {"start": "10月14日", "end": "10月16日", "nights": 2, "price": "180", "amount": "360"}],
                     "signature": ["张三"]},
                    {"seq": 4, "names": ["李四"], "periods": [
                        {"start": "10月14日", "end": "10月16日", "nights": 2, "price": "180", "amount": "360"}],
                     "signature": ["李四"]},
                ],
            },
            {
                "subtotal": {"nights": 14, "amount": "2520"},
                "lowercase_amount": "2520.00",
                "capital_amount": "贰仟伍佰贰拾元整",
                "rows": [
                    {"seq": 2, "names": ["张三"], "periods": [
                        {"start": "10月16日", "end": "10月23日", "nights": 7, "price": "180", "amount": "1260"}],
                     "signature": ["张三"]},
                    {"seq": 2, "names": ["李四"], "periods": [
                        {"start": "10月16日", "end": "10月23日", "nights": 7, "price": "180", "amount": "1260"}],
                     "signature": ["李四"]},
                ],
            },
        ],
    }


class DetailOnlyTests(unittest.TestCase):
    def test_detail_only_pass(self):
        report = check(detail_only_dataset())
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["summary"]["total_nights"], 26)
        self.assertEqual(report["summary"]["total_amount"], "4376")
        rules = {item["rule"]: item["status"] for item in report["rules"]}
        self.assertEqual(rules["double_room"], "skip")
        self.assertEqual(rules["statement"], "skip")
        self.assertEqual(rules["signature"], "pass")
        self.assertIn("STATEMENT_NOT_REQUIRED", [item["rule"] for item in report["notes"]])

    def test_detail_only_signature_unverified(self):
        data = detail_only_dataset()
        data["signature_verified"] = False
        for sheet in data["detail_sheets"]:
            for row in sheet["rows"]:
                row["signature"] = []
        report = check(data)
        self.assertEqual(report["status"], "needs_review")
        self.assertIn("SIGNATURE_UNVERIFIED", [item["rule"] for item in report["warnings"]])

    def test_detail_only_nights_mismatch(self):
        data = detail_only_dataset()
        data["detail_sheets"][0]["rows"][0]["periods"][0]["nights"] = 5
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("NIGHTS_MISMATCH", [item["rule"] for item in report["errors"]])

    def test_detail_only_subtotal_mismatch(self):
        data = detail_only_dataset()
        data["detail_sheets"][1]["subtotal"] = {"nights": 13, "amount": "2520"}
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("SUBTOTAL_NIGHTS", [item["rule"] for item in report["errors"]])

    def test_detail_only_capital_mismatch(self):
        data = detail_only_dataset()
        data["detail_sheets"][0]["capital_amount"] = "壹仟捌佰伍拾陆元"
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("CAPITAL_MISMATCH", [item["rule"] for item in report["errors"]])

    def test_detail_only_duplicate_person_night(self):
        data = detail_only_dataset()
        # 张三在两张表里同一天重复出现（10月16日）
        data["detail_sheets"][1]["rows"][0]["periods"][0]["start"] = "10月15日"
        data["detail_sheets"][1]["rows"][0]["periods"][0]["nights"] = 8
        data["detail_sheets"][1]["rows"][0]["periods"][0]["amount"] = "1440"
        data["detail_sheets"][1]["subtotal"] = {"nights": 15, "amount": "2700"}
        data["detail_sheets"][1]["lowercase_amount"] = "2700.00"
        data["detail_sheets"][1]["capital_amount"] = cn_capital(Decimal("2700"))
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("DUPLICATE_PERSON_NIGHT", [item["rule"] for item in report["errors"]])

    def test_detail_only_signature_mismatch(self):
        data = detail_only_dataset()
        data["detail_sheets"][0]["rows"][0]["signature"] = ["李四"]
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("SIGNATURE_MISMATCH", [item["rule"] for item in report["errors"]])

    def test_detail_only_rate_over_office_cap(self):
        """机关住宿费上限 200 元/天：201 元/天必须报错。"""
        data = detail_only_dataset()
        sheet = data["detail_sheets"][0]
        sheet["rows"][0]["periods"][0]["price"] = "201"
        sheet["rows"][0]["periods"][0]["amount"] = "804"
        sheet["subtotal"] = {"nights": 12, "amount": "2092"}
        sheet["lowercase_amount"] = "2092.00"
        sheet["capital_amount"] = cn_capital(Decimal("2092"))
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("RATE_OVER_LIMIT", [item["rule"] for item in report["errors"]])

    def test_detail_only_rate_at_office_cap_ok(self):
        """机关上限 200 元/天：正好 200 元/天允许。"""
        data = detail_only_dataset()
        sheet = data["detail_sheets"][0]
        sheet["rows"][0]["periods"][0]["price"] = "200"
        sheet["rows"][0]["periods"][0]["amount"] = "800"
        sheet["subtotal"] = {"nights": 12, "amount": "2088"}
        sheet["lowercase_amount"] = "2088.00"
        sheet["capital_amount"] = cn_capital(Decimal("2088"))
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])


class RateCapTests(unittest.TestCase):
    def test_workshop_rate_over_cap(self):
        """车间住宿费上限 130 元/天：131 元/天必须报错。"""
        data = base_dataset()
        row = data["detail_sheet"]["rows"][0]
        row["periods"][0]["price"] = "131"
        row["periods"][0]["amount"] = "393"
        data["detail_sheet"]["subtotal"] = {"nights": 7, "amount": "905"}
        data["detail_sheet"]["lowercase_amount"] = "905.00"
        data["detail_sheet"]["capital_amount"] = cn_capital(Decimal("905"))
        data["hotel_statement"]["rows"][0]["price"] = "131"
        data["hotel_statement"]["rows"][0]["amount"] = "393"
        data["hotel_statement"]["payment_amounts"] = ["905", "905"]
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("RATE_OVER_LIMIT", [item["rule"] for item in report["errors"]])

    def test_workshop_rate_at_cap_ok(self):
        """车间上限 130 元/天：正好 130 元/天允许。"""
        data = base_dataset()
        row = data["detail_sheet"]["rows"][0]
        row["periods"][0]["price"] = "130"
        row["periods"][0]["amount"] = "390"
        data["detail_sheet"]["subtotal"] = {"nights": 7, "amount": "902"}
        data["detail_sheet"]["lowercase_amount"] = "902.00"
        data["detail_sheet"]["capital_amount"] = cn_capital(Decimal("902"))
        data["hotel_statement"]["rows"][0]["price"] = "130"
        data["hotel_statement"]["rows"][0]["amount"] = "390"
        data["hotel_statement"]["payment_amounts"] = ["902", "902"]
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])


def multi_page_dataset():
    """两页明细 + 一页流水：合并核对 + 逐页小计/大小写。"""
    page1_rows = [
        {"seq": 1, "room": "301", "names": ["张三", "李四"],
         "periods": [{"start": "10月10日", "end": "10月13日", "nights": 3, "price": "128", "amount": "384"}],
         "signature": ["张三", "李四"]},
    ]
    page2_rows = [
        {"seq": 2, "room": "302", "names": ["王五"],
         "periods": [{"start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"}],
         "signature": ["王五"]},
    ]
    return {
        "schema_version": "finance-accommodation/1",
        "year": 2026,
        "department": "某段A车间",
        "policy": {"room_capacity": 2, "rate": "128", "directors": ["林七"]},
        "detail_sheet": {
            "rows": page1_rows + page2_rows,
            "subtotal": {"nights": 7, "amount": "896"},
            "lowercase_amount": "896.00",
            "capital_amount": "捌佰玖拾陆元整",
        },
        "detail_sheets": [
            {"rows": page1_rows, "subtotal": {"nights": 3, "amount": "384"},
             "lowercase_amount": "384.00", "capital_amount": "叁佰捌拾肆元整"},
            {"rows": page2_rows, "subtotal": {"nights": 4, "amount": "512"},
             "lowercase_amount": "512.00", "capital_amount": "伍佰壹拾贰元整"},
        ],
        "hotel_statement": {
            "rows": [
                {"room": "301", "start": "10月10日", "end": "10月13日", "nights": 3, "price": "128", "amount": "384"},
                {"room": "302", "start": "10月10日", "end": "10月14日", "nights": 4, "price": "128", "amount": "512"},
            ],
            "payment_amounts": ["896"],
        },
    }


class MultiSheetTests(unittest.TestCase):
    def test_multi_page_pass(self):
        report = check(multi_page_dataset())
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertEqual(report["summary"]["total_nights"], 7)
        self.assertEqual(len(report["summary"]["per_sheet"]), 2)

    def test_multi_page_subtotal_mismatch(self):
        data = multi_page_dataset()
        data["detail_sheets"][1]["subtotal"] = {"nights": 5, "amount": "512"}
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("SUBTOTAL_NIGHTS", [item["rule"] for item in report["errors"]])

    def test_multi_page_capital_mismatch(self):
        data = multi_page_dataset()
        data["detail_sheets"][0]["capital_amount"] = "叁佰捌拾肆元"
        report = check(data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("TOTAL_CAPITAL", [item["rule"] for item in report["errors"]])

    def test_signature_verified_standard_mode(self):
        data = multi_page_dataset()
        for row in data["detail_sheet"]["rows"]:
            row["signature"] = []
        self.assertEqual(check(data)["status"], "needs_review")
        data["signature_verified"] = True
        report = check(data)
        self.assertEqual(report["status"], "pass", report["errors"])
        self.assertIn("SIGNATURE_VERIFIED", [item["rule"] for item in report["notes"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
