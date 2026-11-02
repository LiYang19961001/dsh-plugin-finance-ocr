# dsh-plugin-finance-ocr — 财务图片核对插件

把**拍照 / 扫描的财务单据图片**变成**可追溯、经过守恒校验的 Excel**。

给 DSH（DeepSeek Harness）增加六个模型可见工具，全部在本机离线运行（RapidOCR + onnxruntime），
图片不上传、不联网。核心不是"识别"，而是**识别错了必须被发现**：金额全程 `Decimal`，
用单据自身的守恒关系（明细 = 合计）当校验和，对不上就报错，绝不静默放行。

- 仓库：<https://github.com/LiYang19961001/dsh-plugin-finance-ocr>
- 插件市场：`https://liyang19961001.github.io/dsh-plugin-market/index.json`（分类：财务）

```
图片 → ① finance_ocr_extract → ② finance_verify → ③ finance_export_xlsx
        （行/列/坐标/置信度）      （硬校验，fail 即拒绝）      （4 sheet 工作簿）
文件夹 → finance_batch_run（逐张跑上面三步，互不合并）
住宿费 → finance_check_accommodation（施工住宿费明细表 + 酒店流水，6 条业务规则逐条核对）
```

## 三个工具

| 工具 | 输入 | 输出 |
|---|---|---|
| `finance_ocr_extract` | 图片绝对路径 | extraction JSON：列映射、逐行字段、每格 `bbox`/`line_id`/OCR 原文/置信度、源图 SHA-256；落盘到 `%TEMP%\dsh-finance-ocr\<sha8>.extract.json` |
| `finance_verify` | extraction 路径 | `status` = `pass` / `needs_review` / `fail`，逐条 `checks`、`hard_failures`（含差额与行号）、`review_items` |
| `finance_export_xlsx` | extraction 路径 + 目标 xlsx | 数据 / 校验报告 / 问题清单 / 运行清单 四个 sheet；拒覆盖源图；校验 fail 默认拒导出 |
| `finance_batch_run` | 一个文件夹 | 逐张跑「识别→校验→导出」，每张一个 xlsx + `batch-summary.json` 汇总状态；**不同单据金额不合并** |
| `finance_check_accommodation` | 明细表图片（1~2 页）+ 酒店流水图片，或已结构化 dataset | 住宿费 6 条业务规则逐条核对：双人间/单住、同日房间人员不重复、天数金额、入住人员=签字、小计合计、流水匹配 |
| `finance_ledger` | action=list/export/archive | 住宿费核对台账：累积每次核对结果（JSONL + xlsx），可按条数/日期归档旧记录 |

典型用法（对 Agent 说）：

> 把 `D:\凭证\报销单1.png` 识别成表，校验后导出到 `D:\凭证\报销单1.xlsx`。

> 把 `D:\凭证\2026-10\` 里所有图片都跑一遍，xlsx 放到 `D:\凭证\2026-10\输出\`。

> 核对 `D:\住宿费\明细表-1.png 明细表-2.png` 和 `D:\住宿费\酒店流水.png`。

单张走 ①→②→③；文件夹走 `finance_batch_run`；住宿费走 `finance_check_accommodation`
（自动去红章、自动合并跨页和一个序号的两个日期段）。
`finance_verify` 返回 `fail` 时不要继续导出，应把差异清单交给人确认。

## 安装（DSH 插件）

1. 安装 OCR 依赖到 DSH 运行时自带的 Python（一次性，约 150 MB，之后完全离线可用）：

   ```powershell
   $py = "$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
   & $py -m pip install rapidocr_onnxruntime "opencv-python<5" -i https://pypi.tuna.tsinghua.edu.cn/simple/
   ```

2. 装插件：在 DSH 的插件管理里安装本目录（`D:\工作区\dsh-plugin-finance-ocr`），
   或用 `plugin_manager` 的 `install_bundle` 指向该目录。安装后 `finance_*` 三个工具生效。

3. 自检（把路径换成你的插件目录）：

   ```powershell
   $py -m pip -q show rapidocr_onnxruntime
   & $py "D:\工作区\dsh-plugin-finance-ocr\lib\cli.py" selftest --input selftest.in.json --output selftest.json
   ```

   `selftest.in.json` 内容写 `{}` 即可；结果里 `ok: true` 表示 OCR 引擎、openpyxl、中文字体都就位。

## 校验规则（核心，不许省）

| 规则 | 级别 |
|---|---|
| 明细金额合计 = 票面合计（每个金额列，容差默认 0.01，即**1 分钱差异必抓**） | 硬失败 |
| 行数守恒：每个 OCR 行都有明确去向（明细/合计/跳过并注明原因），无遗漏无重复 | 硬失败 |
| 金额可解析（全角、`￥`、千分位、括号负数先清洗；解析不了绝不猜） | 硬失败 |
| 长编号保护：发票号码/凭证号等被转科学计数法或丢前导 0 | 硬失败 |
| 源图哈希校验：读取前后 SHA-256 必须一致 | 硬失败 |
| 重复行：同编号 + 同金额重复计入 | 硬失败 |
| 低置信度（默认 < 0.90）、金额×税率≠税额、数量×单价≠金额、缺字段、日期格式 | 待人工确认 |
| 没有合计行可校验 | 待人工确认（不能算 pass） |

金额一律 `Decimal`（`HALF_UP`, 2 位），不出现浮点。

## 输出工作簿

- **数据**：规范字段 + 溯源列（`行类型`/`行号`/原图/bbox/OCR 原文/置信度）；明细行在前，合计行在后，
  合计行 `行类型`=合计。金额是数值格式，编号是文本格式（`@`），不会被 Excel 转成科学计数法。
- **校验报告**：每条检查的通过/失败/待确认与人类可读说明，明细合计 vs 票面合计对照。
- **问题清单**：全部硬失败与待确认项（含差额、行号、原始识别文本）。
- **运行清单**：输入路径与 SHA-256、OCR 引擎及版本、容差、输出 SHA-256、插件版本。

## 住宿费核对（finance_check_accommodation，v0.2.0）

针对固定版式的「施工住宿费明细表（1~2 页）+ 酒店流水（1 页）」，按 6 条业务规则逐条核对：

| # | 规则 | 说明 |
|---|---|---|
| 1 | 双人间 / 单住 | 除**正主任**外必须双人间（**副主任不能单住**）；单住只允许"当天总人数为奇数（凑不够）"或住客是正主任 |
| 2 | 同日不重复 | 同一天不能出现重复房间，也不能一人同时住两间 |
| 3 | 天数金额 | 住宿天数 = 结束 − 开始；住宿金额 = 天数 × 单价 |
| 4 | 人员=签字 | 入住人员姓名必须与签字确认是同一组人（顺序不限） |
| 5 | 小计合计 | 明细小计（天数/金额）与合计小写、大写正确 |
| 6 | 流水匹配 | 酒店流水逐房间、逐日期段、逐金额匹配，支付金额 = 明细合计 |

- 识别：自动**去红章**后再 OCR；明细表用表格线定位行列（补助金额/签字列不会被相邻列抢走），
  一个序号两个日期段自动合并，跨 2 页自动拼接；流水按表头定列、按序号/房间号锚点定记录。
- 判定：结构性错误（天数对不上、金额对不上、小计错、流水对不上、单住违规、重复入住）→ **fail**；
  手写签字无法机读、红章遮挡房间号 → **needs_review**（待人工确认，不硬报错）；
  单住房晚清单、房间中途空档（退房）→ 提示。
- 车间主任名单、单价等存在本机策略文件 `%USERPROFILE%\.dsh\finance-ocr\accommodation-policy.json`，
  会按车间区分（C车间的同名人员不算B车间的车间主任）。
- 也可以传 `dataset`（已结构化 JSON）只跑核对；识别结果用 `save_dataset` 留档，人工修正后可复跑。
- **多住宿地点**：同一批报销里如果不同人住在不同酒店，用 `pairs` 一次传多组
  （每组 = 该地点的明细图 + 流水图），各自核对后汇总总天数/总金额；组号/地点名可自定。
- 报销频率不固定也可以：每次按批次提交、按住宿地点分组即可，不依赖"每月一次"。
- **台账**：核对完成后自动往台账追一行（批次、地点、期间、人数、天数、金额、结论、待确认数），
  JSONL 是源、同名 xlsx 是人看版；台账路径在策略文件里配置。记录多了用 `finance_ledger` 的 `archive`
  按条数/日期归档，避免无限膨胀。
- **人员字典（v0.2.3+）**：策略文件里配 `roster`（人员名单解析出的字典），核对时会检查姓名：
  OCR 不确定的行给出"疑似误识"建议、名单外人员列出提示并记入 `unknown_register` 待确认名单、
  其他部门人员标注可能借调、已登记的外包/临时人员单独提示；
  **姓名以住宿明细表（表格）为准，工具只提示不自动改名**。轨道车司机、设计室借调人员按共享人员处理。
- **机关待遇/不需要流水的部门（v0.2.5）**：传 `no_statement=true`，只核对明细表（可多张），
  自动跳过"酒店流水匹配"与"双人间/单住"（表格没有房间号）；核对天数/金额/小计/大写/同一人同日不重复/签字，
  签字人工核对后可传 `signature_verified=true`。
- 一张明细表对应**一个住宿地点**（用户口径），pairs 模式用于"一批报销含多个地点"的情况。
- 一张明细表对应**一个住宿地点**（用户口径），pairs 模式用于"一批报销含多个地点"的情况。

## 明确不做

- 不生成凭证、不记账、不过账；
- 不出税务/会计处理结论；
- 不自动确认有歧义的匹配（差额只分级，不替你定性）；
- 不联网、不上传原始凭证。

## 已知限制（初版）

- `auto` 模板按**中文表头关键词**识别列；表头缺失或版式特殊的单据需要校准词表（`lib/table_extract.py` 顶部的
  `FIELD_KEYWORDS`），或先用真实样图跑一遍看 `skipped_lines`。
- 识别不到表头/明细时 `finance_ocr_extract` 直接返回 `ok=false, code=NO_HEADER`（不会拿空表去导出一个"看起来正常"的 xlsx）。
- 主要面向**规整截图/扫描件**；严重倾斜、反光、手写体、多张票据拼一页的图，识别率会下降（仍会报低置信度，不会静默）。
- 批量目前是**逐张处理**：一个文件夹多张图可以，但"一张图里拼了多张票据"的自动切分还没做（需要真实样本再设计）。
- 住宿费通道的**手写签字无法机读**：规则 4 只能做到"系统提示需人工核对"，无法替代人工看签名。
- 住宿费通道的识别依赖固定版式；红章压在序号/房间号上时，房间号会识别不清（会用日期段+金额反查并提示人工确认）；
  姓名印刷体也会有个别字识别错误，`names_uncertain` 会标出，报告里以人工核对为准。
- 无独立 UI 页面；能力通过 Agent 工具暴露。

## Python 解释器

默认按 `%DSH_HOME%\dsh-runtimes\*\dependencies\python\python.exe` → `PATH` 的顺序查找；
需要指定别的解释器时设置环境变量 `DSH_FINANCE_PYTHON` 指向 `python.exe`。

## 验收（可复现）

```powershell
$py = "$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py tests\gen_sample.py          # 生成合成样图 samples\sample_reimbursement.png
& $py tests\test_pipeline.py       # 端到端：识别→行数守恒→校验→改坏1分钱必报错→导出→确定性→拒绝覆盖
& $py -m unittest tests.test_finance_core -v
& $py tests\test_accommodation.py  # 住宿费 6 条规则：单住/重复/天数/签字/小计/流水匹配
```

## 相关文件

| 文件 | 作用 |
|---|---|
| `lib/CONTRACT.md` | Node ↔ Python 接口契约（envelope / extraction / verify / export 文档） |
| `lib/cli.py` | Python 侧唯一入口（Node 只调它） |
| `lib/finance_core.py` | 金额清洗、`Decimal`、全部校验 |
| `lib/xlsx_export.py` | 4 sheet 工作簿导出 |
| `lib/ocr_engine.py` | RapidOCR 封装（保留 bbox，映射回原图坐标） |
| `lib/table_extract.py` | 表头识别、列映射、行聚合、溯源单元格 |
| `lib/accommodation.py` | 住宿费 6 条业务规则（纯 stdlib，可单独单测） |
| `lib/accommodation_ocr.py` | 施工住宿费明细表/酒店流水固定版式识别（表格线+锚点+跨页合并） |
| `lib/ledger.py` | 住宿费核对台账（JSONL + xlsx，去重/导出/归档） |
| `index.js` | Cordis Host 插件：注册五个工具、spawn Python |
| `%USERPROFILE%\.dsh\finance-ocr\accommodation-policy.json` | 本机策略：车间主任名单、单价（不随插件发布） |
| `REQUIREMENTS.md` | 最初的需求备忘 |

MIT License.
