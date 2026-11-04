# -*- coding: utf-8 -*-
"""住宿费核对：施工住宿费明细表 vs 酒店流水（业务规则层，纯 stdlib）。

用户口径（2026-11-02 确认）：
1. 除车间主任可以单住外，其余人必须双人间；单住只允许"当天人数凑不够"（当天总人数为奇数）。
2. 同一天内不能出现重复的房间或重复的人（一个房间同一天只能有一组人；一个人同一天只能住一个房间）。
3. 住宿天数 = 结束日期 - 开始日期；住宿金额 = 住宿天数 × 单价。
4. 入住人员姓名必须与签字确认姓名一致（同一组人，顺序不限）。
5. 明细的小计（天数、金额）、合计大小写必须正确。
6. 酒店流水必须与明细逐房间、逐日期段匹配，支付金额一致。

设计原则与 OCR 层一致：只报差异，不"修复"；手写签字无法机读时明确标记为待人工核对，不假装通过。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "finance-accommodation/1"

CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
CN_UNITS = ["", "拾", "佰", "仟"]
CN_SECTIONS = ["", "万", "亿"]


class AccommodationError(Exception):
    """带错误码的业务异常，cli 会映射成契约里的 error.code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 基础工具


def parse_cn_date(text: str, year: int) -> date:
    """解析 '10月10日' / '2026年10月10日' / '10.10' 一类日期。"""
    text = str(text).strip()
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日?", text)
    if match:
        return date(year, int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", text)
    if match:
        return date(year, int(match.group(1)), int(match.group(2)))
    raise AccommodationError("BAD_INPUT", f"无法解析日期：{text!r}")


def parse_decimal(value, field: str = "金额") -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise AccommodationError("BAD_INPUT", f"{field} 不是数字：{value!r}")


def _capital_ignoring_zero(text: str) -> str:
    return str(text or "").replace("零", "")


def _capital_ocr_damaged(capital: str, expected: str) -> bool:
    """大写 OCR 缺字判断：缺的是金额字符（不是只缺末尾"整"）才算 OCR 缺字。"""
    capital = str(capital or "").strip()
    expected = str(expected or "")
    if not capital or not expected:
        return True
    if capital in expected:
        missing = expected.replace(capital, "", 1)
        return missing.replace("零", "") != "整"
    if expected in capital:
        return True
    iterator = iter(expected)
    return all(char in iterator for char in capital)


def cn_capital(amount: Decimal) -> str:
    """人民币大写（0 及正数，两位小数）。"""
    amount = amount.quantize(Decimal("0.01"))
    integer = int(amount)
    jiao = int((amount * 10) % 10)
    fen = int((amount * 100) % 10)
    if integer == 0 and jiao == 0 and fen == 0:
        return "零元整"

    def section(number: int) -> str:
        out = ""
        zero = False
        for index in range(3, -1, -1):
            digit = (number // (10 ** index)) % 10
            if digit == 0:
                if out:
                    zero = True
            else:
                if zero:
                    out += "零"
                    zero = False
                out += CN_DIGITS[digit] + CN_UNITS[index]
        return out

    groups = []
    remaining = integer
    group_index = 0
    while remaining > 0:
        groups.append((remaining % 10000, group_index))
        remaining //= 10000
        group_index += 1
    parts: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        chunk, _ = groups[index]
        if chunk == 0:
            continue
        if parts and chunk < 1000:
            parts.append("零")
        parts.append(section(chunk) + CN_SECTIONS[index])
    body = "".join(parts)
    out = (body + "元") if body else ""
    if jiao == 0 and fen == 0:
        return (out or "零元") + "整"
    if jiao:
        out += CN_DIGITS[jiao] + "角"
    elif body:
        out += "零"
    if fen:
        out += CN_DIGITS[fen] + "分"
    else:
        out += "整"
    return out


def load_dataset(path) -> dict:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise AccommodationError("EXTRACTION_NOT_FOUND", f"数据文件不存在：{path}")
    except json.JSONDecodeError as error:
        raise AccommodationError("BAD_INPUT", f"不是合法 JSON：{path}（{error}）")
    if not isinstance(data, dict) or "detail_sheet" not in data:
        raise AccommodationError("SCHEMA_MISMATCH", "住宿费数据缺少 detail_sheet")
    data.setdefault("schema_version", SCHEMA)
    return data


# ------------------------------------------------------------------ 规则实现


def _rule(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def check(data: dict) -> dict:
    """对一份住宿费数据跑全部规则，返回报告。

    返回的 status 三态：pass / needs_review / fail。
    errors 是硬失败（必须改），warnings 是待人工确认，notes 是提示（不影响结论）。
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    notes: list[dict] = []

    def err(rule: str, message: str, **extra):
        errors.append({"rule": rule, "message": message, **extra})

    def warn(rule: str, message: str, **extra):
        warnings.append({"rule": rule, "message": message, **extra})

    def note(rule: str, message: str, **extra):
        notes.append({"rule": rule, "message": message, **extra})

    if data.get("detail_only"):
        return check_detail_only(data)

    sheet = data.get("detail_sheet") or {}
    rows = sheet.get("rows") or []
    if not rows:
        raise AccommodationError("BAD_INPUT", "detail_sheet.rows 为空，无法核对")

    # ---------- 多页明细：逐页核对各页打印的小计/小写/大写（合并后的总金额稍后统一校） ----------
    detail_pages = data.get("detail_sheets") or []
    per_sheet_summary: list[dict] = []
    if detail_pages:
        for page_index, page_sheet in enumerate(detail_pages, 1):
            label = f"第{page_index}页"
            page_rows = page_sheet.get("rows") or []
            page_nights = 0
            page_amount = Decimal("0")
            for page_row in page_rows:
                for period in page_row.get("periods") or []:
                    try:
                        page_nights += int(period.get("nights") or 0)
                        page_amount += parse_decimal(period.get("amount"), "住宿金额")
                    except (AccommodationError, TypeError, ValueError):
                        continue
            page_subtotal = page_sheet.get("subtotal") or {}
            page_subtotal_nights = page_subtotal.get("nights")
            if page_subtotal_nights is not None and int(page_subtotal_nights) != page_nights:
                err(
                    "SUBTOTAL_NIGHTS",
                    f"{label}小计天数 {page_subtotal_nights} ≠ 该页各段合计 {page_nights}",
                    expected=str(page_subtotal_nights),
                    actual=str(page_nights),
                )
            page_subtotal_amount = page_subtotal.get("amount")
            if page_subtotal_amount not in (None, ""):
                if parse_decimal(page_subtotal_amount) != page_amount:
                    err(
                        "SUBTOTAL_AMOUNT",
                        f"{label}小计金额 {page_subtotal_amount} ≠ 该页合计 {page_amount}",
                        expected=str(page_subtotal_amount),
                        actual=str(page_amount),
                    )
            page_lowercase = page_sheet.get("lowercase_amount")
            if page_lowercase not in (None, ""):
                if parse_decimal(page_lowercase) != page_amount:
                    err(
                        "TOTAL_LOWERCASE",
                        f"{label}合计小写 {page_lowercase} ≠ 该页合计 {page_amount}",
                        expected=str(page_lowercase),
                        actual=str(page_amount),
                    )
            page_capital = str(page_sheet.get("capital_amount") or "").strip()
            if page_capital:
                expected_capital = cn_capital(page_amount)
                if page_capital != expected_capital:
                    if _capital_ignoring_zero(page_capital) == _capital_ignoring_zero(expected_capital):
                        note(
                            "TOTAL_CAPITAL_STYLE",
                            f"{label}合计大写「{page_capital}」金额一致但写法不规范（规范为「{expected_capital}」）",
                        )
                    elif _capital_ocr_damaged(page_capital, expected_capital):
                        warn(
                            "TOTAL_CAPITAL_OCR",
                            f"{label}合计大写「{page_capital}」像 OCR 缺字（应为「{expected_capital}」），请人工核对大写",
                        )
                    else:
                        err(
                            "TOTAL_CAPITAL",
                            f"{label}合计大写「{page_capital}」与金额 {page_amount} 不符，应为「{expected_capital}」",
                            expected=expected_capital,
                            actual=page_capital,
                        )
            per_sheet_summary.append(
                {
                    "sheet": page_index,
                    "n_rows": len(page_rows),
                    "nights": page_nights,
                    "amount": str(page_amount),
                    "lowercase_amount": page_lowercase,
                    "capital_amount": page_capital or None,
                }
            )

    year = int(data.get("year") or (data.get("hotel_statement") or {}).get("year") or date.today().year)
    policy = data.get("policy") or {}
    capacity = int(policy.get("room_capacity", 2))
    directors = [str(name).strip() for name in (policy.get("directors") or []) if str(name).strip()]
    rate_expected = policy.get("rate")
    rate_expected = parse_decimal(rate_expected, "预期单价") if rate_expected not in (None, "") else None
    rate_cap_raw = policy.get("rate_cap_workshop") or policy.get("rate_cap") or "130"
    rate_cap = parse_decimal(rate_cap_raw, "车间住宿费上限")
    signature_verified = bool(data.get("signature_verified"))

    # ---------- 规则 3/5：每个日期段的天数、金额；明细合计 ----------
    segments: list[dict] = []
    total_nights = 0
    total_amount = Decimal("0")
    nights_check_failed = False
    amount_check_failed = False
    rate_limit_failed = False
    for row in rows:
        seq = row.get("seq")
        room = str(row.get("room") or "").strip()
        names = [str(name).strip() for name in (row.get("names") or []) if str(name).strip()]
        signature = [str(name).strip() for name in (row.get("signature") or []) if str(name).strip()]
        if not room:
            err("ROW_EMPTY_ROOM", f"序号{seq} 缺少房间号", seq=seq)
        if not names:
            warn(
                "ROW_NAMES_MISSING",
                f"序号{seq} 房间{room} 没有识别到入住人员（可能被印章遮挡或识别失败），需人工核对",
                seq=seq,
                room=room,
            )
        if len(names) > capacity:
            err(
                "ROOM_OVER_CAPACITY",
                f"序号{seq} 房间{room} 有 {len(names)} 人，超过双人间容量 {capacity}",
                seq=seq,
                room=room,
            )
        periods = row.get("periods") or []
        if not periods:
            err("ROW_EMPTY_PERIOD", f"序号{seq} 房间{room} 没有住宿日期段", seq=seq)
        for period in periods:
            try:
                start = parse_cn_date(period["start"], year)
                end = parse_cn_date(period["end"], year)
            except AccommodationError as error:
                err("BAD_DATE", f"序号{seq} 房间{room}：{error.message}", seq=seq)
                continue
            if period.get("nights") in (None, "") or period.get("price") in (None, "") or period.get("amount") in (None, ""):
                warn(
                    "INCOMPLETE_PERIOD",
                    f"序号{seq} 房间{room} {period.get('start')}→{period.get('end')} 识别不完整"
                    f"（天数={period.get('nights')} 单价={period.get('price')} 金额={period.get('amount')}），需人工核对",
                    seq=seq,
                    room=room,
                )
                nights = (end - start).days
                total_nights += nights
                continue
            nights = (end - start).days
            declared = int(period.get("nights"))
            price = parse_decimal(period.get("price"), "单价")
            amount = parse_decimal(period.get("amount"), "住宿金额")
            if nights <= 0:
                err(
                    "BAD_DATE_RANGE",
                    f"序号{seq} 房间{room} {period['start']}→{period['end']} 天数 {nights} 不合法",
                    seq=seq,
                )
            if nights != declared:
                nights_check_failed = True
                err(
                    "NIGHTS_MISMATCH",
                    f"序号{seq} 房间{room} {period['start']}→{period['end']} 应为 {nights} 天，表里写 {declared} 天",
                    seq=seq,
                    room=room,
                )
            calc = nights * price
            if calc != amount:
                amount_check_failed = True
                err(
                    "AMOUNT_MISMATCH",
                    f"序号{seq} 房间{room} {period['start']}→{period['end']} {nights}天×{price}={calc}，表里写 {amount}",
                    seq=seq,
                    room=room,
                )
            if price > rate_cap:
                rate_limit_failed = True
                err(
                    "RATE_OVER_LIMIT",
                    f"序号{seq} 房间{room} 单价 {price} 元/天超过车间住宿费上限 {rate_cap} 元/天",
                    seq=seq,
                    room=room,
                )
            if rate_expected is not None and price != rate_expected:
                note(
                    "RATE_DIFF",
                    f"序号{seq} 房间{room} 单价 {price} 与常规单价 {rate_expected} 不同（未超上限，仅供参考）",
                    seq=seq,
                    room=room,
                )
            total_nights += nights
            total_amount += amount
            segments.append(
                {
                    "seq": seq,
                    "room": room,
                    "names": names,
                    "start": start,
                    "end": end,
                    "nights": nights,
                    "price": price,
                    "amount": amount,
                    "signature": signature,
                    "signature_uncertain": bool(row.get("signature_uncertain")),
                }
            )

    subtotal = sheet.get("subtotal") or {}
    subtotal_nights = int(subtotal.get("nights")) if subtotal.get("nights") not in (None, "") else None
    subtotal_amount = (
        parse_decimal(subtotal.get("amount"), "小计金额")
        if subtotal.get("amount") not in (None, "")
        else None
    )
    if subtotal_nights is None:
        warn("SUBTOTAL_MISSING", "明细没有小计天数，无法核对小计")
    elif subtotal_nights != total_nights:
        err(
            "SUBTOTAL_NIGHTS",
            f"小计天数 {subtotal_nights} ≠ 明细各段合计 {total_nights}",
            expected=str(subtotal_nights),
            actual=str(total_nights),
        )
    if subtotal_amount is None:
        warn("SUBTOTAL_MISSING", "明细没有小计金额，无法核对小计")
    elif subtotal_amount != total_amount:
        err(
            "SUBTOTAL_AMOUNT",
            f"小计金额 {subtotal_amount} ≠ 明细各段合计 {total_amount}",
            expected=str(subtotal_amount),
            actual=str(total_amount),
        )
    lowercase = sheet.get("lowercase_amount")
    if lowercase not in (None, ""):
        if parse_decimal(lowercase, "合计小写") != total_amount:
            err(
                "TOTAL_LOWERCASE",
                f"合计小写 {lowercase} ≠ 明细合计 {total_amount}",
                expected=str(lowercase),
                actual=str(total_amount),
            )
    capital = str(sheet.get("capital_amount") or "").strip()
    if capital:
        expected_capital = cn_capital(total_amount)
        if capital != expected_capital:
            if _capital_ignoring_zero(capital) == _capital_ignoring_zero(expected_capital):
                note(
                    "TOTAL_CAPITAL_STYLE",
                    f"合计大写「{capital}」金额一致但写法不规范（规范为「{expected_capital}」），不影响金额核对",
                    expected=expected_capital,
                    actual=capital,
                )
            elif _capital_ocr_damaged(capital, expected_capital):
                warn(
                    "TOTAL_CAPITAL_OCR",
                    f"合计大写「{capital}」像是 OCR 识别不完整（应为「{expected_capital}」），请人工核对大写",
                    expected=expected_capital,
                    actual=capital,
                )
            else:
                err(
                    "TOTAL_CAPITAL",
                    f"合计大写「{capital}」与金额 {total_amount} 不符，应为「{expected_capital}」",
                    expected=expected_capital,
                    actual=capital,
                )

    # ---------- 规则 4：入住人员 = 签字确认（同组人，顺序不限） ----------
    signature_failed = False
    signature_verified_count = 0
    for segment in segments:
        if segment["signature_uncertain"] or not segment["signature"]:
            if signature_verified:
                signature_verified_count += 1
                continue
            warn(
                "SIGNATURE_UNVERIFIED",
                f"序号{segment['seq']} 房间{segment['room']} 签字为手写体，系统无法机读，需人工核对",
                seq=segment["seq"],
                room=segment["room"],
            )
            signature_failed = True
            continue
        if sorted(segment["signature"]) != sorted(segment["names"]):
            signature_failed = True
            err(
                "SIGNATURE_MISMATCH",
                f"序号{segment['seq']} 房间{segment['room']} 入住人员 {'、'.join(segment['names'])} "
                f"≠ 签字 {'、'.join(segment['signature'])}",
                seq=segment["seq"],
                room=segment["room"],
            )
    if signature_verified_count:
        note(
            "SIGNATURE_VERIFIED",
            f"签字为手写体，已人工核对 {signature_verified_count} 个日期段与入住人员一致",
            count=signature_verified_count,
        )

    # ---------- 规则 2：同一天不能重复房间 / 重复人 ----------
    room_nights: dict[date, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    person_nights: dict[date, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for segment in segments:
        for offset in range(max(segment["nights"], 0)):
            night = date.fromordinal(segment["start"].toordinal() + offset)
            room_nights[night][segment["room"]].append(segment)
            for name in segment["names"]:
                person_nights[night][name].append(segment)

    duplicate_failed = False
    for night in sorted(room_nights):
        for room, segs in room_nights[night].items():
            if len(segs) > 1:
                duplicate_failed = True
                err(
                    "DUPLICATE_ROOM",
                    f"{night} 房间{room} 同时出现在 {len(segs)} 条住宿记录里（日期段重叠）",
                    date=str(night),
                    room=room,
                )
        for name, segs in person_nights[night].items():
            if len(segs) > 1:
                duplicate_failed = True
                err(
                    "DUPLICATE_PERSON",
                    f"{night} {name} 同时出现在 {[seg['room'] for seg in segs]}（同一天重复入住）",
                    date=str(night),
                    person=name,
                )

    # ---------- 规则 1：双人间；正主任可单住；其他人同一天最多 1 间单住 ----------
    # 用户 2026-11-03 澄清：判断标准不是"当天总人数奇偶"，而是"同一天除主任外是否出现 2 间及以上单人间"。
    # 只有一间非主任单人间时，说明房间数/人数凑不成对（凑不够），允许；出现两间就可以互相拼房，不允许。
    double_room_failed = False
    single_records: list[dict] = []
    for night in sorted(room_nights):
        headcount = sum(len(segs[0]["names"]) for segs in room_nights[night].values())
        single_rooms_today: list[tuple[str, str]] = []
        for room, segs in room_nights[night].items():
            occupants = segs[0]["names"]
            if len(occupants) == 1:
                lone = occupants[0]
                single_records.append({"date": str(night), "room": room, "person": lone})
                if lone in directors:
                    continue  # 正主任单住，例外
                single_rooms_today.append((room, lone))
            elif len(occupants) > capacity:
                double_room_failed = True
                err(
                    "ROOM_OVER_CAPACITY",
                    f"{night} 房间{room} 有 {len(occupants)} 人，超过双人间容量 {capacity}",
                    date=str(night),
                    room=room,
                )
        if len(single_rooms_today) >= 2:
            double_room_failed = True
            pairs = "、".join(f"{room}房 {person}" for room, person in single_rooms_today)
            err(
                "MULTIPLE_SINGLE_ROOMS",
                f"{night} 除主任外出现 {len(single_rooms_today)} 间单住（{pairs}），"
                f"当天共 {headcount} 人，至少应有两间拼住",
                date=str(night),
                rooms=[room for room, _ in single_rooms_today],
            )

    if single_records:
        note(
            "SINGLE_ROOM_LIST",
            "单住房晚 {} 条：{}".format(
                len(single_records),
                "；".join(f"{item['date']} {item['room']}房 {item['person']}" for item in single_records),
            ),
            count=len(single_records),
        )

    # ---------- 附加提示：同一房间两段住宿之间的空档（退房） ----------
    room_periods: dict[str, list[dict]] = defaultdict(list)
    for segment in segments:
        room_periods[segment["room"]].append(segment)
    gaps: list[dict] = []
    for room, items in sorted(room_periods.items()):
        items = sorted(items, key=lambda item: item["start"])
        for left, right in zip(items, items[1:]):
            gap_start = left["end"]
            gap_nights = (right["start"] - gap_start).days
            if gap_nights > 0:
                gaps.append(
                    {
                        "room": room,
                        "from": str(gap_start),
                        "to": str(right["start"]),
                        "nights": gap_nights,
                    }
                )
    if gaps:
        note(
            "ROOM_GAP",
            "以下房间在两段住宿之间有空档（通常为退房/换房，如非正常退房请核实）："
            + "；".join(
                f"房间{gap['room']} {gap['from']}→{gap['to']} 空 {gap['nights']} 晚" for gap in gaps
            ),
            gaps=gaps,
        )

    # ---------- 规则 6：酒店流水与明细匹配 ----------
    statement = data.get("hotel_statement") or {}
    stmt_rows = statement.get("rows") or []
    stmt_segments: list[dict] = []
    unknown_room_segments: list[dict] = []
    statement_failed = False
    statement_incomplete: list[str] = []
    for index, row in enumerate(stmt_rows):
        room = str(row.get("room") or "").strip()
        if row.get("start") in (None, "") or row.get("end") in (None, ""):
            statement_incomplete.append(f"第{index + 1}行（房间{room or '未识别'}）日期未识别")
            continue
        try:
            start = parse_cn_date(row["start"], year)
            end = parse_cn_date(row["end"], year)
        except AccommodationError as error:
            statement_incomplete.append(f"第{index + 1}行日期无法解析（{error.message}）")
            continue
        if row.get("price") in (None, "") or row.get("amount") in (None, ""):
            statement_incomplete.append(f"第{index + 1}行（房间{room or '未识别'}）单价/金额未识别")
            continue
        nights = (end - start).days
        declared = int(row.get("nights")) if row.get("nights") not in (None, "") else nights
        price = parse_decimal(row.get("price"), "流水单价")
        amount = parse_decimal(row.get("amount"), "流水金额")
        if not room:
            unknown_room_segments.append(
                {
                    "start": start,
                    "end": end,
                    "nights": nights,
                    "price": price,
                    "amount": amount,
                }
            )
            continue
        if nights != declared:
            statement_failed = True
            err(
                "STMT_NIGHTS",
                f"流水 房间{room} {row['start']}→{row['end']} 天数写 {declared}，应为 {nights}",
                room=room,
            )
        if nights * price != amount:
            statement_failed = True
            err(
                "STMT_AMOUNT",
                f"流水 房间{room} {row['start']}→{row['end']} "
                f"{nights}天×{price}={nights * price}，写 {amount}",
                room=room,
            )
        stmt_segments.append(
            {
                "room": room,
                "start": start,
                "end": end,
                "nights": nights,
                "price": price,
                "amount": amount,
            }
        )
    if statement_incomplete:
        warn(
            "STATEMENT_INCOMPLETE",
            f"酒店流水有 {len(statement_incomplete)} 行识别不完整（可能被印章遮挡），"
            "逐段匹配与合计校验按剩余可识别行进行，需人工补核："
            + "；".join(statement_incomplete[:8]),
        )

    # 流水里识别出的房间号若不在明细房间集合里（印章遮挡误识，如 308→806），
    # 也按"房间号未识别"处理，用日期段+天数+金额反查明细。
    detail_rooms = {segment["room"] for segment in segments}
    invalid_room_segments = [item for item in stmt_segments if item["room"] not in detail_rooms]
    if invalid_room_segments:
        stmt_segments = [item for item in stmt_segments if item["room"] in detail_rooms]
        for item in invalid_room_segments:
            unknown_room_segments.append(
                {
                    "start": item["start"],
                    "end": item["end"],
                    "nights": item["nights"],
                    "price": item["price"],
                    "amount": item["amount"],
                }
            )

    stmt_total = sum(
        (item["amount"] for item in stmt_segments + unknown_room_segments), Decimal("0")
    )
    stmt_nights = sum(item["nights"] for item in stmt_segments + unknown_room_segments)

    detail_index = {(seg["room"], seg["start"], seg["end"]): seg for seg in segments}
    stmt_index = {(seg["room"], seg["start"], seg["end"]): seg for seg in stmt_segments}
    matched_detail_keys: set[tuple] = set()
    for key, seg in detail_index.items():
        other = stmt_index.get(key)
        if other is None:
            continue
        matched_detail_keys.add(key)
        for field in ("nights", "price", "amount"):
            if seg[field] != other[field]:
                statement_failed = True
                err(
                    "STATEMENT_VALUE_DIFF",
                    f"房间{seg['room']} {seg['start']}→{seg['end']} {field}：明细 {seg[field]} vs 流水 {other[field]}",
                    room=seg["room"],
                    field=field,
                )
    # 房间号未识别（印章遮挡）的流水行：按日期段+天数+金额反查明细
    for segment in unknown_room_segments:
        candidate = None
        for key, seg in detail_index.items():
            if key in matched_detail_keys:
                continue
            if (
                seg["start"] == segment["start"]
                and seg["end"] == segment["end"]
                and seg["nights"] == segment["nights"]
                and seg["amount"] == segment["amount"]
            ):
                candidate = (key, seg)
                break
        if candidate is None:
            warn(
                "STATEMENT_ROOM_UNMATCHED",
                f"酒店流水有一行房间号未识别（{segment['start']}→{segment['end']} "
                f"{segment['nights']}天 {segment['amount']} 元），且无法按日期段+金额匹配到明细，需人工核对",
            )
            continue
        key, seg = candidate
        matched_detail_keys.add(key)
        note(
            "STATEMENT_ROOM_MATCHED",
            f"酒店流水有一行房间号被印章遮挡未识别，按日期段+天数+金额唯一匹配到明细 房间{seg['room']} "
            f"{seg['start']}→{seg['end']}，已按该房间核对",
            room=seg["room"],
        )
    for key, seg in detail_index.items():
        if key in matched_detail_keys:
            continue
        message = f"明细中 房间{seg['room']} {seg['start']}→{seg['end']} 在酒店流水里找不到"
        if statement_incomplete or unknown_room_segments:
            warn("MISSING_IN_STATEMENT", message + "（流水有未识别行，可能属于未识别部分）", room=seg["room"])
        else:
            statement_failed = True
            err("MISSING_IN_STATEMENT", message, room=seg["room"])
    for key, seg in stmt_index.items():
        if key not in detail_index:
            statement_failed = True
            err(
                "MISSING_IN_DETAIL",
                f"酒店流水中 房间{seg['room']} {seg['start']}→{seg['end']} 在明细表里找不到",
                room=seg["room"],
            )

    if not statement_incomplete:
        if stmt_total != total_amount:
            statement_failed = True
            err(
                "STATEMENT_TOTAL",
                f"流水合计 {stmt_total} ≠ 明细合计 {total_amount}",
                expected=str(stmt_total),
                actual=str(total_amount),
            )
        if stmt_nights != total_nights:
            statement_failed = True
            err(
                "STATEMENT_NIGHTS_TOTAL",
                f"流水总天数 {stmt_nights} ≠ 明细总天数 {total_nights}",
                expected=str(stmt_nights),
                actual=str(total_nights),
            )
    for index, value in enumerate(statement.get("payment_amounts") or []):
        if parse_decimal(value, "支付金额") != total_amount:
            statement_failed = True
            err(
                "PAYMENT_AMOUNT",
                f"流水支付金额（第{index + 1}个）{value} ≠ 明细合计 {total_amount}",
            )

    per_day = {
        str(night): {
            "headcount": sum(len(segs[0]["names"]) for segs in rooms.values()),
            "rooms": len(rooms),
        }
        for night, rooms in sorted(room_nights.items())
    }

    hard_failure = bool(errors)
    status = "fail" if hard_failure else ("needs_review" if warnings else "pass")

    return {
        "schema_version": SCHEMA,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "rules": [
            {"rule": "double_room", "name": "双人间/单住", "status": "fail" if double_room_failed else "pass"},
            {"rule": "duplicate", "name": "同一天房间/人员不重复", "status": "fail" if duplicate_failed else "pass"},
            {"rule": "nights_amount", "name": "天数与金额", "status": "fail" if (nights_check_failed or amount_check_failed) else "pass"},
            {"rule": "rate_limit", "name": "单价上限", "status": "fail" if rate_limit_failed else "pass"},
            {
                "rule": "signature",
                "name": "入住人员=签字",
                "status": "fail" if any(item["rule"] == "SIGNATURE_MISMATCH" for item in errors) else ("review" if signature_failed else "pass"),
            },
            {"rule": "subtotal", "name": "小计/合计", "status": "fail" if any(item["rule"].startswith("SUBTOTAL") or item["rule"].startswith("TOTAL") for item in errors) else "pass"},
            {"rule": "statement", "name": "酒店流水匹配", "status": "fail" if statement_failed else "pass"},
        ],
        "summary": {
            "n_detail_segments": len(segments),
            "total_nights": total_nights,
            "total_amount": str(total_amount),
            "statement_segments": len(stmt_segments),
            "statement_total": str(stmt_total),
            "statement_nights": stmt_nights,
            "n_errors": len(errors),
            "n_warnings": len(warnings),
            "n_notes": len(notes),
            "first_night": str(min(room_nights)) if room_nights else None,
            "last_night": str(max(room_nights)) if room_nights else None,
            "department": sheet.get("department") or data.get("department"),
            "per_sheet": per_sheet_summary,
        },
        "single_rooms": single_records,
        "room_gaps": gaps,
        "per_day": per_day,
    }


def check_detail_only(data: dict) -> dict:
    """机关待遇/无流水模式：只核对明细表（无房间号、不核酒店流水）。

    适用的表格：序号/入住人员/开始日期/结束日期/住宿天数/单价/住宿金额/签字确认。
    规则：天数=结束-开始、金额=天数×单价、每张表小计与大小写、同一人同一天不重复、
    入住人员=签字（手写签字只能人工核对）；不核对双人间/单住与酒店流水。
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    notes: list[dict] = []

    def err(rule: str, message: str, **extra):
        errors.append({"rule": rule, "message": message, **extra})

    def warn(rule: str, message: str, **extra):
        warnings.append({"rule": rule, "message": message, **extra})

    def note(rule: str, message: str, **extra):
        notes.append({"rule": rule, "message": message, **extra})

    sheets = data.get("detail_sheets") or []
    if not sheets:
        raise AccommodationError("BAD_INPUT", "detail_sheets 为空，无法核对")
    year = int(data.get("year") or date.today().year)
    policy = data.get("policy") or {}
    rate_cap_raw = policy.get("rate_cap_office") or policy.get("rate_cap") or "200"
    rate_cap = parse_decimal(rate_cap_raw, "机关住宿费上限")
    signature_verified = bool(data.get("signature_verified"))

    total_nights = 0
    total_amount = Decimal("0")
    all_segments: list[dict] = []
    per_sheet_totals: list[dict] = []
    prices_used: dict[str, int] = defaultdict(int)
    nights_ok = amounts_ok = subtotals_ok = capitals_ok = signatures_ok = True
    rate_limit_failed = False
    signature_unverified = False
    person_nights: dict[str, dict[date, str]] = defaultdict(dict)
    duplicate_person = False

    for sheet_index, sheet in enumerate(sheets):
        label = f"第{sheet_index + 1}张表"
        rows = sheet.get("rows") or []
        if not rows:
            err("SHEET_EMPTY", f"{label}没有识别到数据行")
            continue
        sheet_nights = 0
        sheet_amount = Decimal("0")
        for row in rows:
            seq = row.get("seq")
            names = [str(name).strip() for name in (row.get("names") or []) if str(name).strip()]
            signature = [str(name).strip() for name in (row.get("signature") or []) if str(name).strip()]
            if not names:
                warn("ROW_NAMES_MISSING", f"{label}序号{seq} 没有入住人员姓名，需人工核对")
            if signature:
                if set(signature) != set(names):
                    signatures_ok = False
                    err(
                        "SIGNATURE_MISMATCH",
                        f"{label}序号{seq} 入住人员（{ '、'.join(names) }）与签字（{ '、'.join(signature) }）不是同一组人",
                        seq=seq,
                    )
            else:
                signature_unverified = True
            for period in row.get("periods") or []:
                start_text = period.get("start")
                end_text = period.get("end")
                try:
                    start = parse_cn_date(start_text, year)
                    end = parse_cn_date(end_text, year)
                except AccommodationError:
                    err("DATE_INVALID", f"{label}序号{seq} 日期无法识别：{start_text} / {end_text}", seq=seq)
                    continue
                computed_nights = (end - start).days
                nights = period.get("nights")
                if nights is not None and int(nights) != computed_nights:
                    nights_ok = False
                    err(
                        "NIGHTS_MISMATCH",
                        f"{label}序号{seq} 住宿天数 {nights} ≠ 结束-开始（{computed_nights} 天）：{start_text}→{end_text}",
                        seq=seq,
                    )
                price = parse_decimal(period.get("price"), "单价")
                amount = parse_decimal(period.get("amount"), "住宿金额")
                if computed_nights * price != amount:
                    amounts_ok = False
                    err(
                        "AMOUNT_MISMATCH",
                        f"{label}序号{seq} 金额 {amount} ≠ 天数×单价（{computed_nights}×{price} = {computed_nights * price}）",
                        seq=seq,
                    )
                if price > rate_cap:
                    rate_limit_failed = True
                    err(
                        "RATE_OVER_LIMIT",
                        f"{label}序号{seq} 单价 {price} 元/天超过机关住宿费上限 {rate_cap} 元/天",
                        seq=seq,
                    )
                prices_used[str(price)] += computed_nights
                sheet_nights += computed_nights
                sheet_amount += amount
                for person in names:
                    for offset in range(computed_nights):
                        night = start + timedelta(days=offset)
                        previous = person_nights[person].get(night)
                        if previous is not None and previous != f"{label}序号{seq}":
                            duplicate_person = True
                            err(
                                "DUPLICATE_PERSON_NIGHT",
                                f"{person} 在同一天（{night}）重复出现在 {previous} 和 {label}序号{seq}",
                            )
                        person_nights[person][night] = f"{label}序号{seq}"
                all_segments.append(
                    {
                        "sheet": sheet_index + 1,
                        "seq": seq,
                        "names": names,
                        "start": str(start),
                        "end": str(end),
                        "nights": computed_nights,
                        "price": str(price),
                        "amount": str(amount),
                    }
                )
        subtotal = sheet.get("subtotal") or {}
        subtotal_nights = subtotal.get("nights")
        subtotal_amount = subtotal.get("amount")
        if subtotal_nights is not None and int(subtotal_nights) != sheet_nights:
            subtotals_ok = False
            err(
                "SUBTOTAL_NIGHTS",
                f"{label}小计天数 {subtotal_nights} ≠ 逐行合计 {sheet_nights}",
                expected=str(sheet_nights),
                actual=str(subtotal_nights),
            )
        if subtotal_amount not in (None, ""):
            if parse_decimal(subtotal_amount) != sheet_amount:
                subtotals_ok = False
                err(
                    "SUBTOTAL_AMOUNT",
                    f"{label}小计金额 {subtotal_amount} ≠ 逐行合计 {sheet_amount}",
                    expected=str(sheet_amount),
                    actual=str(subtotal_amount),
                )
        lowercase = sheet.get("lowercase_amount")
        if lowercase not in (None, ""):
            if parse_decimal(lowercase) != sheet_amount:
                capitals_ok = False
                err(
                    "LOWERCASE_MISMATCH",
                    f"{label}合计小写 {lowercase} ≠ 逐行合计 {sheet_amount}",
                    expected=str(sheet_amount),
                    actual=str(lowercase),
                )
        capital = sheet.get("capital_amount")
        if capital:
            expected_capital = cn_capital(sheet_amount)
            if str(capital).strip() != expected_capital:
                capitals_ok = False
                err(
                    "CAPITAL_MISMATCH",
                    f"{label}合计大写「{capital}」≠ 应为「{expected_capital}」",
                    expected=expected_capital,
                    actual=str(capital),
                )
        total_nights += sheet_nights
        total_amount += sheet_amount
        per_sheet_totals.append(
            {
                "sheet": sheet_index + 1,
                "n_rows": len(rows),
                "nights": sheet_nights,
                "amount": str(sheet_amount),
                "lowercase_amount": lowercase,
                "capital_amount": capital,
            }
        )

    # ---------- 其他提示 ----------
    note("ROOM_INFO_MISSING", "此表没有房间号列，双人间/单住规则无法机读判断（机关待遇按人天报销）")
    note("STATEMENT_NOT_REQUIRED", "设计室享受机关待遇，按用户口径不核对酒店流水")
    if signature_unverified and not signature_verified:
        warnings.append(
            {
                "rule": "SIGNATURE_UNVERIFIED",
                "message": "签字为手写体，无法机读，需人工核对签字与入住人员是否同一组人",
            }
        )
    elif signature_verified:
        note("SIGNATURE_VERIFIED", "签字为手写体，已人工核对与入住人员一致")
    if prices_used and not rate_limit_failed:
        note(
            "RATE_WITHIN_LIMIT",
            f"单价均在机关住宿费上限 {rate_cap} 元/天内："
            + "、".join(f"{price} 元/天（{nights} 晚）" for price, nights in sorted(prices_used.items())),
        )
    if len(prices_used) > 1:
        note(
            "RATE_MULTIPLE",
            "本批出现多个单价：" + "、".join(f"{price} 元/天（{nights} 晚）" for price, nights in sorted(prices_used.items())),
        )

    # 逐日人数（按人天占用算）
    day_people: dict[str, set[str]] = defaultdict(set)
    for segment in all_segments:
        start = date.fromisoformat(segment["start"])
        for offset in range(segment["nights"]):
            day_people[str(start + timedelta(days=offset))].add(tuple(segment["names"])[0] if segment["names"] else "")
    per_day = {day: {"headcount": len(people), "rooms": None} for day, people in sorted(day_people.items())}

    first_night = min((date.fromisoformat(segment["start"]) for segment in all_segments), default=None)
    last_night = max((date.fromisoformat(segment["end"]) - timedelta(days=1) for segment in all_segments), default=None)

    signature_rule = "fail" if not signatures_ok else ("pass" if signature_verified else "review")
    status = "fail" if errors else ("needs_review" if warnings else "pass")
    return {
        "schema_version": SCHEMA,
        "status": status,
        "mode": "detail_only",
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "rules": [
            {"rule": "double_room", "name": "双人间/单住", "status": "skip", "detail": "无房间号列，不适用"},
            {"rule": "duplicate", "name": "同一天人员不重复", "status": "fail" if duplicate_person else "pass"},
            {"rule": "nights_amount", "name": "天数与金额", "status": "fail" if (not nights_ok or not amounts_ok) else "pass"},
            {"rule": "rate_limit", "name": "单价上限", "status": "fail" if rate_limit_failed else "pass"},
            {"rule": "signature", "name": "入住人员=签字", "status": signature_rule},
            {"rule": "subtotal", "name": "小计/合计", "status": "fail" if (not subtotals_ok or not capitals_ok) else "pass"},
            {"rule": "statement", "name": "酒店流水匹配", "status": "skip", "detail": "机关待遇，不核流水"},
        ],
        "summary": {
            "n_sheets": len(sheets),
            "n_detail_segments": len(all_segments),
            "total_nights": total_nights,
            "total_amount": str(total_amount),
            "n_errors": len(errors),
            "n_warnings": len(warnings),
            "n_notes": len(notes),
            "first_night": str(first_night) if first_night else None,
            "last_night": str(last_night) if last_night else None,
            "department": data.get("department"),
            "no_statement": True,
            "no_room": True,
            "prices": {price: nights for price, nights in sorted(prices_used.items())},
            "per_sheet": per_sheet_totals,
        },
        "single_rooms": [],
        "room_gaps": [],
        "per_day": per_day,
        "segments": all_segments,
    }
