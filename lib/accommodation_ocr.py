# -*- coding: utf-8 -*-
"""住宿费单据的固定版式识别（施工住宿费明细表 + 酒店流水）。

这一层只做"OCR 行 -> 结构化数据"，不做业务判断（业务规则在 accommodation.py）。
针对用户 2026-11 确认的版式：

施工住宿费明细表
  列：序号 | 房间号 | 入住人员 | 开始日期 | 结束日期 | 住宿天数 | 单价 |
      住宿金额 | 补助天数 | 补助金额 | 签字确认
  特点：一个序号可能含 2 个日期段（合并单元格），明细可能跨 2 页，末页有 小计/合计(小写/大写)。

酒店流水
  列：序号 | 房间号 | 开始日期 | 结束日期 | 累计天数 | 单价(元/天) | 金额（元）
  特点：一个房间可能 2 个日期段；底部有 支付金额。

识别策略：
- 明细表的表格线是深色实线：用 OpenCV 检测网格线，得到精确的列边界和行带；
  这样"补助金额 / 签字确认"这种窄列 + 左侧手写签名不会被错分到相邻列。
- 酒店流水没有可靠表格线：按表头文字位置划分列，按房间号锚点切分记录。
- 手写签字无法可靠机读：签字列一律标记 signature_uncertain=True，交由业务层提示人工核对。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - 依赖缺失时给出明确错误
    cv2 = None

SCHEMA = "finance-accommodation/1"

# 明细表字段（顺序与表格列顺序一致），aliases 兼容 OCR 常见误识
DETAIL_FIELDS = [
    ("seq", ["序号", "序亏", "序"]),
    ("room", ["房间号", "方间号", "房问号", "房间亏", "房号"]),
    ("names", ["入住人员", "入住人负", "入住人员"]),
    ("start", ["开始日期", "开蛤日期", "开始日其"]),
    ("end", ["结束日期", "站束日期", "结束日其"]),
    ("nights", ["住宿天数", "住宿天败", "住宿夭数"]),
    ("price", ["单价"]),
    ("amount", ["住宿金额", "住宿金领"]),
    ("subsidy_nights", ["补助天数", "补助天败"]),
    ("subsidy_amount", ["补助金额", "并助金额", "补助金领"]),
    ("signature", ["签字确认", "签字确让", "签宇确认"]),
]

STATEMENT_FIELDS = [
    ("seq", ["序号"]),
    ("room", ["房间号", "方间号"]),
    ("start", ["开始日期"]),
    ("end", ["结束日期", "站束日期"]),
    ("nights", ["累计", "天数", "累计天数"]),
    ("price", ["单价", "元/天"]),
    ("amount", ["金额", "元"]),
]

DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
CAPITAL_RE = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分整]{2,}")
ROOM_RE = re.compile(r"^\d{3}[A-Za-z]?$")

# 常见姓氏：用于把"王三董晓威"这类粘在一起的姓名切成两个人
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏"
    "潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮"
    "卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘"
    "缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴"
    "糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶"
    "幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉"
    "宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终"
    "暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙"
    "乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公年"
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _imread_unicode(path) -> "np.ndarray | None":
    if cv2 is None:
        return None
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None


def _merge_axis(values, gap: int = 3) -> list[int]:
    groups: list[list[int]] = []
    for value in sorted(set(int(v) for v in values)):
        if groups and value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [int(round(sum(group) / len(group))) for group in groups]


def detect_grid(image_path) -> dict | None:
    """检测表格网格线，返回 {"xs": [...], "ys": [...], "width", "height"} 或 None。"""
    gray = _imread_unicode(image_path)
    if gray is None or gray.size == 0:
        return None
    height, width = gray.shape
    best = None
    for threshold in (160, 190, 210, 225):
        _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, int(height * 0.05))))
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)
        col_sums = (vertical > 0).sum(axis=0)
        xs = [int(x) for x in np.where(col_sums > height * 0.5)[0]]
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, int(width * 0.08)), 1))
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)
        row_sums = (horizontal > 0).sum(axis=1)
        ys = [int(y) for y in np.where(row_sums > width * 0.3)[0]]
        merged_x = _merge_axis(xs)
        merged_y = _merge_axis(ys)
        if len(merged_x) >= 4 and len(merged_y) >= 3:
            candidate = {"xs": merged_x, "ys": merged_y, "width": width, "height": height}
            if best is None or len(merged_x) + len(merged_y) > best["_score"]:
                candidate["_score"] = len(merged_x) + len(merged_y)
                best = candidate
    if best:
        best.pop("_score", None)
    return best


def _bands(ys: list[int], height: int) -> list[tuple[int, int]]:
    edges = list(ys)
    if not edges or edges[0] > height * 0.02:
        edges.insert(0, 0)
    if edges[-1] < height - 2:
        edges.append(height)
    return [(edges[index], edges[index + 1]) for index in range(len(edges) - 1)]


def _intervals(xs: list[int], width: int) -> list[tuple[int, int, float]]:
    """把竖线坐标变成列区间（含中心点）。"""
    edges = list(xs)
    if not edges or edges[0] > width * 0.02:
        edges.insert(0, 0)
    if edges[-1] < width - 2:
        edges.append(width)
    out = []
    for index in range(len(edges) - 1):
        x0, x1 = edges[index], edges[index + 1]
        out.append((x0, x1, (x0 + x1) / 2.0))
    # 去掉左右边缘的窄条（右边界线外的那几个像素），它们不是数据列
    while out and (out[-1][1] - out[-1][0]) < max(6, width * 0.012):
        out.pop()
    while out and (out[0][1] - out[0][0]) < max(6, width * 0.012):
        out.pop(0)
    return out


def _assign_columns(lines: list[dict], intervals: list[tuple[int, int, float]]) -> list[list[dict]]:
    buckets: list[list[dict]] = [[] for _ in intervals]
    for line in lines:
        x1, _, x2, _ = line["bbox"]
        center = (x1 + x2) / 2.0
        for index, (x0, x1b, _) in enumerate(intervals):
            if x0 <= center < x1b:
                buckets[index].append(line)
                break
    return buckets


def _lines_in_band(lines: list[dict], y0: int, y1: int) -> list[dict]:
    out = []
    for line in lines:
        _, yy0, _, yy1 = line["bbox"]
        center = (yy0 + yy1) / 2.0
        if y0 <= center < y1:
            out.append(line)
    return out


def _fuzzy_score(alias: str, text: str) -> float:
    """表头文字匹配打分：容忍 OCR 把"房间号"认成"厉间号"、"签字确认"截成"签字认"。"""
    if not alias or not text:
        return 0.0
    if text == alias:
        return 1.0
    if alias in text:
        return 0.9
    if text in alias and len(text) >= 2:
        return 0.85
    # 最长公共子串（中文表头短，逐 2-gram 比对足够）
    best = 0
    for size in range(min(len(alias), len(text)), 1, -1):
        if any(alias[index:index + size] in text for index in range(len(alias) - size + 1)):
            best = size
            break
    if best >= 2:
        return 0.55 + 0.05 * best
    return 0.0


def _match_field(text: str, fields) -> str | None:
    normalized = _norm(text)
    if not normalized:
        return None
    best_score = 0.0
    best_field = None
    for field, aliases in fields:
        for alias in aliases:
            score = _fuzzy_score(alias, normalized)
            if score > best_score:
                best_score = score
                best_field = field
    return best_field if best_score >= 0.6 else None


def _parse_number(text: str, integer: bool = False):
    normalized = _norm(text).replace(",", "").replace("，", "")
    match = NUM_RE.search(normalized)
    if not match:
        return None
    value = Decimal(match.group(0))
    if integer:
        return int(value)
    return value


def _extract_dates(text: str) -> list[str]:
    return [match.group(0) for match in DATE_RE.finditer(_norm(text).replace(" ", ""))]


def _split_names(text: str) -> tuple[list[str], bool]:
    """把 '王三董晓威' 这类粘连姓名切成 ['王三','董晓威']；返回 (names, uncertain)。"""
    cleaned = _norm(text)
    for token in ("入住人员", "签字确认"):
        cleaned = cleaned.replace(token, "")
    if not cleaned:
        return [], True
    # 去掉可能粘进来的日期
    cleaned = DATE_RE.sub("", cleaned)
    if not cleaned:
        return [], True
    parts = [part for part in re.split(r"[、,，/;；\s]+", cleaned) if part]
    if len(parts) >= 2:
        return parts, False
    single = parts[0]
    length = len(single)
    if length <= 4 and length >= 2:
        # 2-4 个字：可能是单人名，也可能是两个两字名
        if length == 4:
            candidates = [index for index in range(1, length) if single[index] in SURNAMES]
            if candidates:
                split = candidates[0]
                return [single[:split], single[split:]], False
            return [single[:2], single[2:]], True
        return [single], False
    if 5 <= length <= 8:
        candidates = [
            index
            for index in range(2, length - 1)
            if single[index] in SURNAMES and 1 < index < length - 1
        ]
        if candidates:
            # 常见姓名 2~3 字：优先第一段 3 字（张三+李四=2+2 也能匹配），
            # 再按与 3 的距离兜底，避免把"曹四钱七"切成"郭建/平钱七"。
            split = min(candidates, key=lambda index: (abs(index - 3), index))
            return [single[:split], single[split:]], False
        split = length // 2
        return [single[:split], single[split:]], True
    # 超过 8 个字：按 3 字一段硬切，标记不确定
    return [single[index : index + 3] for index in range(0, length, 3)], True


def _header_columns(lines: list[dict], intervals, width: int, fields):
    """用表头文字确定每一列对应的字段；返回 [(field, x0, x1, center), ...]。"""
    mapping = {}
    for line in lines:
        field = _match_field(line["text"], fields)
        if not field or field in mapping:
            continue
        x1, _, x2, _ = line["bbox"]
        center = (x1 + x2) / 2.0
        for index, (x0, x1b, _) in enumerate(intervals):
            if x0 <= center < x1b:
                mapping[field] = index
                break
    columns = []
    for index, (x0, x1, center) in enumerate(intervals):
        field = None
        for name, column_index in mapping.items():
            if column_index == index:
                field = name
                break
        columns.append((field, x0, x1, center))
    return columns


def extract_detail(pages: list[tuple[str, dict]]) -> dict:
    """明细表（1~2 页）识别。pages: [(image_path, ocr_result), ...]。"""
    all_records: list[dict] = []
    subtotal = {}
    lowercase = None
    capital = None
    department = None
    title = None
    field_order = [field for field, _ in DETAIL_FIELDS]

    for page_index, (image_path, ocr_result) in enumerate(pages):
        lines = [line for line in ocr_result.get("lines") or [] if _norm(line.get("text"))]
        grid = detect_grid(image_path)
        if not grid:
            continue
        intervals = _intervals(grid["xs"], grid["width"])
        bands = _bands(grid["ys"], grid["height"])

        # 表头行：含 >=3 个表头关键词的行带；用于跳过并在其上找标题/部门
        header_band = None
        for band_index, (y0, y1) in enumerate(bands):
            band_lines = _lines_in_band(lines, y0, y1)
            matched = {field for field in (_match_field(line["text"], DETAIL_FIELDS) for line in band_lines) if field}
            if len(matched) >= 3:
                header_band = band_index
                dept_text = None
                unit_text = None
                for line in lines:
                    if line["bbox"][3] > y0:
                        continue
                    text = line["text"].strip()
                    if "明细表" in text and title is None:
                        title = text
                    if "部门" in text and dept_text is None:
                        dept_text = text
                    elif "单位" in text and unit_text is None:
                        unit_text = text
                if dept_text:
                    cleaned = re.sub(r"^.*?部门[:：]?", "", dept_text)
                    cleaned = re.sub(r"[（(].*?[）)]", "", cleaned).replace("盖章", "").strip()
                    department = cleaned or None
                elif unit_text:
                    department = unit_text
                break

        # 版式固定：列数 = 标准字段数时直接按表格顺序对应；
        # 只有版式变了（列数不一致）才回退到表头关键词匹配。
        if len(intervals) == len(field_order):
            columns = [
                (field_order[index], x0, x1, center)
                for index, (x0, x1, center) in enumerate(intervals)
            ]
        else:
            header_lines = (
                _lines_in_band(lines, *bands[header_band]) if header_band is not None else []
            )
            columns = _header_columns(header_lines, intervals, grid["width"], DETAIL_FIELDS)
            if not any(field for field, *_ in columns):
                columns = [
                    (field_order[index] if index < len(field_order) else None, x0, x1, center)
                    for index, (x0, x1, center) in enumerate(intervals)
                ]
        fields = [column[0] for column in columns]
        # 表头"序号房号"被 OCR 合并成一个单元格时，房号列会缺失/错位：
        # 按"序号-房号-入住人员"的固定版式位置把两列纠正回来（9 列版式常见）。
        if "room" not in fields and "seq" in fields and "names" in fields:
            seq_index = fields.index("seq")
            names_index = fields.index("names")
            if names_index - seq_index >= 2:
                room_index = seq_index + 1
                columns[room_index] = ("room",) + tuple(columns[room_index][1:])
                fields[room_index] = "room"
            elif names_index == seq_index + 1 and seq_index >= 1 and fields[seq_index - 1] is None:
                # 合并表头被识别到了房号列：序号应在左一列，房号在中间列
                columns[seq_index - 1] = ("seq",) + tuple(columns[seq_index - 1][1:])
                fields[seq_index - 1] = "seq"
                columns[seq_index] = ("room",) + tuple(columns[seq_index][1:])
                fields[seq_index] = "room"

        # ---- 逐行带收集字段，再按"房间号/序号"锚点把行带归并成记录 ----
        # 只从表头下方开始处理，避免把标题/单位/logo 行当成数据行。
        start_band = (header_band + 1) if header_band is not None else 0
        infos: list[dict] = []
        for band_index in range(start_band, len(bands)):
            y0, y1 = bands[band_index]
            band_lines = _lines_in_band(lines, y0, y1)
            if not band_lines:
                continue
            buckets = _assign_columns(band_lines, [(x0, x1, center) for _, x0, x1, center in columns])

            def texts_of(field: str, _buckets=buckets) -> list[str]:
                if field not in fields:
                    return []
                index = fields.index(field)
                if index >= len(_buckets):
                    return []
                return [line["text"].strip() for line in _buckets[index] if line["text"].strip()]

            joined = " ".join(_norm(line["text"]) for line in band_lines)
            if "小计" in joined:
                values = texts_of("nights")
                if values:
                    subtotal["nights"] = _parse_number(values[0], integer=True)
                values = texts_of("amount")
                if values:
                    subtotal["amount"] = str(_parse_number(values[0]))
                values = texts_of("subsidy_nights")
                if values:
                    subtotal["subsidy_nights"] = _parse_number(values[0], integer=True)
                continue
            if "大写" in joined:
                for line in band_lines:
                    match = CAPITAL_RE.search(_norm(line["text"]))
                    if match and len(match.group(0)) >= 4:
                        capital = match.group(0)
                continue
            if "小写" in joined:
                for line in band_lines:
                    if "小写" in _norm(line["text"]):
                        continue
                    cleaned = _norm(line["text"]).replace(",", "").replace("，", "")
                    value = _parse_number(cleaned)
                    if value is not None and len(NUM_RE.findall(cleaned)) == 1:
                        lowercase = str(value)
                continue

            # 小计行有时没有 OCR 出"小计"两个字：只有天数/金额、没有姓名和日期 → 按小计处理
            probe_start_dates: list[str] = []
            for text in texts_of("start"):
                probe_start_dates.extend(_extract_dates(text))
            if not probe_start_dates and not texts_of("names") and not subtotal:
                probe_nights = texts_of("nights")
                probe_amount = texts_of("amount")
                if probe_nights and probe_amount:
                    nights_value = _parse_number(probe_nights[0], integer=True)
                    amount_value = _parse_number(probe_amount[0])
                    if nights_value and amount_value is not None:
                        subtotal["nights"] = nights_value
                        subtotal["amount"] = str(amount_value)
                        continue

            seq_texts = texts_of("seq")
            room_texts = texts_of("room")
            name_texts = texts_of("names")
            start_texts = texts_of("start")
            end_texts = texts_of("end")
            nights_texts = texts_of("nights")
            price_texts = texts_of("price")
            amount_texts = texts_of("amount")
            signature_texts = texts_of("signature")

            start_dates: list[str] = []
            for text in start_texts:
                start_dates.extend(_extract_dates(text))
            name_joined = " ".join(name_texts)
            if not start_dates:
                start_dates = _extract_dates(name_joined)
                if start_dates:
                    name_joined = DATE_RE.sub("", name_joined)
            end_dates: list[str] = []
            for text in end_texts:
                end_dates.extend(_extract_dates(text))

            room = next(
                (match.group(0) for text in room_texts for match in [ROOM_RE.search(_norm(text))] if match),
                "",
            )
            seq = _parse_number(seq_texts[0], integer=True) if seq_texts else None
            anchor_y = None
            for field in ("room", "seq"):
                if field in fields and fields.index(field) < len(buckets) and buckets[fields.index(field)]:
                    line = buckets[fields.index(field)][0]
                    anchor_y = (line["bbox"][1] + line["bbox"][3]) / 2.0
                    break
            names, names_uncertain = _split_names(name_joined) if name_joined else ([], False)
            if "names" in fields and fields.index("names") < len(buckets):
                sim = [line["confidence"] for line in buckets[fields.index("names")]]
                if sim and min(sim) < 0.9:
                    names_uncertain = True
            if not start_dates and not end_dates and not room and seq is None:
                continue  # 标题/logo/水印等非数据行
            infos.append(
                {
                    "band_index": band_index,
                    "y_center": (y0 + y1) / 2.0,
                    "anchor_y": anchor_y,
                    "seq": seq,
                    "room": room,
                    "names": names,
                    "names_uncertain": names_uncertain,
                    "starts": start_dates,
                    "ends": end_dates,
                    "nights": _parse_number(nights_texts[0], integer=True) if nights_texts else None,
                    "price": (
                        str(_parse_number(price_texts[0]))
                        if price_texts and _parse_number(price_texts[0]) is not None else None
                    ),
                    "amount": (
                        str(_parse_number(amount_texts[0]))
                        if amount_texts and _parse_number(amount_texts[0]) is not None else None
                    ),
                    "has_signature": bool(signature_texts),
                    "has_data": bool(start_dates or end_dates or nights_texts or amount_texts),
                }
            )

        # 记录边界：以"房间号/序号"所在 y 为锚，相邻锚点中点即分界
        anchor_items = sorted(
            [(info["anchor_y"], info) for info in infos if info["anchor_y"] is not None],
            key=lambda item: item[0],
        )
        if not anchor_items:
            continue
        anchor_ys = [item[0] for item in anchor_items]
        bounds = [-1e9] + [
            (left + right) / 2.0 for left, right in zip(anchor_ys, anchor_ys[1:])
        ] + [1e9]
        slots = [
            {
                "seq": anchor_info["seq"],
                "room": anchor_info["room"],
                "names": anchor_info["names"],
                "names_uncertain": anchor_info["names_uncertain"],
                "signature_uncertain": False,
                "periods": [],
                "infos": [],
            }
            for _, anchor_info in anchor_items
        ]
        for info in infos:
            if not info["has_data"] and info["anchor_y"] is None:
                continue
            slot_index = None
            for index in range(len(slots)):
                if bounds[index] <= info["y_center"] < bounds[index + 1]:
                    slot_index = index
                    break
            if slot_index is None:
                continue
            slot = slots[slot_index]
            slot["infos"].append(info)
            if info["names"] and not slot["names"]:
                slot["names"] = info["names"]
                slot["names_uncertain"] = info["names_uncertain"]
            if not slot["room"] and info["room"]:
                slot["room"] = info["room"]
            if slot["seq"] is None and info["seq"] is not None:
                slot["seq"] = info["seq"]
            if info["has_signature"]:
                slot["signature_uncertain"] = True
        # 房号单元格纵向合并时只有一行能 OCR 到房号：按"日期段首尾相接"补到缺房号的那一行
        def _slot_bounds(slot):
            items = [info for info in slot["infos"] if info["starts"] and info["ends"]]
            if not items:
                return None
            return items[0]["starts"][0], items[-1]["ends"][-1]

        for index, slot in enumerate(slots):
            if slot["room"]:
                continue
            own = _slot_bounds(slot)
            if not own:
                continue
            for neighbor_index in (index - 1, index + 1):
                if not (0 <= neighbor_index < len(slots)):
                    continue
                neighbor = slots[neighbor_index]
                if not neighbor["room"]:
                    continue
                neighbor_bounds = _slot_bounds(neighbor)
                if not neighbor_bounds:
                    continue
                if neighbor_bounds[0] == own[1] or neighbor_bounds[1] == own[0]:
                    slot["room"] = neighbor["room"]
                    break
            if not slot["room"] and slot["names"]:
                # 两行是同一组人、中间隔了几天（合并单元格拆成两段）：按同名组补房号
                for neighbor_index in (index + 1, index - 1):
                    if not (0 <= neighbor_index < len(slots)):
                        continue
                    neighbor = slots[neighbor_index]
                    if neighbor["room"] and set(neighbor["names"]) == set(slot["names"]):
                        slot["room"] = neighbor["room"]
                        break
        for slot in slots:
            for info in sorted(slot["infos"], key=lambda item: item["band_index"]):
                if not info["starts"] and not info["ends"]:
                    continue
                slot["periods"].append(
                    {
                        "start": info["starts"][0] if info["starts"] else None,
                        "end": info["ends"][0] if info["ends"] else None,
                        "nights": info["nights"],
                        "price": info["price"],
                        "amount": info["amount"],
                    }
                )
            all_records.append(
                {
                    "seq": slot["seq"],
                    "room": slot["room"],
                    "names": slot["names"],
                    "names_uncertain": slot["names_uncertain"],
                    "periods": slot["periods"],
                    "signature": [],
                    "signature_uncertain": slot["signature_uncertain"],
                }
            )

    # 补齐：把没有 start/end 的周期丢掉并记录问题
    clean_rows = []
    problems = []
    for index, record in enumerate(all_records):
        periods = [period for period in record["periods"] if period.get("start") and period.get("end")]
        if len(periods) != len(record["periods"]) or not periods:
            problems.append(f"第{index + 1}条记录（房间{record.get('room') or '?'}）日期段识别不完整")
        record["periods"] = periods
        clean_rows.append(record)

    sheet = {
        "source": [str(page[0]) for page in pages],
        "rows": clean_rows,
        "subtotal": subtotal,
        "lowercase_amount": lowercase,
        "capital_amount": capital,
        "title": title,
        "department": department,
    }
    if problems:
        sheet["problems"] = problems
    return sheet


def _statement_columns(header_lines: list[dict], width: int) -> list[tuple]:
    """用表头文字中心点划分列；识别不到就按常见比例兜底。"""
    field_centers: dict[str, list[float]] = {}
    for line in header_lines:
        field = _match_field(line["text"], STATEMENT_FIELDS)
        if not field:
            continue
        x1, _, x2, _ = line["bbox"]
        field_centers.setdefault(field, []).append((x1 + x2) / 2.0)
    if len(field_centers) >= 4:
        centers = sorted((sum(values) / len(values), field) for field, values in field_centers.items())
        boundaries = [
            (left_center + right_center) / 2.0
            for (left_center, _), (right_center, _) in zip(centers, centers[1:])
        ]
        edges = [0.0] + boundaries + [float(width)]
        fields = [field for _, field in centers]
        return [
            (fields[index], int(edges[index]), int(edges[index + 1]),
             (edges[index] + edges[index + 1]) / 2.0)
            for index in range(len(fields))
        ]
    ratios = [0.0, 0.10, 0.22, 0.36, 0.50, 0.62, 0.73, 1.0]
    edges = [int(ratio * width) for ratio in ratios]
    names = ["seq", "room", "start", "end", "nights", "price", "amount"]
    return [
        (names[index], edges[index], edges[index + 1], (edges[index] + edges[index + 1]) / 2.0)
        for index in range(len(names))
    ]


def extract_statement(image_path: str, ocr_result: dict) -> dict:
    """酒店流水识别。没有可靠表格线，用表头定列、房间号/序号锚点定记录。"""
    lines = [line for line in ocr_result.get("lines") or [] if _norm(line.get("text"))]
    width = int(ocr_result.get("width") or 0)
    height = int(ocr_result.get("height") or 0)
    if not width or not height:
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size

    printed_date = None
    title = None
    header_lines: list[dict] = []
    for line in lines:
        text = line["text"]
        if "打印日期" in text:
            printed_date = text.strip()
        if "流水" in text and line["bbox"][1] < height * 0.15:
            title = text.strip()
        if line["bbox"][3] < height * 0.3:
            header_lines.append(line)
    columns = _statement_columns(header_lines, width)
    fields = [column[0] for column in columns]
    intervals = [(x0, x1, center) for _, x0, x1, center in columns]

    # 锚点：序号列的数字 + 房间号列的 3 位数字，按 y 合并（同一行的两个锚点取平均）
    seq_i = fields.index("seq") if "seq" in fields else 0
    room_i = fields.index("room") if "room" in fields else 1
    seq_x0, seq_x1 = columns[seq_i][1], columns[seq_i][2]
    room_x0, room_x1 = columns[room_i][1], columns[room_i][2]

    def in_span(line, x0, x1):
        x1_, _, x2_, _ = line["bbox"]
        center = (x1_ + x2_) / 2.0
        return x0 <= center < x1

    raw_anchors = []
    for line in lines:
        text = _norm(line["text"])
        center_y = (line["bbox"][1] + line["bbox"][3]) / 2.0
        if in_span(line, seq_x0, seq_x1) and NUM_RE.fullmatch(text.rstrip(".")):
            raw_anchors.append(center_y)
        elif in_span(line, room_x0, room_x1) and ROOM_RE.fullmatch(text.replace("房", "")):
            raw_anchors.append(center_y)
    raw_anchors.sort()
    anchors: list[float] = []
    for value in raw_anchors:
        if anchors and value - anchors[-1] < 12:
            anchors[-1] = (anchors[-1] + value) / 2.0
            continue
        anchors.append(value)
    if len(anchors) < 2:
        return {
            "source": image_path,
            "rows": [],
            "payment_amounts": [],
            "printed_date": printed_date,
            "title": title,
            "problems": ["酒店流水未识别到序号/房间号锚点"],
        }

    bounds = [0.0] + [(left + right) / 2.0 for left, right in zip(anchors, anchors[1:])] + [float(height)]
    rows: list[dict] = []
    problems: list[str] = []
    for band_index in range(len(bounds) - 1):
        y0, y1 = bounds[band_index], bounds[band_index + 1]
        band_lines = [
            line for line in lines
            if y0 <= (line["bbox"][1] + line["bbox"][3]) / 2.0 < y1
        ]
        if not band_lines:
            continue
        buckets = _assign_columns(band_lines, intervals)

        def texts_of(field: str) -> list[str]:
            if field not in fields:
                return []
            index = fields.index(field)
            if index >= len(buckets):
                return []
            return [line["text"].strip() for line in buckets[index] if line["text"].strip()]

        room_text = " ".join(texts_of("room"))
        match = ROOM_RE.search(_norm(room_text))
        room = match.group(0) if match else ""
        if not room:
            problems.append(f"第{band_index + 1}条流水记录房间号未识别（可能被印章遮挡）")
        dates_start = [date for text in texts_of("start") for date in _extract_dates(text)]
        dates_end = [date for text in texts_of("end") for date in _extract_dates(text)]
        nights_values = [
            value for text in texts_of("nights")
            for value in [_parse_number(text, integer=True)] if value is not None
        ]
        price_values = [
            value for text in texts_of("price")
            for value in [_parse_number(text)] if value is not None
        ]
        amount_values = [
            value for text in texts_of("amount")
            for value in [_parse_number(text)] if value is not None
        ]
        count = max(len(dates_start), len(dates_end))
        for index in range(count):
            rows.append(
                {
                    "room": room,
                    "start": dates_start[index] if index < len(dates_start) else None,
                    "end": dates_end[index] if index < len(dates_end) else None,
                    "nights": nights_values[index] if index < len(nights_values) else None,
                    "price": str(price_values[index]) if index < len(price_values) else None,
                    "amount": str(amount_values[index]) if index < len(amount_values) else None,
                }
            )

    # 支付金额：最后一条记录下方的纯数字行（在金额列附近）
    payment_amounts = []
    if fields:
        amount_i = fields.index("amount") if "amount" in fields else len(fields) - 1
        amount_x0, amount_x1 = columns[amount_i][1], columns[amount_i][2]
        last_anchor = anchors[-1]
        for line in lines:
            center_y = (line["bbox"][1] + line["bbox"][3]) / 2.0
            if center_y <= last_anchor + 15:
                continue
            text = _norm(line["text"])
            if not NUM_RE.fullmatch(text.rstrip(".")):
                continue
            _, _, x2, _ = line["bbox"]
            center_x = (line["bbox"][0] + x2) / 2.0
            if abs(center_x - (amount_x0 + amount_x1) / 2.0) < (amount_x1 - amount_x0):
                value = _parse_number(text)
                if value is not None:
                    payment_amounts.append(str(value))

    return {
        "source": image_path,
        "rows": rows,
        "payment_amounts": payment_amounts,
        "printed_date": printed_date,
        "title": title,
        "problems": problems,
    }


def _cluster_lines_by_y(lines: list[dict], tolerance: float = 9.0) -> list[list[dict]]:
    clusters: list[list[dict]] = []
    centers: list[float] = []
    for line in sorted(lines, key=lambda item: (item["bbox"][1] + item["bbox"][3]) / 2.0):
        center = (line["bbox"][1] + line["bbox"][3]) / 2.0
        if clusters and abs(center - centers[-1]) <= tolerance:
            clusters[-1].append(line)
            centers[-1] = sum((item["bbox"][1] + item["bbox"][3]) / 2.0 for item in clusters[-1]) / len(clusters[-1])
        else:
            clusters.append([line])
            centers.append(center)
    return clusters


def _number_text(value) -> str:
    text = str(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _parse_statement_row(cells: list[tuple[float, str]]) -> dict | None:
    """把一行的 OCR 单元格解析成流水记录（按日期位置分左右，再按顺序识别数字）。"""
    dates: list[tuple[float, str]] = []
    numbers: list[tuple[float, str, "object"]] = []
    for x_center, text in cells:
        normalized = _norm(text)
        tokens = re.findall(r"\d+", normalized)
        if any(len(token) >= 6 for token in tokens):
            continue  # 水印/打印时间等长数字
        found_dates = _extract_dates(text)
        if found_dates:
            for date_text in found_dates:
                dates.append((x_center, date_text))
            continue
        cleaned = normalized.replace(",", "").replace("，", "")
        if re.search(r"[年月日]", cleaned):
            continue
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            continue
        value = _parse_number(cleaned)
        if value is not None:
            numbers.append((x_center, cleaned, value))
    dates.sort(key=lambda item: item[0])
    if len(dates) < 2:
        return None
    start, end = dates[0][1], dates[1][1]
    first_date_x, last_date_x = dates[0][0], dates[-1][0]
    before = sorted(item for item in numbers if item[0] < first_date_x)
    after = sorted(item for item in numbers if item[0] > last_date_x)
    room = ""
    seq = None
    for _, text, value in before:
        if not room and re.fullmatch(r"\d{3}[A-Za-z]?", text) and 100 <= value <= 999:
            room = text
        elif seq is None and re.fullmatch(r"\d{1,3}", text) and 1 <= value <= 200:
            seq = int(value)
    nights = price = amount = None
    after_values = [value for _, _, value in after if 50 <= value <= 30000]
    try:
        computed_nights = (_parse_statement_date(end) - _parse_statement_date(start)).days
    except Exception:
        computed_nights = None
    if len(after_values) >= 3:
        first, second, third = after_values[0], after_values[1], after_values[2]
        nights = int(first) if first <= 60 and float(first).is_integer() else computed_nights
        price, amount = second, third
    elif len(after_values) == 2:
        first, second = after_values
        if first <= 60 and float(first).is_integer():
            nights, amount = int(first), second
            price = amount / nights if nights else None
        else:
            price, amount = first, second
            nights = computed_nights
    elif len(after_values) == 1:
        amount = after_values[0]
        nights = computed_nights
        price = amount / nights if nights else None
    if amount is None or not nights or nights <= 0:
        return None
    if price is None:
        return None
    if nights > 60 or not (30 <= price <= 1000) or not (30 <= amount <= 30000):
        return None
    return {
        "room": room,
        "seq": seq,
        "start": start,
        "end": end,
        "nights": int(nights),
        "price": _number_text(price),
        "amount": _number_text(amount),
    }


def extract_statement_pages(pages: list[tuple[str, dict]]) -> dict:
    """多页"酒店明细表"（无表格线版式）识别：按行聚类 + 按日期位置分左右，空白房间号沿用上一行。

    pages: [(image_path, ocr_result), ...]；返回合并后的 {rows, payment_amounts, problems, source}。
    """
    rows: list[dict] = []
    payment_amounts: list[str] = []
    problems: list[str] = []
    for image_path, ocr in pages:
        lines = [line for line in ocr.get("lines") or [] if _norm(line.get("text"))]
        if not lines:
            continue
        width = float(ocr.get("width") or max((line["bbox"][2] for line in lines), default=1) or 1)
        room_candidates: list[tuple[float, str]] = []
        for line in lines:
            text = _norm(line.get("text"))
            if not re.fullmatch(r"\d{3}[A-Za-z]?", text):
                continue
            x_center = (line["bbox"][0] + line["bbox"][2]) / 2.0
            if x_center > width * 0.4:
                continue
            if not (100 <= int(text[:3]) <= 999):
                continue
            room_candidates.append(((line["bbox"][1] + line["bbox"][3]) / 2.0, text))
        last_room = None
        for cluster in _cluster_lines_by_y(lines):
            cells = sorted(
                ((line["bbox"][0] + line["bbox"][2]) / 2.0, str(line["text"]).strip()) for line in cluster
            )
            joined = _norm("".join(text for _, text in cells))
            if "消费金额" in joined or "现金支付" in joined:
                for _, text in cells:
                    normalized = _norm(text)
                    if any(len(token) >= 6 for token in re.findall(r"\d+", normalized)):
                        continue
                    number = _parse_number(normalized)
                    if number is not None and 1000 <= number <= 1_000_000:
                        payment_amounts.append(_number_text(number))
                continue
            if "序号" in joined or "房间号" in joined or "部门" in joined or "明细表" in joined:
                continue
            parsed = _parse_statement_row(cells)
            if parsed is None:
                continue
            row_y = sum((line["bbox"][1] + line["bbox"][3]) / 2.0 for line in cluster) / len(cluster)
            if not parsed["room"] and room_candidates:
                nearest = min(room_candidates, key=lambda item: abs(item[0] - row_y))
                if abs(nearest[0] - row_y) <= 18:
                    parsed["room"] = nearest[1]
            if not parsed["room"] and last_room:
                parsed["room"] = last_room
            if parsed["room"]:
                last_room = parsed["room"]
            rows.append(
                {
                    "room": parsed["room"],
                    "start": parsed["start"],
                    "end": parsed["end"],
                    "nights": parsed["nights"],
                    "price": parsed["price"],
                    "amount": parsed["amount"],
                }
            )
    return {
        "source": [str(path) for path, _ in pages],
        "rows": rows,
        "payment_amounts": payment_amounts,
        "printed_date": None,
        "problems": problems,
    }


def _parse_statement_date(text: str):
    from datetime import date

    match = DATE_RE.search(str(text))
    if not match:
        raise ValueError(text)
    return date(2000, int(match.group(1)), int(match.group(2)))


def build_dataset(
    detail_pages: list[tuple[str, dict]],
    statement_pages: "list[tuple[str, dict]] | dict | None",
    policy: dict | None = None,
) -> dict:
    """把识别结果组装成 accommodation.check 需要的数据集（支持多页明细/多页流水）。"""
    if isinstance(statement_pages, dict):
        statement_pages = [(statement_pages.get("source", ""), statement_pages)]
    statement_pages = statement_pages or []

    if len(detail_pages) > 1:
        sheets = [extract_detail([page]) for page in detail_pages]
        rows = [row for sheet in sheets for row in (sheet.get("rows") or [])]
        subtotal_nights = sum(
            sum(int(period["nights"]) for row in (sheet.get("rows") or []) for period in row.get("periods") or [])
            for sheet in sheets
        )
        subtotal_amount = sum(
            sum(Decimal(str(period["amount"])) for row in (sheet.get("rows") or []) for period in row.get("periods") or [])
            for sheet in sheets
        )
        sheet = {
            "source": [str(path) for path, _ in detail_pages],
            "rows": rows,
            "subtotal": {"nights": subtotal_nights, "amount": str(subtotal_amount)},
            "lowercase_amount": None,
            "capital_amount": None,
            "title": sheets[0].get("title") if sheets else None,
            "department": sheets[0].get("department") if sheets else None,
            "problems": [item for sheet_item in sheets for item in sheet_item.get("problems") or []],
        }
        detail_sheets = sheets
    else:
        sheet = extract_detail(detail_pages)
        detail_sheets = None

    if len(statement_pages) > 1:
        statement_data = extract_statement_pages(statement_pages)
    elif statement_pages:
        path, ocr = statement_pages[0]
        statement_data = extract_statement(str(path), ocr)
    else:
        statement_data = {}

    year = None
    printed = statement_data.get("printed_date") or ""
    match = re.search(r"(\d{4})年", printed)
    if match:
        year = int(match.group(1))
    if year is None:
        year = int((policy or {}).get("year") or 2026)

    # 补充年份到日期（供下游核对与展示）
    for row in sheet.get("rows") or []:
        for period in row.get("periods") or []:
            for key in ("start", "end"):
                value = period.get(key)
                if value and "年" not in str(value):
                    period[key] = f"{year}年{value}"

    clean_policy = {
        key: value for key, value in (policy or {}).items()
        if key not in ("ledger", "roster", "unknown_register")
    }
    dataset = {
        "schema_version": SCHEMA,
        "template": sheet.get("title") or "施工住宿费明细表",
        "year": year,
        "department": sheet.get("department"),
        "detail_sheet": {
            "source": sheet.get("source"),
            "rows": sheet.get("rows") or [],
            "subtotal": sheet.get("subtotal") or {},
            "lowercase_amount": sheet.get("lowercase_amount"),
            "capital_amount": sheet.get("capital_amount"),
            "title": sheet.get("title"),
            "department": sheet.get("department"),
            "problems": sheet.get("problems") or [],
        },
        "hotel_statement": {
            "source": statement_data.get("source"),
            "rows": statement_data.get("rows") or [],
            "payment_amounts": statement_data.get("payment_amounts") or [],
            "printed_date": statement_data.get("printed_date"),
            "problems": statement_data.get("problems") or [],
        },
        "policy": {
            "room_capacity": 2,
            "rate": "128",
            **clean_policy,
        },
    }
    if detail_sheets:
        dataset["detail_sheets"] = [
            {
                "source": item.get("source"),
                "rows": item.get("rows") or [],
                "subtotal": item.get("subtotal") or {},
                "lowercase_amount": item.get("lowercase_amount"),
                "capital_amount": item.get("capital_amount"),
                "title": item.get("title"),
                "department": item.get("department"),
                "problems": item.get("problems") or [],
            }
            for item in detail_sheets
        ]
    return dataset


# ---------------------------------------------------------------------------
# 机关待遇/无流水模式：8 列表格（序号/入住人员/开始/结束/天数/单价/金额/签字），没有房间号
# ---------------------------------------------------------------------------

LOWERCASE_RE = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2}")


def _cell_text(lines: list[dict]) -> str:
    return _norm("".join(str(line.get("text") or "") for line in lines))


def _dept_title(lines: list[dict]) -> tuple[str | None, str | None]:
    department, title = None, None
    for line in lines:
        text = _norm(line.get("text") or "")
        if "明细表" in text and "施工" in text or "住宿费明细表" in text:
            title = text if title is None else title
    for line in lines:
        text = _norm(line.get("text") or "")
        if "部门" not in text:
            continue
        match = re.search(r"部门[:：]?(.*)", text)
        if not match:
            continue
        raw = match.group(1)
        raw = re.sub(r"[（(].*?[）)]", "", raw)
        raw = raw.replace("盖章", "").strip()
        if len(raw) >= 2:
            department = raw
            break
    return department, title


def _synth_grid_from_headers(lines: list[dict], width: float, height: float) -> dict | None:
    """网格线检测失败时（扫描件轻微倾斜/表格线太淡），用表头文字位置 + 行聚类构造列/行边界。"""
    header_candidates: list[dict] = []
    for line in lines:
        field = _match_field(line.get("text") or "", DETAIL_FIELDS)
        if field:
            header_candidates.append(line)
    if len(header_candidates) < 3:
        return None
    clusters = _cluster_lines_by_y(header_candidates, tolerance=14.0)
    best_cluster = None
    best_mapping: dict[str, dict] = {}
    for cluster in clusters:
        mapping: dict[str, dict] = {}
        for line in cluster:
            field = _match_field(line.get("text") or "", DETAIL_FIELDS)
            if field and field not in mapping:
                mapping[field] = line
        if len(mapping) >= 3 and "names" in mapping and len(mapping) > len(best_mapping):
            best_cluster, best_mapping = cluster, mapping
    if not best_cluster or "names" not in best_mapping:
        return None
    header_top = min(line["bbox"][1] for line in best_cluster)
    header_bottom = max(line["bbox"][3] for line in best_cluster)
    ordered = sorted(
        ((line["bbox"][0] + line["bbox"][2]) / 2.0, field) for field, line in best_mapping.items()
    )
    xs = [0.0]
    for index in range(len(ordered) - 1):
        xs.append((ordered[index][0] + ordered[index + 1][0]) / 2.0)
    xs.append(width)
    data_lines = [
        line for line in lines
        if (line["bbox"][1] + line["bbox"][3]) / 2.0 > header_bottom
    ]
    row_clusters = _cluster_lines_by_y(data_lines, tolerance=10.0)
    row_centers = [
        sum((line["bbox"][1] + line["bbox"][3]) / 2.0 for line in cluster) / len(cluster)
        for cluster in row_clusters
    ]
    ys = [0, max(0, header_top - 2), min(height, header_bottom + 2)]
    for index in range(len(row_centers) - 1):
        ys.append((row_centers[index] + row_centers[index + 1]) / 2.0)
    ys.append(height)
    ys = sorted({int(round(value)) for value in ys})
    return {"xs": [int(round(value)) for value in xs], "ys": ys,
            "width": int(width), "height": int(height)}


def extract_detail_no_room(pages: list[tuple[str, dict]]) -> dict:
    """识别"无房间号"的 8 列住宿明细表（机关待遇，不需要酒店流水）。

    逐页返回一个 sheet：rows/subtotal/lowercase_amount/capital_amount/title/department。
    """
    sheets = []
    problems: list[str] = []
    for image_path, ocr in pages:
        lines = list(ocr.get("lines") or [])
        width = float(ocr.get("width") or 0)
        height = float(ocr.get("height") or 0)
        grid = detect_grid(image_path)
        intervals = _intervals(grid["xs"], grid["width"]) if grid else []
        if not grid or len(intervals) < 5:
            # 扫描件轻微倾斜/表格线太淡时，网格检测会少列；用表头文字位置兜底
            synthesized = _synth_grid_from_headers(lines, width, height) if width and height else None
            if synthesized:
                grid = synthesized
        sheet = {
            "source": [str(image_path)],
            "rows": [],
            "subtotal": {},
            "lowercase_amount": None,
            "capital_amount": None,
            "title": None,
            "department": None,
            "no_room": True,
            "problems": [],
        }
        if not grid:
            sheet["problems"].append("表格网格线识别失败")
            sheets.append(sheet)
            continue
        height, width = grid["height"], grid["width"]
        bands = _bands(grid["ys"], height)
        intervals = _intervals(grid["xs"], width)
        header_index, header_map = None, {}
        for index, (y0, y1) in enumerate(bands):
            cells = _assign_columns(_lines_in_band(lines, y0, y1), intervals)
            mapping = {}
            for column, cell_lines in enumerate(cells):
                text = _cell_text(cell_lines)
                field = _match_field(text, DETAIL_FIELDS)
                if field:
                    mapping[column] = field
            if len(set(mapping.values())) >= 3 and "names" in mapping.values():
                header_index, header_map = index, mapping
                break
        if header_index is None:
            sheet["problems"].append("表头识别失败")
            sheets.append(sheet)
            continue
        column_fields = {field: column for column, field in header_map.items()}
        footer_started = False
        for y0, y1 in bands[header_index + 1:]:
            cells = _assign_columns(_lines_in_band(lines, y0, y1), intervals)
            texts = [_cell_text(cell) for cell in cells]
            combined = "".join(texts)
            if not combined:
                continue
            if "小计" in combined or "合计" in combined or "大写" in combined or "小写" in combined:
                footer_started = True
            if footer_started:
                if "小计" in combined:
                    nights_text = _cell_text(cells[column_fields["nights"]]) if "nights" in column_fields else ""
                    amount_text = _cell_text(cells[column_fields["amount"]]) if "amount" in column_fields else ""
                    sheet["subtotal"] = {
                        "nights": _parse_number(nights_text, integer=True),
                        "amount": str(_parse_number(amount_text)) if _parse_number(amount_text) is not None else None,
                    }
                continue
            names_text = texts[column_fields.get("names", 1)] if len(texts) > 1 else ""
            name_lines = cells[column_fields["names"]] if "names" in column_fields else []
            name_confidence = min((float(line.get("confidence") or 1.0) for line in name_lines), default=1.0)
            dates = []
            if "start" in column_fields:
                dates = _extract_dates(texts[column_fields["start"]])
            nights = _parse_number(_cell_text(cells[column_fields["nights"]]), integer=True) if "nights" in column_fields else None
            price = _parse_number(_cell_text(cells[column_fields["price"]])) if "price" in column_fields else None
            amount = _parse_number(_cell_text(cells[column_fields["amount"]])) if "amount" in column_fields else None
            if not dates and nights is None and amount is None:
                continue  # 空行/水印/印章残留
            if not names_text and not dates:
                continue
            names, names_uncertain = _split_names(names_text)
            if not names:
                continue
            start = dates[0] if dates else ""
            end = ""
            if "end" in column_fields:
                end_dates = _extract_dates(texts[column_fields["end"]])
                end = end_dates[0] if end_dates else ""
            if nights is None and start and end:
                try:
                    nights = (_parse_statement_date(end) - _parse_statement_date(start)).days
                except Exception:
                    nights = None
            seq_text = _cell_text(cells[column_fields["seq"]]) if "seq" in column_fields else ""
            seq_value = _parse_number(seq_text, integer=True)
            if seq_value is None:
                seq_value = len(sheet["rows"]) + 1  # 序号被水印/印章盖住时按行序补
            sheet["rows"].append(
                {
                    "seq": seq_value,
                    "room": "",
                    "names": names,
                    "names_uncertain": bool(names_uncertain or name_confidence < 0.90),
                    "periods": [
                        {
                            "start": start,
                            "end": end,
                            "nights": nights,
                            "price": str(price) if price is not None else None,
                            "amount": str(amount) if amount is not None else None,
                        }
                    ],
                    "signature": [],
                    "signature_uncertain": True,
                }
            )
        # 小写/大写金额：按"小写/大写"所在行附近找
        for line in lines:
            text = _norm(line.get("text") or "")
            if "小写" in text or "大写" in text:
                _, y0, _, y1 = line["bbox"]
                band_lines = _lines_in_band(lines, y0 - 12, y1 + 12)
                for candidate in band_lines:
                    candidate_text = str(candidate.get("text") or "")
                    if "小写" in text:
                        match = LOWERCASE_RE.search(candidate_text)
                        if match:
                            sheet["lowercase_amount"] = match.group(0).replace(",", "")
                    if "大写" in text:
                        match = CAPITAL_RE.search(candidate_text)
                        if match and len(match.group(0)) >= 4:
                            sheet["capital_amount"] = match.group(0)
        department, title = _dept_title(lines)
        sheet["department"] = department
        sheet["title"] = title or "施工住宿费明细表"
        sheets.append(sheet)
    return sheets


def build_detail_only_dataset(sheets: list[dict], policy: dict | None = None) -> dict:
    """把无房间号/无流水的明细表组装成台账数据集（detail_only 模式）。"""
    policy = policy or {}
    year = int(policy.get("year") or 2026)
    departments = [sheet.get("department") for sheet in sheets if sheet.get("department")]
    department = departments[0] if departments else policy.get("department")
    for sheet in sheets:
        for row in sheet.get("rows") or []:
            for period in row.get("periods") or []:
                for key in ("start", "end"):
                    value = period.get(key)
                    if value and "年" not in str(value):
                        period[key] = f"{year}年{value}"
        sheet["year"] = year
    # 本机路径（台账/字典/待确认名单）不写进数据集，避免数据集被分享时泄露本机配置
    clean_policy = {
        key: value for key, value in policy.items()
        if key not in ("ledger", "roster", "unknown_register")
    }
    return {
        "schema_version": "finance-accommodation/1",
        "template": (sheets[0].get("title") if sheets else None) or "施工住宿费明细表",
        "year": year,
        "department": department,
        "detail_only": True,
        "no_statement": True,
        "no_room": True,
        "detail_sheets": [
            {
                "source": sheet.get("source"),
                "rows": sheet.get("rows") or [],
                "subtotal": sheet.get("subtotal") or {},
                "lowercase_amount": sheet.get("lowercase_amount"),
                "capital_amount": sheet.get("capital_amount"),
                "title": sheet.get("title"),
                "department": sheet.get("department"),
                "problems": sheet.get("problems") or [],
            }
            for sheet in sheets
        ],
        "policy": {
            "room_capacity": 2,
            "rate": "128",
            **policy,
        },
    }
