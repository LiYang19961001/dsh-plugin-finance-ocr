# -*- coding: utf-8 -*-
"""人员字典（住宿费核对用）：按部门 + 共享人员纠正 OCR 误识的姓名。

数据来源：`各车间人员分布` Excel 解析出的 `人员字典-2026.9.json`（本机策略文件里配路径）。
设计：
- 姓名以**住宿明细表（表格）**为准；字典只纠正高置信的 OCR 误识（同姓 + 相似度 >= 阈值）；
- 轨道车司机、设计室借调人员是**共享人员**，任何部门都可用；
- 字典里查不到的姓名保留原样，列进 `unknown`，由人工确认（不替用户改名）。
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

SCHEMA = "finance-roster/1"
MATCH_THRESHOLD = 0.60


class RosterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load(path) -> dict:
    path = Path(path).expanduser()
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise RosterError("EXTRACTION_NOT_FOUND", f"人员字典不存在：{path}")
    except json.JSONDecodeError as error:
        raise RosterError("BAD_INPUT", f"人员字典不是合法 JSON：{path}（{error}）")
    if not isinstance(data, dict) or not isinstance(data.get("departments"), dict):
        raise RosterError("SCHEMA_MISMATCH", "人员字典缺少 departments")
    data.setdefault("schema_version", SCHEMA)
    data["path"] = str(path)
    return data


def _normalize_department(department: str | None) -> str:
    text = str(department or "").strip()
    for prefix in ("某段", "某单位"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip()


def resolve_department(roster: dict, department: str | None) -> str | None:
    """把"某段A车间"这类写法映射到字典里的部门键。"""
    normalized = _normalize_department(department)
    if not normalized:
        return None
    departments = roster.get("departments") or {}
    if normalized in departments:
        return normalized
    for key in departments:
        if key and (key in normalized or normalized in key):
            return key
    return None


def scope(roster: dict, department: str | None) -> dict:
    """某部门可用的姓名/主任集合（部门人员 + 共享人员）。"""
    key = resolve_department(roster, department)
    departments = roster.get("departments") or {}
    names: list[str] = []
    directors: list[str] = []
    if key:
        info = departments.get(key) or {}
        names.extend(info.get("names") or [])
        directors.extend(item.get("name") for item in info.get("directors") or [] if item.get("name"))
    names.extend(roster.get("shared_names") or [])
    return {
        "department": key,
        "names": list(dict.fromkeys(names)),
        "directors": list(dict.fromkeys(directors)),
    }


def _best_match(name: str, candidates: list[str]) -> tuple[str | None, float]:
    # 2 字姓名只有同姓候选时（如"吴四"→"吴十"），Edit 距离必然只有 0.5，
    # 单独给一个"同姓唯一候选"加分；有多个同姓候选则不猜（避免乱点鸳鸯）。
    if len(name) <= 2:
        same = [c for c in candidates if c and len(c) <= 2 and c[0] == name[0]]
        if len(same) == 1:
            return same[0], 0.65
    same_surname = [candidate for candidate in candidates if candidate and candidate[0] == name[0]]
    pool = same_surname or candidates
    best, best_ratio = None, 0.0
    for candidate in pool:
        ratio = difflib.SequenceMatcher(None, name, candidate).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best, round(best_ratio, 3)


def correct_names(names: list[str], roster: dict, department: str | None,
                  threshold: float = MATCH_THRESHOLD) -> dict:
    """检查一组姓名，**不修改输入**，只给出建议。

    - 部门名单 + 共享人员里的名字：通过；
    - 其他部门名单里有的：记为 cross_department（可能是借调）；
    - 都不在：按"同姓优先"找形近候选（部门名单优先、再看全段），相似度够则记为 suggestions，
      仅供参考——姓名以住宿表格为准，不自动改名，由人工确认；
    - 仍找不到：记 unknown，交人工确认。
    """
    scoped = scope(roster, department)
    scoped_names = scoped["names"] if scoped.get("department") else []
    outsourced_names = list(roster.get("outsourced_names") or [])
    global_names = list(roster.get("name_index") or {}) + list(roster.get("shared_names") or [])
    global_names = list(dict.fromkeys(global_names))
    candidates = scoped_names or global_names
    suggestions: list[dict] = []
    unknown: list[str] = []
    cross_department: list[str] = []
    outsourced: list[str] = []
    for name in names:
        if not name:
            continue
        if name in scoped_names:
            continue
        if name in outsourced_names:
            outsourced.append(name)
            continue
        if name in global_names:
            cross_department.append(name)
            continue
        candidate, ratio = _best_match(name, candidates)
        if ratio < threshold:
            candidate, ratio = _best_match(name, global_names)
        if candidate and ratio >= threshold:
            # 不在名单里（按表格保留、需确认身份），同时给出可能的 OCR 误识候选
            unknown.append(name)
            suggestions.append(
                {"from": name, "to": candidate, "ratio": ratio, "scope": scoped["department"] or "全部"}
            )
        else:
            unknown.append(name)
    return {
        "department": scoped["department"],
        "suggestions": suggestions,
        "unknown": list(dict.fromkeys(unknown)),
        "cross_department": list(dict.fromkeys(cross_department)),
        "outsourced": list(dict.fromkeys(outsourced)),
        "scope_size": len(candidates),
    }


def correct_dataset(dataset: dict, roster: dict) -> dict:
    """检查数据集里的姓名并给出 name_match 摘要；**不修改数据集里的姓名**。

    只有明确标记 names_uncertain（OCR 不确定）的行才会给出"疑似误识"建议，
    人工核对后的数据不会再被字典改动。
    """
    department = dataset.get("department") or (dataset.get("detail_sheet") or {}).get("department")
    suggestions: list[dict] = []
    unknown: list[str] = []
    unmatched_uncertain: list[str] = []
    cross_department: list[str] = []
    outsourced: list[str] = []
    scoped_department = None
    if dataset.get("detail_only"):
        row_groups = [
            (sheet.get("rows") or []) for sheet in (dataset.get("detail_sheets") or [])
        ]
    else:
        row_groups = [(dataset.get("detail_sheet") or {}).get("rows") or []]
    for rows in row_groups:
        for row in rows:
            names = [str(name) for name in row.get("names") or []]
            if not names:
                continue
            result = correct_names(names, roster, department)
            scoped_department = scoped_department or result["department"]
            uncertain = bool(row.get("names_uncertain"))
            # 疑似误识建议一直给出（只提示不改名）：自动识别时 OCR 可能高置信但错字，
            # 人工核对过的数据也不会被改动，最多多一行提示。
            suggestions.extend(result["suggestions"])
            for name in result["unknown"]:
                # OCR 不确定的行，姓名本身可能就是误识，不记成"待确认人员"
                target = unmatched_uncertain if uncertain else unknown
                if name not in target:
                    target.append(name)
            for name in result["cross_department"]:
                if name not in cross_department:
                    cross_department.append(name)
            for name in result.get("outsourced") or []:
                if name not in outsourced:
                    outsourced.append(name)
    deduped = []
    seen = set()
    for item in suggestions:
        key = (item["from"], item["to"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return {
        "schema_version": SCHEMA,
        "roster": roster.get("path"),
        "roster_version": roster.get("version"),
        "department": scoped_department or _normalize_department(department) or None,
        "suggestions": deduped,
        "unknown": unknown,
        "unmatched_uncertain": unmatched_uncertain,
        "cross_department": cross_department,
        "outsourced": outsourced,
    }
