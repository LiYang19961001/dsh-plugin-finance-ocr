#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验收测试：图片 -> extraction -> 校验 -> xlsx，全部通过 lib/cli.py 的进程接口跑。

用法（必须用 DSH 运行时里的 python）：
    C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe tests\\test_pipeline.py

刻意不 mock 任何环节：真跑 OCR、真跑校验、真写 xlsx。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CLI = PLUGIN_DIR / "lib" / "cli.py"
GEN_SAMPLE = PLUGIN_DIR / "tests" / "gen_sample.py"
SAMPLE = PLUGIN_DIR / "samples" / "sample_reimbursement.png"
WORK = Path(tempfile.mkdtemp(prefix="finance-ocr-test-"))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def run_cli(command: str, payload: dict) -> tuple[dict, int]:
    """按 index.js 的方式调用 cli.py：写 input 文件、spawn、读 output 文件。"""
    in_path = WORK / f"{command}.in.json"
    out_path = WORK / f"{command}.out.json"
    in_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CLI), command, "--input", str(in_path), "--output", str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        cwd=str(PLUGIN_DIR),
    )
    if not out_path.is_file():
        raise AssertionError(
            f"{command} 没有写出输出文件；exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(out_path.read_text(encoding="utf-8")), proc.returncode


def amount_of(cell: dict) -> Decimal:
    return Decimal(str(cell["value"]))


def main() -> int:
    print(f"工作目录: {WORK}")

    # ---------- 0. 样图 ----------
    print("\n[0] 合成样图")
    if not SAMPLE.is_file():
        gen = subprocess.run(
            [sys.executable, str(GEN_SAMPLE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=str(PLUGIN_DIR),
        )
        check("gen_sample", gen.returncode == 0, (gen.stderr or gen.stdout or "")[-400:])
    else:
        check("gen_sample", True, "复用已有样图")
    check("sample_exists", SAMPLE.is_file(), str(SAMPLE))

    # ---------- 1. 环境自检 ----------
    print("\n[1] selftest")
    selftest, _ = run_cli("selftest", {})
    check("selftest.ok", bool(selftest.get("ok")), json.dumps(
        [c for c in selftest.get("checks", []) if not c.get("ok")], ensure_ascii=False)[:600]
    )
    check("selftest.engine", bool(selftest.get("engine_version")), str(selftest.get("engine_version")))

    # ---------- 2. OCR 识别 ----------
    print("\n[2] ocr_extract")
    extract, _ = run_cli("ocr_extract", {"image": str(SAMPLE), "template": "auto"})
    check("extract.ok", bool(extract.get("ok")), json.dumps(extract.get("error"), ensure_ascii=False))
    if not extract.get("ok"):
        return report()
    ext_path = Path(extract["extraction_path"])
    check("extract.cache_written", ext_path.is_file(), str(ext_path))

    extraction = json.loads(ext_path.read_text(encoding="utf-8"))
    check("extract.schema", extraction.get("schema_version") == "finance-extract/1", extraction.get("schema_version"))
    check("extract.source_sha", len(extraction.get("source", {}).get("sha256", "")) == 64)
    rows = extraction.get("rows") or []
    totals = extraction.get("total_rows") or []
    fields = [c.get("field") for c in extraction.get("columns") or []]
    check("extract.rows", len(rows) >= 3, f"rows={len(rows)}")
    check("extract.total_row", len(totals) >= 1, f"total_rows={len(totals)}")
    check("extract.has_amount_column", "amount" in fields, f"columns={fields}")
    print(f"        识别列: {fields}")
    print(f"        识别行数: {len(rows)}，合计行: {[r.get('label') for r in totals]}")

    # 每行每格都能追回原图位置
    traceable = True
    missing_trace = []
    for row in rows:
        for field, cell in (row.get("cells") or {}).items():
            bbox = cell.get("bbox")
            if not (isinstance(bbox, list) and len(bbox) == 4) or cell.get("line_id") is None or not cell.get("raw"):
                traceable = False
                missing_trace.append(f"row{row.get('row_index')}.{field}")
    check("extract.traceable", traceable, ", ".join(missing_trace[:6]))

    # ---------- 3. 行数守恒 ----------
    covered = set()
    for row in rows:
        for cell in (row.get("cells") or {}).values():
            covered.add(cell.get("line_id"))
    for row in totals:
        for cell in (row.get("cells") or {}).values():
            covered.add(cell.get("line_id"))
    for item in extraction.get("skipped_lines") or []:
        covered.add(item.get("line_id"))
    n_lines = (extraction.get("ocr") or {}).get("n_lines")
    covered.discard(None)
    check(
        "extract.line_accounting",
        n_lines is not None and len(covered) == int(n_lines),
        f"covered={len(covered)} n_lines={n_lines}",
    )

    # ---------- 4. 校验必须通过 ----------
    print("\n[3] verify (真实数据)")
    verify, _ = run_cli("verify", {"extraction": str(ext_path)})
    report_doc = verify.get("report") or {}
    check("verify.ok", bool(verify.get("ok")), json.dumps(verify.get("error"), ensure_ascii=False))
    # 人工复核项（如单个数字置信度低）是设计内的分级，不算失败；硬失败必须为零。
    check(
        "verify.no_hard_failures",
        not (report_doc.get("hard_failures") or []),
        json.dumps(report_doc.get("hard_failures"), ensure_ascii=False)[:500],
    )
    check(
        "verify.status_acceptable",
        report_doc.get("status") in ("pass", "needs_review"),
        f"status={report_doc.get('status')} review={len(report_doc.get('review_items') or [])}",
    )
    print(f"        校验状态: {report_doc.get('status')}，人工复核项 {len(report_doc.get('review_items') or [])} 条")

    # 明细合计 == 票面合计（独立用 Decimal 再算一遍，不信任实现）
    if "amount" in fields and totals:
        detail_sum = sum((amount_of(r["cells"]["amount"]) for r in rows if "amount" in r.get("cells", {})), Decimal("0"))
        total_cell = (totals[0].get("cells") or {}).get("amount")
        if total_cell:
            check(
                "verify.detail_equals_total_independent",
                detail_sum == amount_of(total_cell),
                f"{detail_sum} vs {amount_of(total_cell)}",
            )

    # ---------- 5. 人为改坏 1 分钱，校验必须报错 ----------
    print("\n[4] verify (人为改坏合计 1 分钱)")
    corrupted = json.loads(json.dumps(extraction))
    target = None
    if corrupted.get("total_rows"):
        cells = corrupted["total_rows"][0].get("cells") or {}
        for field in ("amount", "total", "tax"):
            if field in cells:
                target = (cells[field], field)
                break
    if target is None:
        check("verify.corrupted", False, "extraction 里没有可改坏的合计金额单元格")
    else:
        cell, field = target
        original = Decimal(str(cell["value"]))
        cell["value"] = str((original + Decimal("0.01")).quantize(Decimal("0.01")))
        bad_path = WORK / "corrupted.extract.json"
        bad_path.write_text(json.dumps(corrupted, ensure_ascii=False, indent=2), encoding="utf-8")
        bad, bad_code = run_cli("verify", {"extraction": str(bad_path)})
        bad_report = bad.get("report") or {}
        codes = [item.get("code") for item in bad_report.get("hard_failures") or []]
        check(
            "verify.corrupted_caught",
            bad_report.get("status") == "fail" and "TOTAL_MISMATCH" in codes,
            f"status={bad_report.get('status')} codes={codes} exit={bad_code}",
        )

        # 校验 fail 时导出必须被拒绝（除非 allow_unverified）
        print("\n[5] export_xlsx (校验 fail 时拒绝)")
        refused, refused_doc = run_cli(
            "export_xlsx", {"extraction": str(bad_path), "output": str(WORK / "should_not_exist.xlsx")}
        )
        check(
            "export.refuses_failed_verification",
            not refused.get("ok") and not (WORK / "should_not_exist.xlsx").exists(),
            json.dumps(refused.get("error"), ensure_ascii=False),
        )

    # ---------- 6. 正常导出 ----------
    print("\n[6] export_xlsx (正常)")
    out_xlsx = WORK / "result.xlsx"
    export, export_code = run_cli("export_xlsx", {"extraction": str(ext_path), "output": str(out_xlsx)})
    check("export.ok", bool(export.get("ok")), json.dumps(export.get("error"), ensure_ascii=False))
    check("export.file_exists", out_xlsx.is_file(), f"{out_xlsx} exit={export_code}")

    if out_xlsx.is_file():
        from openpyxl import load_workbook

        workbook = load_workbook(out_xlsx)
        names = workbook.sheetnames
        check("xlsx.sheets", all(s in names for s in ("数据", "校验报告", "问题清单", "运行清单")), str(names))
        data = workbook["数据"]
        header = [cell.value for cell in data[1]]
        print(f"        数据 sheet 表头: {header}")
        row_types = []
        if "行类型" in header:
            index = header.index("行类型") + 1
            row_types = [data.cell(row=r, column=index).value for r in range(2, data.max_row + 1)]
            row_types = [value for value in row_types if value]
        detail_count = sum(1 for value in row_types if value == "明细")
        total_count = sum(1 for value in row_types if value == "合计")
        check("xlsx.has_row_type", "行类型" in header, str(header))
        check("xlsx.detail_rows", detail_count == len(rows), f"明细 {detail_count} vs rows {len(rows)}")
        check("xlsx.total_rows", total_count == len(totals), f"合计 {total_count} vs {len(totals)}")
        # 金额必须是数字格式而不是文本
        amount_col = None
        for name in ("金额", "amount"):
            if name in header:
                amount_col = header.index(name) + 1
                break
        if amount_col:
            cell = data.cell(row=2, column=amount_col)
            check("xlsx.amount_numeric", isinstance(cell.value, (int, float)), f"{cell.value!r} {cell.data_type}")
        workbook.close()

    # ---------- 7. 确定性：同一张图跑两次，extraction 完全一致 ----------
    print("\n[7] 确定性 (同一张图跑两次)")
    extract2, _ = run_cli("ocr_extract", {"image": str(SAMPLE), "template": "auto"})
    if extract2.get("ok"):
        second = Path(extract2["extraction_path"]).read_text(encoding="utf-8")
        first = ext_path.read_text(encoding="utf-8")
        check("determinism.extraction_identical", first == second, f"{len(first)} vs {len(second)} bytes")
    else:
        check("determinism.extraction_identical", False, "第二次识别失败")

    # ---------- 8. 拒绝覆盖源文件 ----------
    print("\n[8] export_xlsx 拒绝覆盖源图")
    overwrite, _ = run_cli("export_xlsx", {"extraction": str(ext_path), "output": str(SAMPLE)})
    check(
        "export.refuses_source_overwrite",
        not overwrite.get("ok"),
        json.dumps(overwrite.get("error"), ensure_ascii=False),
    )
    check("sample_untouched", SAMPLE.is_file(), "源图仍在")

    # ---------- 9. 批量 ----------
    print("\n[9] batch")
    batch_in = WORK / "batch-in"
    batch_in.mkdir(parents=True, exist_ok=True)
    for name in ("报销单A.png", "报销单B.png"):
        shutil.copyfile(SAMPLE, batch_in / name)
    from PIL import Image as _Image

    _Image.new("RGB", (600, 400), "white").save(batch_in / "空白图.png")
    batch_out = WORK / "batch-out"
    batch, batch_code = run_cli(
        "batch", {"directory": str(batch_in), "output_dir": str(batch_out), "tolerance": "0.01"}
    )
    check("batch.ok", bool(batch.get("ok")), json.dumps(batch.get("error"), ensure_ascii=False))
    counts = batch.get("counts") or {}
    check(
        "batch.counts",
        counts.get("pass") == 2 and counts.get("no_table") == 1 and counts.get("fail") == 0,
        json.dumps(counts, ensure_ascii=False),
    )
    check("batch.exported", batch.get("n_exported") == 2, f"n_exported={batch.get('n_exported')}")
    xlsx_files = sorted(p.name for p in batch_out.glob("*.xlsx"))
    check("batch.xlsx_files", len(xlsx_files) == 2, ", ".join(xlsx_files))
    check(
        "batch.summary_written",
        (batch_out / "batch-summary.json").is_file(),
        str(batch_out / "batch-summary.json"),
    )
    blank_result = [
        item for item in (batch.get("results") or []) if item.get("file") == "空白图.png"
    ]
    check(
        "batch.blank_is_no_table",
        bool(blank_result) and blank_result[0].get("status") == "no_table",
        json.dumps(blank_result, ensure_ascii=False)[:300],
    )

    return report()


def report() -> int:
    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    if not FAILED:
        print("全部通过，OK")
        shutil.rmtree(WORK, ignore_errors=True)
        return 0
    print(f"工作目录保留在 {WORK} 供排查")
    return 1


if __name__ == "__main__":
    sys.exit(main())
