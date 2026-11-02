# -*- coding: utf-8 -*-
"""人员字典（姓名检查）单元测试（全部使用占位姓名/部门，不放真实数据）。"""

import sys
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import roster  # noqa: E402


def sample_roster():
    departments = {
        "A车间": {
            "names": ["王五", "周九", "吴十", "郑一", "王二", "陈三", "刘四", "杨五", "孙八"],
            "directors": [{"name": "王五", "post": "主任"}],
        },
        "B车间": {
            "names": ["赵六", "钱七", "孙八"],
            "directors": [{"name": "赵六", "post": "主任"}],
        },
        "设计室": {
            "names": ["张三", "黄六"],
            "directors": [{"name": "张三", "post": "主任"}],
        },
    }
    name_index = {}
    for department, info in departments.items():
        for name in info["names"]:
            name_index.setdefault(name, []).append(department)
    return {
        "schema_version": "finance-roster/1",
        "version": "test",
        "departments": departments,
        "shared_names": ["周九", "孙八", "黄六"],
        "outsourced_names": ["王三", "刘六"],
        "name_index": name_index,
    }


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.roster = sample_roster()

    def test_department_resolution(self):
        self.assertEqual(roster.resolve_department(self.roster, "某段A车间"), "A车间")
        self.assertEqual(roster.resolve_department(self.roster, "A车间"), "A车间")
        self.assertIsNone(roster.resolve_department(self.roster, "未知车间"))

    def test_exact_names_pass(self):
        result = roster.correct_names(["周九", "吴十"], self.roster, "某段A车间")
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["cross_department"], [])

    def test_ocr_name_gets_suggestion(self):
        result = roster.correct_names(["周八", "吴四"], self.roster, "某段A车间")
        pairs = {item["from"]: item["to"] for item in result["suggestions"]}
        self.assertEqual(pairs.get("周八"), "周九")
        self.assertEqual(pairs.get("吴四"), "吴十")
        self.assertIn("周八", result["unknown"])

    def test_same_surname_required(self):
        result = roster.correct_names(["李一"], self.roster, "某段A车间")
        for item in result["suggestions"]:
            self.assertEqual(item["from"][0], item["to"][0])

    def test_shared_name_ok_any_department(self):
        result = roster.correct_names(["孙八", "黄六"], self.roster, "某段A车间")
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["cross_department"], [])

    def test_cross_department_name_kept(self):
        result = roster.correct_names(["钱七"], self.roster, "某段A车间")
        # 钱七 是 B车间的人，按表格保留并标记跨部门（可能是借调）
        self.assertIn("钱七", result["cross_department"])
        self.assertEqual(result["unknown"], [])

    def test_outsourced_name_recognized(self):
        result = roster.correct_names(["王三"], self.roster, "某段A车间")
        self.assertIn("王三", result["outsourced"])
        self.assertEqual(result["unknown"], [])

    def test_correct_dataset_does_not_modify_names(self):
        dataset = {
            "department": "某段A车间",
            "detail_sheet": {
                "rows": [
                    {"names": ["周八", "吴四"], "names_uncertain": True},
                    {"names": ["王五"], "names_uncertain": False},
                ]
            },
        }
        name_match = roster.correct_dataset(dataset, self.roster)
        self.assertEqual(dataset["detail_sheet"]["rows"][0]["names"], ["周八", "吴四"])
        self.assertEqual(name_match["department"], "A车间")
        self.assertTrue(name_match["suggestions"])
        # names_uncertain=False 的行不产生建议（人工核对过的数据不再被字典改动）
        self.assertEqual(dataset["detail_sheet"]["rows"][1]["names"], ["王五"])

    def test_roster_load_roundtrip(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(self.roster, handle, ensure_ascii=False)
            path = handle.name
        loaded = roster.load(path)
        self.assertEqual(loaded["version"], "test")
        self.assertEqual(len(loaded["departments"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
