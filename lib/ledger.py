# -*- coding: utf-8 -*-
"""住宿费核对台账：把每次核对结果累积成一份可追溯的台账。

设计：
- 源数据是 JSONL（一行一条记录，追加/去重方便，体量小）；
- 同时生成一份 xlsx 方便人看/打印（每次追加后重新生成，永远与 JSONL 一致）；
- 去重键 = (批次, 住宿地点, 流水哈希, 期间起, 期间止, 金额)：同一批同一地点重复核对只更新状态，不重复记账；
- 归档：`archive(keep_last=N)` 保留最近 N 条，其余移到 `台账归档-<时间>.jsonl`，台账文件不会无限膨胀。

台账记录字段：
  recorded_at / batch / location / department / period_start / period_end / n_people /
  n_segments / total_nights / total_amount / status / n_errors / n_warnings / source / dataset_path / plugin_version
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _read(ledger_path: Path) -> list[dict]:
    if not ledger_path.is_file():
        return []
    entries = []
    for line in ledger_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _write(ledger_path: Path, entries: list[dict]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + ("\n" if entries else "")
    handle, tmp_name = tempfile.mkstemp(prefix=ledger_path.name + ".", dir=str(ledger_path.parent))
    os.close(handle)
    try:
        with open(tmp_name, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.replace(tmp_name, ledger_path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _key(entry: dict) -> tuple:
    return (
        str(entry.get("batch") or ""),
        str(entry.get("location") or ""),
        str(entry.get("source") or ""),
        str(entry.get("period_start") or ""),
        str(entry.get("period_end") or ""),
        str(entry.get("total_amount") or ""),
    )


def build_entry(dataset: dict, report: dict, *, batch: str, location: str,
                dataset_path: str | None, plugin_version: str, source: str | None) -> dict:
    """从一次核对的数据集 + 报告生成台账记录。"""
    sheet = dataset.get("detail_sheet") or {}
    statement = dataset.get("hotel_statement") or {}
    if dataset.get("detail_only"):
        rows = [row for item in dataset.get("detail_sheets") or [] for row in (item.get("rows") or [])]
    else:
        rows = sheet.get("rows") or []
    people = set()
    for row in rows:
        for name in row.get("names") or []:
            text = str(name).strip()
            if text:
                people.add(text)
    summary = report.get("summary") or {}
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    return {
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch": batch,
        "location": location,
        "department": summary.get("department") or dataset.get("department"),
        "period_start": summary.get("first_night"),
        "period_end": summary.get("last_night"),
        "n_people": len(people),
        "n_segments": summary.get("n_detail_segments"),
        "total_nights": summary.get("total_nights"),
        "total_amount": summary.get("total_amount"),
        "status": report.get("status"),
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "source": source,
        "dataset_path": dataset_path,
        "plugin_version": plugin_version,
        "note": (
            ("机关待遇/不核流水；" if dataset.get("detail_only") else "")
            + ("、".join(item.get("rule", "") for item in errors[:5])
               or "、".join(item.get("rule", "") for item in warnings[:5]))
        ),
    }


def append(ledger_path, entries: list[dict]) -> dict:
    """追加/更新台账，随后重新生成同名 xlsx。返回 {n_added, n_updated, n_total, xlsx}。"""
    path = Path(ledger_path).expanduser()
    current = _read(path)
    index = {_key(item): position for position, item in enumerate(current)}
    n_added = n_updated = 0
    for entry in entries:
        key = _key(entry)
        if key in index:
            position = index[key]
            keep_recorded = current[position].get("recorded_at")
            current[position].update(entry)
            current[position]["recorded_at"] = keep_recorded or entry.get("recorded_at")
            n_updated += 1
        else:
            current.append(entry)
            index[key] = len(current) - 1
            n_added += 1
    current.sort(key=lambda item: str(item.get("recorded_at") or ""))
    _write(path, current)
    xlsx_path = path.with_suffix(".xlsx")
    export_xlsx(path, xlsx_path)
    return {
        "ledger": str(path),
        "xlsx": str(xlsx_path),
        "n_added": n_added,
        "n_updated": n_updated,
        "n_total": len(current),
    }


def list_entries(ledger_path) -> dict:
    path = Path(ledger_path).expanduser()
    entries = _read(path)
    total_nights = sum(int(item.get("total_nights") or 0) for item in entries)
    total_amount = sum(
        (
            Decimal(str(item.get("total_amount") or "0"))
            for item in entries
            if str(item.get("total_amount") or "").strip()
        ),
        Decimal("0"),
    )
    return {
        "ledger": str(path),
        "n_total": len(entries),
        "total_nights": total_nights,
        "total_amount": str(total_amount),
        "entries": entries,
    }


def export_xlsx(ledger_path, output_path) -> str:
    """把 JSONL 台账导出成 xlsx（人看的版本），每次重新生成，保持与 JSONL 一致。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    path = Path(ledger_path).expanduser()
    output = Path(output_path).expanduser()
    entries = _read(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "住宿费核对台账"
    headers = [
        "记录时间",
        "批次",
        "部门/车间",
        "住宿地点",
        "期间开始",
        "期间结束",
        "人数",
        "日期段数",
        "住宿天数",
        "住宿金额",
        "核对结论",
        "错误数",
        "待确认数",
        "数据文件",
        "备注",
    ]
    sheet.append(headers)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A2"

    for item in entries:
        amount = item.get("total_amount")
        try:
            amount_value = float(Decimal(str(amount))) if amount not in (None, "") else None
        except (InvalidOperation, ValueError):
            amount_value = None
        sheet.append(
            [
                item.get("recorded_at"),
                item.get("batch"),
                item.get("department"),
                item.get("location"),
                item.get("period_start"),
                item.get("period_end"),
                item.get("n_people"),
                item.get("n_segments"),
                item.get("total_nights"),
                amount_value,
                item.get("status"),
                item.get("n_errors"),
                item.get("n_warnings"),
                item.get("dataset_path"),
                item.get("note"),
            ]
        )

    if entries:
        last_row = sheet.max_row + 1
        sheet.cell(row=last_row, column=1, value="合计")
        sheet.cell(row=last_row, column=9, value=f"=SUM(I2:I{last_row - 1})")
        sheet.cell(row=last_row, column=10, value=f"=SUM(J2:J{last_row - 1})")
        for column in (9, 10):
            sheet.cell(row=last_row, column=column).font = Font(bold=True)

    widths = [21, 14, 22, 18, 12, 12, 8, 10, 12, 14, 12, 8, 10, 40, 24]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=False)
    sheet.column_dimensions["J"].number_format = "#,##0.00"

    workbook.save(output)
    return str(output)


def record_unknown(register_path, names: list[str], batch: str | None, department: str | None) -> dict:
    """把人员字典里查不到的姓名记到"待确认名单"（JSONL），下次核对/补字典时用。

    同一姓名只保留一条，累计出现批次、次数，方便以后统一确认（外包人员变动频繁）。
    """
    path = Path(register_path).expanduser()
    entries = _read(path)
    index = {str(item.get("name")): item for item in entries}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    added = []
    for name in dict.fromkeys(str(item) for item in names if str(item).strip()):
        item = index.get(name)
        if item is None:
            item = {
                "name": name,
                "first_seen": now,
                "last_seen": now,
                "department": department,
                "batches": [batch] if batch else [],
                "count": 1,
                "status": "pending",
            }
            entries.append(item)
            index[name] = item
            added.append(name)
        else:
            item["last_seen"] = now
            item["count"] = int(item.get("count") or 0) + 1
            batches = item.setdefault("batches", [])
            if batch and batch not in batches:
                batches.append(batch)
            if department and not item.get("department"):
                item["department"] = department
    entries.sort(key=lambda item: str(item.get("name") or ""))
    _write(path, entries)
    return {
        "register": str(path),
        "n_total": len(entries),
        "added": added,
        "pending": [item["name"] for item in entries if item.get("status") == "pending"],
    }


def archive(ledger_path, keep_last: int | None = None, before: str | None = None,
            archive_dir=None) -> dict:
    """归档旧记录：保留最近 keep_last 条（或 before 日期之后的），其余移入归档文件。"""
    path = Path(ledger_path).expanduser()
    entries = _read(path)
    entries.sort(key=lambda item: str(item.get("recorded_at") or ""))
    if before:
        kept = [item for item in entries if str(item.get("recorded_at") or "") >= before]
    elif keep_last is not None and keep_last >= 0:
        kept = entries[-keep_last:] if keep_last else []
    else:
        raise ValueError("archive 需要 keep_last 或 before 参数")
    moved = [item for item in entries if item not in kept]
    if not moved:
        _write(path, entries)
        return {"ledger": str(path), "n_archived": 0, "n_kept": len(entries), "archive": None}
    target_dir = Path(archive_dir).expanduser() if archive_dir else path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive_path = target_dir / f"{path.stem}-归档-{stamp}.jsonl"
    _write(archive_path, moved)
    _write(path, kept)
    export_xlsx(path, path.with_suffix(".xlsx"))
    return {
        "ledger": str(path),
        "xlsx": str(path.with_suffix(".xlsx")),
        "archive": str(archive_path),
        "n_archived": len(moved),
        "n_kept": len(kept),
    }
