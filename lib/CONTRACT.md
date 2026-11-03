# 内部接口契约（Node ↔ Python，以及模块之间）

> 版本：v0.1（2026-11-02）
> 所有金额一律 **字符串 + Decimal**，不许出现 float。所有输出 JSON 必须可 `json.dumps(..., ensure_ascii=False, sort_keys=False)` 往返。

## 1. 目录约定（谁写哪个文件）

| 文件 | 负责人 | 职责 |
|---|---|---|
| `lib/finance_core.py` | verify-core | 金额清洗/Decimal 量化、结构化结果读取、第 4 节全部硬校验 |
| `lib/xlsx_export.py` | verify-core | 出 xlsx（4 个 sheet）+ 运行清单 |
| `lib/ocr_engine.py` | ocr-lab | RapidOCR 封装：图片 → 文本行（含 bbox/置信度） |
| `lib/table_extract.py` | ocr-lab | 文本行 → 表头识别、列映射、行聚合、溯源单元格 |
| `lib/cli.py` | Lead | 三个子命令的统一入口，Node 只调它 |
| `index.js` / `package.json` / `cordis.patch.yml` / `locale/` / `icon.svg` | Lead | DSH 插件壳 |
| `tests/test_finance_core.py` | verify-core | 校验层单测（含"人为改坏合计必须报错"） |
| `tests/gen_sample.py` | ocr-lab | 生成合成样图（PIL 渲染一张中文财务表格） |
| `tests/test_pipeline.py` | Lead | 端到端 + 确定性 + 溯源 + 拒绝覆盖源文件 |

## 2. 进程调用约定（Node 侧唯一方式）

```
python.exe lib/cli.py <command> --input <input.json> --output <output.json>
```

- `command ∈ {ocr_extract, verify, export_xlsx, selftest}`。
- 不读 stdin、不靠 stdout 传数据：输入输出都走**文件**（Node 用 `spawn` + `stdio: 'ignore'`，然后读 output 文件）。
- 无论成败，`--output` 指定的文件**必须**写出一个 JSON envelope（见下）。异常时也要写。
- 进程退出码：`0` 成功；`2` 参数/IO 错误；`3` 业务失败（如校验不过）。Node 以 JSON 的 `ok/status` 为准，退出码只作日志。
- stdout/stderr 只放人类可读日志，Node 不解析。

### envelope 公共字段

```json
{ "ok": true, "command": "verify", "plugin_version": "0.1.0", "error": null }
```

失败时 `ok=false`、`error={"code":"...","message":"..."}`。

## 3. extraction 文档（`ocr_extract` 的产物，`verify`/`export_xlsx` 的输入）

落盘到 `%TEMP%\dsh-finance-ocr\<sha8>.extract.json`（sha8 = 源图 SHA-256 前 8 位），
同一张图重复跑会产生同名文件（内容必须一致）。

```json
{
  "schema_version": "finance-extract/1",
  "plugin_version": "0.1.0",
  "source": {
    "path": "D:\\...\\sample.png",
    "sha256": "64位小写hex",
    "bytes": 123456,
    "width": 1600,
    "height": 1200,
    "mtime": "2026-11-02T09:00:00+08:00"
  },
  "template": "auto",
  "ocr": {
    "engine": "rapidocr_onnxruntime",
    "engine_version": "1.4.4",
    "n_lines": 40,
    "mean_confidence": 0.9731
  },
  "columns": [
    { "field": "seq", "header": "序号", "x_min": 40, "x_max": 120, "align": "right" }
  ],
  "header_line": 3,
  "rows": [
    {
      "row_index": 1,
      "y_center": 512,
      "cells": {
        "seq":      { "raw": "1", "value": "1", "confidence": 0.99, "bbox": [40, 500, 118, 526], "line_id": 7 },
        "summary":  { "raw": "办公用品", "value": "办公用品", "confidence": 0.97, "bbox": [130, 500, 420, 526], "line_id": 8 },
        "amount":   { "raw": "￥1,234.50", "value": "1234.50", "confidence": 0.95, "bbox": [900, 500, 1080, 526], "line_id": 9 }
      }
    }
  ],
  "total_rows": [
    {
      "label": "合计",
      "y_center": 780,
      "cells": { "amount": { "raw": "3,200.00", "value": "3200.00", "confidence": 0.98, "bbox": [900, 766, 1080, 792], "line_id": 20 } }
    }
  ],
  "skipped_lines": [
    { "text": "XX公司报销单", "bbox": [300, 40, 900, 80], "line_id": 1, "reason": "title" }
  ],
  "warnings": []
}
```

- `value`：清洗后的**规范文本**。金额类字段是纯十进制字符串（无货币符号/千分位，`-` 表负），不做舍入（舍入在 verify/export 按 `tolerance` 处理）；编号类字段保留原始字符（前导 0、长数字、`-` 分隔）。
- `raw`：OCR 原文，供人工复核与溯源，**不许在 verify 里"顺便修复"**。
- `bbox`：`[x1, y1, x2, y2]`，图片像素坐标（原图，非缩放图）。
- `line_id`：OCR 行号（0 起，按 (y, x) 排序后的稳定序号），用于追回原图位置。
- `skipped_lines.reason ∈ {title, above_header, header, total, noise, unmapped, low_confidence}`。
- 行数守恒（**按 line_id 覆盖集合**，不是简单相加）：`rows`/`total_rows` 的每个单元格、`skipped_lines` 的每条记录所引用的 `line_id` 并集，必须覆盖 `ocr.n_lines` 中的每一行（一个 OCR 行被拆进多个单元格时只算一次）。表头行逐行进 `skipped_lines`（reason=header）供 verify 复算。
- 识别不到表头或明细（`columns` 为空 / `rows` 为空）时，`ocr_extract` 必须返回 `ok=false, error.code=NO_HEADER`，不得把空表交给下游导出。

## 4. verify 文档（`verify` 的产物）

```json
{
  "schema_version": "finance-verify/1",
  "status": "pass",
  "hard_failures": [
    { "code": "TOTAL_MISMATCH", "message": "金额合计不符：明细合计 3210.00，票面合计 3200.00，差额 10.00", "field": "amount", "expected": "3200.00", "actual": "3210.00", "delta": "10.00", "rows": [1,2,3] }
  ],
  "review_items": [
    { "code": "LOW_CONFIDENCE", "message": "...", "row_index": 2, "field": "amount", "raw": "1,23A.50", "confidence": 0.61 }
  ],
  "checks": [
    { "id": "detail_sum_equals_total", "name": "明细合计 = 表内合计", "status": "pass", "detail": "金额 3200.00 == 3200.00" }
  ],
  "summary": { "n_rows": 3, "n_total_rows": 1, "n_amount_fields": 1, "n_hard_failures": 0, "n_review_items": 0 },
  "source": { "path": "...", "sha256": "...", "sha256_now": "..." }
}
```

- `status` 三态：`pass`（全部通过）、`needs_review`（无硬失败但有待人工确认项）、`fail`（有硬失败）。
- 金额比较规则：`|差额| >= tolerance` 即失败（默认 `"0.01"`，即金额常见 2 位小数下**任何一分钱差异都会被抓到**；传 `"0.00"` 要求完全相等）。`TOTAL_MISMATCH`、`TAX_MISMATCH`、`QTY_PRICE_MISMATCH` 同一规则。
- 硬失败至少覆盖：`TOTAL_MISMATCH`、`UNPARSEABLE_AMOUNT`、`ROW_ACCOUNTING`（行数不守恒）、`DANGEROUS_NUMBER`（科学计数法/前导 0 丢失风险）、`SOURCE_HASH_CHANGED`、`NO_AMOUNT_COLUMN`、`DUPLICATE_ROW`（同编号同金额重复计入，按硬失败处理）。
- 待确认项至少覆盖：`LOW_CONFIDENCE`、`TAX_MISMATCH`（金额×税率≠税额，容差 0.01 起）、`QTY_PRICE_MISMATCH`、`MISSING_REQUIRED_FIELD`、`DATE_FORMAT`、`TOTAL_ROW_MISSING`（无合计行可校验 → 不能算 pass，算 needs_review）。
- 金额容差用 `Decimal`，参数 `tolerance` 默认 `"0.01"`；`TOTAL_MISMATCH` 也是这个容差（想严格可传 `"0.00"`）。

## 5. export 文档（`export_xlsx` 的产物）

```json
{
  "ok": true,
  "path": "D:\\...\\out.xlsx",
  "sha256": "...",
  "sheets": [ { "name": "数据", "rows": 5 }, { "name": "校验报告", "rows": 8 }, { "name": "问题清单", "rows": 0 }, { "name": "运行清单", "rows": 12 } ],
  "verification": { "status": "pass", "n_hard_failures": 0, "n_review_items": 0 },
  "run": {
    "plugin_version": "0.1.0",
    "engine": "rapidocr_onnxruntime 1.4.4",
    "input_path": "...", "input_sha256": "...",
    "output_sha256": "...",
    "tolerance": "0.01"
  }
}
```

- 拒绝条件（`ok=false`, `exit 3`）：`output` 与 `source.path` 相同、`output` 覆盖提取 JSON、校验 `status=fail`（除非 `allow_unverified=true`）、输出目录不存在或不可写。
- 函数签名：`export_xlsx(extraction, verification, output_path, run_meta=None, allow_unverified=False)`。
- xlsx 必须 4 个 sheet：`数据`（规范字段 + 溯源列：行类型/行号/原图/bbox/OCR 原文/置信度；**合计行也写入**，`行类型`=合计、行号列写 label，明细行 `行类型`=明细）、`校验报告`（checks 全量 + 明细合计 vs 票面合计对照）、`问题清单`（hard_failures + review_items）、`运行清单`（run 全量）。
- 编号类字段（invoice_no/voucher_no/account）以**文本**写入（单元格 `number_format='@'`），禁止被 Excel 转科学计数法。
- 确定性：同一 extraction 文档两次导出，`数据`等 sheet 的单元格值完全一致。工作簿创建/修改时间取 `source.mtime`，不取当前时间。

## 6. 错误码（`error.code`）

`BAD_INPUT` / `NO_OCR_ENGINE` / `OCR_FAILED` / `EXTRACTION_NOT_FOUND` / `SCHEMA_MISMATCH` / `OUTPUT_REFUSED` / `IO_ERROR`。

## 7. 自检命令

`python lib/cli.py selftest --input <任意> --output <out.json>` 返回 `{ok, python, engine, engine_version, package_dir, checks:[...]}`，供安装后验证环境。

## 8. batch 文档（批量处理一个目录）

命令：`python lib/cli.py batch --input <in.json> --output <out.json>`。

输入：

```json
{
  "directory": "D:\\凭证\\2026-10",
  "output_dir": "D:\\凭证\\2026-10\\finance-ocr-output",
  "pattern": "*",
  "tolerance": "0.01",
  "min_confidence": 0.5,
  "allow_unverified": false,
  "max_files": 200
}
```

- `directory` 必填、绝对路径；只扫当前层（不递归）；支持的扩展名 png/jpg/jpeg/bmp/webp。
- `output_dir` 默认 `<directory>\finance-ocr-output`，不存在会创建。
- 每张图独立执行「extraction → verify → export」，**不同单据的金额不合并**。

输出 envelope 关键字段：

```json
{
  "ok": true,
  "command": "batch",
  "n_total": 3,
  "n_truncated": false,
  "n_exported": 2,
  "counts": {"pass": 2, "needs_review": 0, "fail": 0, "no_table": 1, "error": 0},
  "summary_path": "<output_dir>\\batch-summary.json",
  "results": [
    {"file": "a.png", "image": "...", "status": "pass", "n_rows": 5, "n_total_rows": 1,
     "hard_failures": [], "review_items": 0, "output": "...a.xlsx", "output_sha256": "...",
     "extraction": "...", "source_sha256": "...", "error": null}
  ]
}
```

- `status ∈ {pass, needs_review, fail, no_table, error}`；`no_table` 表示 NO_HEADER（未导出），`fail` 表示硬校验不过且未授权导出。
- 单张图失败（异常）只记该图 `status=error`，不中断整批；只有目录/IO 级错误才 `ok=false`。
- 结果按文件名排序，无时间戳字段，同一目录两次批处理结果一致。
- 同名不同图时输出名追加源图哈希前缀，绝不互相覆盖。

## 9. accommodation 文档（住宿费核对，v0.2.0）

命令：`python lib/cli.py accommodation --input <in.json> --output <out.json>`。
两种输入模式：

```json
{
  "detail_images": ["D:\\...\\明细表-1.png", "D:\\...\\明细表-2.png"],
  "statement_image": "D:\\...\\酒店流水.png",
  "year": 2026,
  "department": "某段B车间",
  "directors": "林七,何八,罗九,赵六,王五,高十,张三",
  "rate": "128",
  "save_dataset": "D:\\...\\自动识别.json"
}
```

或跳过识别、只跑核对：

```json
{ "dataset": "D:\\...\\核对数据.json" }
```

- 图片模式：先做**去红章预处理**，再用 `accommodation_ocr` 按固定版式识别
  （明细表用 OpenCV 检测表格线定位行列；一个序号两个日期段自动合并；跨 2 页自动拼接；酒店流水按表头定列、按序号/房间号锚点定记录），
  然后立即跑规则；`save_dataset` 可把识别结果留档，人工修正后可用 dataset 模式复跑。
- **多住宿地点模式**（同一批报销里不同酒店）：`pairs` 传数组，每组用自己的明细+流水独立核对，再汇总：

```json
{
  "pairs": [
    {"label": "第一住宿地点", "detail_images": ["D:\\...\\明细1.png", "D:\\...\\明细2.png"],
     "statement_image": "D:\\...\\流水1.png", "save_dataset": "D:\\...\\地点1.json"},
    {"label": "第二住宿地点", "detail_images": ["D:\\...\\明细3.png"],
     "statement_image": "D:\\...\\流水2.png"}
  ],
  "year": 2026
}
```

  输出 `{mode:"pairs", n_pairs, pairs:[{label,status,ok,dataset_path,summary,errors,warnings,notes,rules}], aggregate:{n_pairs,counts,total_nights,total_amount}}`；
  单个地点失败只记该地点 `status=fail`，不中断整批；全部地点 `ok` 才算 `ok=true`。
- `directors` / `rate` / `year` / `department` 不传时读取本机策略文件
  `%USERPROFILE%\.dsh\finance-ocr\accommodation-policy.json`。
- **台账**：策略文件里的 `ledger`（或 payload 的 `ledger`）是台账 JSONL 路径；给定时每次核对会自动追加/更新一行台账，
  并重新生成同名 xlsx（人看版）。批次名取 `batch` 参数，缺省按最早住宿日期推成 `YYYY-MM`；地点名取 `label`，
  单组缺省"住宿地点"、pairs 模式用每组 label。去重键 = (批次, 地点, 流水文件名, 期间起, 期间止, 金额)，
  同一批重复核对只更新状态不重复记账。envelope 里返回 `ledger:{ledger,xlsx,n_added,n_updated,n_total,entries}`。
- 识别不确定性（手写签字、印章遮挡导致房间号未识别）**降级为待人工确认**，不当作硬失败；
  被遮挡的流水房间号会用"日期段+天数+金额"反查明细并提示。
- 输出 envelope：`{ok, command:"accommodation", status: pass|needs_review|fail, dataset_path, dataset, report:{...}}`。
  `report.status = fail` 时 `ok=false`、退出码 3。
- `report` 字段：`errors`（硬失败，含 rule/message）、`warnings`（待人工确认）、`notes`（提示：
  单住房晚清单、房间空档）、`rules`（6 条规则状态）、`summary`、`single_rooms`、`room_gaps`、`per_day`。

数据集（dataset）结构：

```json
{
  "schema_version": "finance-accommodation/1",
  "year": 2026,
  "department": "某段B车间",
  "policy": {"room_capacity": 2, "rate": "128", "directors": ["林七"]},
  "detail_sheet": {
    "rows": [
      {"seq": 1, "room": "301", "names": ["张三", "李四"],
       "periods": [{"start": "10月10日", "end": "10月23日", "nights": 13, "price": "128", "amount": "1664"}],
       "signature": ["张三", "李四"], "signature_uncertain": false}
    ],
    "subtotal": {"nights": 161, "amount": "20608"},
    "lowercase_amount": "20608.00",
    "capital_amount": "贰万零陆佰零捌元整"
  },
  "hotel_statement": {
    "rows": [{"room": "301", "start": "10月10日", "end": "10月23日", "nights": 13, "price": "128", "amount": "1664"}],
    "payment_amounts": ["20608", "20608"],
    "printed_date": "2026年10月23日"
  }
}
```

## 10. accommodation 规则（业务口径，2026-11-02 用户确认）

1. 除**正主任**外必须双人间；**副主任目前不能单住**；其他人的单住规则：**同一天除主任外最多只允许 1 间单住**（凑不够），出现 2 间及以上单人间即不通过（不再按当天总人数奇偶判断）。
2. 同一天内不能重复房间、不能一人同时住两间。
3. 住宿天数 = 结束日期 − 开始日期；住宿金额 = 住宿天数 × 单价。
4. 住宿单价不得超上限：**车间住宿费每日最高 130 元/人**，**机关（含机关待遇部门）每日最高 200 元/人**；超过即硬失败（`RATE_OVER_LIMIT`）。
5. 入住人员姓名必须与签字确认是同一组人（顺序不限）；手写签字无法机读时列待人工确认。
6. 明细小计（天数、金额）与合计（小写、大写）必须正确。
7. 酒店流水必须与明细逐房间、逐日期段匹配（天数/单价/金额），支付金额等于明细合计。

## 11. ledger 文档（住宿费核对台账，v0.2.2）

命令：`python lib/cli.py ledger --input <in.json> --output <out.json>`。

```json
{"action": "list",   "ledger": "D:\\...\\住宿费核对台账.jsonl"}
{"action": "export", "ledger": "...", "output": "D:\\...\\台账.xlsx"}
{"action": "archive","ledger": "...", "keep_last": 20}
{"action": "archive","ledger": "...", "before": "2025-01-01", "archive_dir": "D:\\...\\归档"}
```

- `ledger` 不传时读策略文件的 `ledger`。
- `list`：返回 `{n_total, total_nights, total_amount, entries:[...]}`。
- `export`：从 JSONL 重新生成 xlsx（字段：记录时间/批次/部门/住宿地点/期间/人数/日期段数/住宿天数/住宿金额/核对结论/错误数/待确认数/数据文件/备注；末行合计）。
- `archive`：`keep_last` 保留最近 N 条或 `before` 保留该日期之后的记录，其余移到 `台账-归档-<时间>.jsonl`；
  台账文件不会无限膨胀，内存/体积可控。

## 12. roster 文档（人员字典 / 姓名检查，v0.2.4）

- 人员字典路径来自策略文件 `roster`（或 payload 的 `roster`），指向 `人员字典-2026.9.json`。
- 字典结构：`departments{部门:{names[],directors[{name,post}]}}`、`shared_names[]`（轨道车司机 + 设计室借调人员）、
  `outsourced_names[]`（外包/临时人员，变动频繁，按表格保留）、`director_whitelist[]`（正主任白名单）、
  `name_index{name:[部门]}`。
- 姓名规则：**姓名以住宿明细表（表格）为准**，字典只提示、不自动改名：
  - 只在 `names_uncertain=true` 的行给 "疑似 OCR 误识" 建议（`name_match.suggestions`）；
  - 不在部门名单/共享名单/外包名单的人：`unknown`（提示人工确认身份）；
  - 在其他部门名单里出现的人：`cross_department`（可能是借调，按表格保留）；
  - 已登记的外包/临时人员：`outsourced`（单独提示）。
- `name_match` 会写进数据集和命令信封，并在校验报告的 `notes` 里生成 `NAME_SUGGESTION` /
  `NAME_CROSS_DEPARTMENT` / `NAME_NOT_IN_ROSTER` / `NAME_OUTSOURCED` 提示；**不影响规则结论（不改 status）**。
- 字典查不到的姓名会追加到策略文件 `unknown_register` 指向的"待确认名单"（JSONL，按姓名累计出现批次/次数），
  信封里返回 `unknown_register:{register,n_total,added,pending}`；确认身份后补进字典即可。

## 13. detail_only 模式（机关待遇，无酒店流水，v0.2.5）

用于**享受机关待遇、不需要酒店流水**的部门（如设计室）。明细表没有房间号列，
列：序号/入住人员/开始日期/结束日期/住宿天数/单价/住宿金额/签字确认。

- payload：`detail_images`（可多张，逗号分隔）+ `no_statement: true`；或直接 `dataset`（`detail_only: true`）。
- 数据集结构：

```json
{
  "schema_version": "finance-accommodation/1",
  "detail_only": true,
  "no_statement": true,
  "no_room": true,
  "year": 2026,
  "department": "某段设计室",
  "signature_verified": true,
  "detail_sheets": [
    {"rows": [{"seq": 1, "names": ["张三"], "periods": [{"start": "10月10日", "end": "10月14日",
      "nights": 4, "price": "142", "amount": "568"}], "signature": ["张三"]}],
     "subtotal": {"nights": 4, "amount": "568"}, "lowercase_amount": "568.00",
     "capital_amount": "伍佰陆拾捌元整"}
  ]
}
```

- 核对规则：每行天数=结束-开始、金额=天数×单价；单价≤机关上限 200 元/人/天；每张表小计/合计小写/大写正确；
  同一人同一天只能出现一次（跨表也查）；入住人员=签字（手写只能人工核对，`signature_verified` 表示已人工核对）。
- 跳过：双人间/单住（无房间号）、酒店流水匹配（机关待遇不核流水），`rules` 里记为 `skip`。
- 报告里会提示单价档位（如 142/180 元/天）与常规标准的差异，供人工确认。

## 14. 多页 / 9 列版式（v0.2.9）

- **明细表 1~3 页**：逐页识别后，`detail_sheets` 保留每页的 rows/subtotal/lowercase_amount/capital_amount；
  `detail_sheet` 仍是合并后的全部 rows + 合计。check 会**逐页**核对小计/小写/大写（错误信息带"第 N 页"），
  再对合并后的总额、天数做整体核对。
- **9 列版式**（序号/房号/入住人员/开始/结束/住宿天数/单价/住宿金额/签字确认，无补助列）：
  表头"序号房号"被 OCR 合并、房号单元格纵向合并时，会按版式位置/相邻日期段/同组人补全房号。
- **流水 1~2 页**：同一住宿地点的多页流水（同版式）会自动合并，`payment_amounts` 从末页读取；
  续行没有房号时按邻近/上一行补全。流水与明细仍按（房间号 + 起止日期）逐段匹配。
- `statement_image` 支持逗号分隔多页；`detail_images` 支持 1~3 页。手写签字人工核对后可传
  `signature_verified=true`：签字规则记为已核对（报告里 `SIGNATURE_VERIFIED` 提示），不再进待确认。

## 15. 图片型 PDF 输入（v0.2.13）

- `detail_images` / `statement_image` 除了图片，也接受 `.pdf`（绝对路径，多个用逗号分隔，可与图片混用）。
- PDF 按 **250 DPI** 逐页渲染为 PNG（RGB），页序即 PDF 页序；渲染缓存目录
  `%USERPROFILE%\.dsh\finance-ocr\pdf-cache\<文件sha256前12位>-<dpi>\page-001.png`，同一文件不重复渲染。
- 明细 PDF 的页数就是明细页数，流水 PDF 的页数就是流水页数；用户需说明哪份/哪几页是明细、哪份是流水
  （或分成两个 PDF 分别传）。
- 限制与提示：加密 PDF 返回 `PDF_ENCRYPTED`；非图片型（纯文字导出）PDF 版式可能不同，需要样张验证；
  渲染 DPI 可在 payload 里用 `pdf_dpi` 覆盖（默认 250，扫描件建议 ≥200）。
- 数据安全：PDF 与渲染出的 PNG 都只在本机，不上传、不进公开仓库。

