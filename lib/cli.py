#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-plugin-finance-ocr 的 Python 侧唯一入口。

Node 只按下面这一种方式调用本文件，数据全部走文件，不依赖 stdin/stdout：

    python.exe lib/cli.py <command> --input <input.json> --output <output.json>

command:
    ocr_extract   图片 -> extraction 文档（OCR + 表格结构还原）
    verify        extraction 文档 -> 校验报告（只用 Decimal，硬校验不放行）
    export_xlsx   extraction 文档 + 校验报告 -> 4 sheet 工作簿
    selftest      环境自检

契约见 lib/CONTRACT.md。无论成败，--output 都会写出一个 JSON envelope。
退出码：0 成功 / 2 参数或 IO 错误 / 3 业务失败（校验不过、导出被拒绝）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

PLUGIN_VERSION = "0.2.7"
SCHEMA_EXTRACT = "finance-extract/1"
SCHEMA_VERIFY = "finance-verify/1"
HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

WORK_ROOT = Path(tempfile.gettempdir()) / "dsh-finance-ocr"


# ---------------------------------------------------------------- 小工具

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    os.close(handle)
    try:
        with open(tmp_name, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def envelope(command: str, ok: bool, **extra) -> dict:
    doc = {
        "ok": ok,
        "command": command,
        "plugin_version": PLUGIN_VERSION,
        "error": None,
    }
    doc.update(extra)
    return doc


def failure(command: str, code: str, message: str) -> dict:
    return envelope(command, False, error={"code": code, "message": message})


def load_json(path: Path) -> dict:
    try:
        # utf-8-sig：容忍 Windows 工具写出的 BOM，避免把合法输入判成坏 JSON
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except FileNotFoundError:
        raise CliError("EXTRACTION_NOT_FOUND", f"文件不存在：{path}")
    except json.JSONDecodeError as error:
        raise CliError("BAD_INPUT", f"不是合法 JSON：{path}（{error}）")


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def require(payload: dict, key: str) -> object:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise CliError("BAD_INPUT", f"缺少必填参数 `{key}`")
    return value


def import_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise CliError(
            "INTERNAL_ERROR",
            f"插件内部模块 {module_name} 导入失败：{error}",
            3,
        )


def pick_function(module, names, label):
    for name in names:
        attribute = getattr(module, name, None)
        if callable(attribute):
            return attribute
    raise CliError(
        "INTERNAL_ERROR",
        f"{label} 缺少实现函数（期望其中之一：{', '.join(names)}）",
        3,
    )


# ---------------------------------------------------------------- 源文件与缓存

def source_meta(image_path: Path) -> dict:
    from PIL import Image

    stat = image_path.stat()
    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except Exception as error:  # noqa: BLE001 - 图片读不开就是输入错误
        raise CliError("BAD_INPUT", f"无法打开图片 {image_path}：{error}")
    return {
        "path": str(image_path),
        "sha256": sha256_file(image_path),
        "bytes": stat.st_size,
        "width": int(width),
        "height": int(height),
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def extraction_cache_path(source: dict) -> Path:
    return WORK_ROOT / f"{source['sha256'][:8]}.extract.json"


# ---------------------------------------------------------------- 各子命令

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def pipeline_modules() -> dict:
    """一次性取齐四项能力，ocr_extract 与 batch 共用。"""
    ocr_engine = import_module("ocr_engine")
    table_extract = import_module("table_extract")
    finance_core = import_module("finance_core")
    xlsx_export = import_module("xlsx_export")
    return {
        "run_ocr": pick_function(ocr_engine, ["run_ocr", "ocr_image", "recognize"], "ocr_engine"),
        "build_extraction": pick_function(
            table_extract, ["build_extraction", "extract_table", "build"], "table_extract"
        ),
        "verify": pick_function(finance_core, ["verify", "verify_extraction"], "finance_core"),
        "export_xlsx": pick_function(xlsx_export, ["export_xlsx", "export"], "xlsx_export"),
    }


def run_extraction(image: Path, template: str, min_confidence: float,
                   modules: dict | None = None) -> tuple[dict, dict, Path]:
    """图片 -> （规范化后的 extraction 文档, ocr 结果, 缓存路径）。只做识别，不做校验/导出。"""
    modules = modules or pipeline_modules()
    source = source_meta(image)
    ocr_result = modules["run_ocr"](str(image))
    extraction = modules["build_extraction"](
        ocr_result, source, template=template, min_confidence=min_confidence
    )

    # 统一补齐契约要求的公共字段；子模块给出的值优先。
    extraction.setdefault("schema_version", SCHEMA_EXTRACT)
    extraction["plugin_version"] = PLUGIN_VERSION
    extraction["source"] = {**source, **(extraction.get("source") or {})}
    extraction.setdefault("template", template)
    ocr_meta = extraction.setdefault("ocr", {})
    ocr_meta.setdefault("engine", ocr_result.get("engine", "unknown"))
    ocr_meta.setdefault("engine_version", ocr_result.get("engine_version", "unknown"))
    ocr_meta.setdefault("n_lines", len(ocr_result.get("lines") or []))
    ocr_meta.setdefault("mean_confidence", round(float(ocr_result.get("mean_confidence", 0.0)), 4))
    # 注意：耗时等易变字段只进命令信封，不进 extraction 文档，
    # 否则「同一张图跑两次结果完全一致」无法成立。

    cache_path = extraction_cache_path(extraction["source"])
    atomic_write_json(cache_path, extraction)
    return extraction, ocr_result, cache_path


def cmd_ocr_extract(payload: dict) -> tuple[dict, int]:
    image = Path(str(require(payload, "image"))).expanduser()
    if not image.is_absolute():
        raise CliError("BAD_INPUT", "`image` 必须是绝对路径")
    if not image.is_file():
        raise CliError("BAD_INPUT", f"图片不存在：{image}")

    template = str(payload.get("template") or "auto")
    min_confidence = float(payload.get("min_confidence", 0.5))
    extraction, ocr_result, cache_path = run_extraction(image, template, min_confidence)
    rows = extraction.get("rows") or []
    columns = extraction.get("columns") or []

    # 识别不到表头/明细时明确报错，而不是把一张空表交给下游：
    # 空表能通过 finance_export_xlsx 写出一个"看起来正常"的 xlsx，这是财务场景不能接受的。
    if not columns or not rows:
        return (
            envelope(
                "ocr_extract",
                False,
                extraction_path=str(cache_path),
                source=extraction["source"],
                template=extraction.get("template"),
                error={
                    "code": "NO_HEADER",
                    "message": (
                        "没有识别到表头或明细行：这张图可能不是表格单据、拍摄过斜/过暗，"
                        "或表头文字太小。已把 OCR 原文写入 extraction_path 供排查；"
                        "请换一张更清晰、正对拍摄的图片。"
                    ),
                },
                n_rows=len(rows),
                n_total_rows=len(extraction.get("total_rows") or []),
                skipped_lines=extraction.get("skipped_lines") or [],
                warnings=extraction.get("warnings") or [],
                ocr=extraction.get("ocr"),
            ),
            3,
        )

    envelope_doc = envelope(
        "ocr_extract",
        True,
        extraction_path=str(cache_path),
        source=extraction["source"],
        template=extraction.get("template"),
        columns=columns,
        header_line=extraction.get("header_line"),
        rows=rows,
        total_rows=extraction.get("total_rows") or [],
        skipped_lines=extraction.get("skipped_lines") or [],
        n_rows=len(rows),
        n_total_rows=len(extraction.get("total_rows") or []),
        warnings=extraction.get("warnings") or [],
        ocr=extraction.get("ocr"),
        elapsed_ms=ocr_result.get("elapsed_ms"),
    )
    return envelope_doc, 0


def load_extraction_payload(payload: dict, finance_core) -> tuple[dict, str | None]:
    raw = payload.get("extraction")
    if isinstance(raw, dict):
        return raw, None
    if not raw:
        raise CliError("BAD_INPUT", "缺少必填参数 `extraction`（extraction JSON 路径）")
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise CliError("BAD_INPUT", "`extraction` 必须是绝对路径")
    loader = getattr(finance_core, "load_extraction", None)
    if callable(loader):
        extraction = loader(str(path))
    else:
        extraction = load_json(path)
    if not isinstance(extraction, dict):
        raise CliError("SCHEMA_MISMATCH", "extraction 文档不是 JSON 对象")
    if extraction.get("schema_version") != SCHEMA_EXTRACT:
        raise CliError(
            "SCHEMA_MISMATCH",
            f"extraction.schema_version 应为 {SCHEMA_EXTRACT}，实际为 {extraction.get('schema_version')!r}",
            3,
        )
    return extraction, str(path)


def cmd_verify(payload: dict) -> tuple[dict, int]:
    finance_core = import_module("finance_core")
    verify = pick_function(finance_core, ["verify", "verify_extraction"], "finance_core")
    extraction, extraction_path = load_extraction_payload(payload, finance_core)

    tolerance = str(payload.get("tolerance", "0.01"))
    low_confidence = float(payload.get("low_confidence", 0.90))
    report = verify(extraction, tolerance=tolerance, low_confidence=low_confidence)

    report.setdefault("schema_version", SCHEMA_VERIFY)
    report.setdefault("source", extraction.get("source") or {})
    status = report.get("status", "fail")
    ok = status in ("pass", "needs_review")
    exit_code = 0 if ok else 3
    doc = envelope(
        "verify",
        ok,
        status=status,
        extraction_path=extraction_path,
        report=report,
    )
    return doc, exit_code


def cmd_export_xlsx(payload: dict) -> tuple[dict, int]:
    finance_core = import_module("finance_core")
    xlsx_export = import_module("xlsx_export")
    verify = pick_function(finance_core, ["verify", "verify_extraction"], "finance_core")
    export_xlsx = pick_function(xlsx_export, ["export_xlsx", "export"], "xlsx_export")

    extraction, extraction_path = load_extraction_payload(payload, finance_core)
    output = Path(str(require(payload, "output"))).expanduser()
    if not output.is_absolute():
        raise CliError("BAD_INPUT", "`output` 必须是绝对路径")
    if not output.parent.is_dir():
        raise CliError("BAD_INPUT", f"输出目录不存在：{output.parent}")

    tolerance = str(payload.get("tolerance", "0.01"))
    low_confidence = float(payload.get("low_confidence", 0.90))
    allow_unverified = bool(payload.get("allow_unverified", False))

    report = verify(extraction, tolerance=tolerance, low_confidence=low_confidence)
    run_meta = {
        "plugin_version": PLUGIN_VERSION,
        "engine": (
            f"{(extraction.get('ocr') or {}).get('engine', 'unknown')} "
            f"{(extraction.get('ocr') or {}).get('engine_version', '')}"
        ).strip(),
        "input_path": (extraction.get("source") or {}).get("path"),
        "input_sha256": (extraction.get("source") or {}).get("sha256"),
        "tolerance": tolerance,
        "extraction_path": extraction_path,
    }
    result = export_xlsx(
        extraction,
        report,
        str(output),
        run_meta,
        allow_unverified=allow_unverified,
    )

    if isinstance(result, dict) and result.get("ok") is False:
        code = result.get("error", {}).get("code") if isinstance(result.get("error"), dict) else None
        message = result.get("error", {}).get("message") if isinstance(result.get("error"), dict) else "导出被拒绝"
        raise CliError(code or "OUTPUT_REFUSED", message or "导出被拒绝", 3)

    status = report.get("status", "fail")
    doc = envelope(
        "export_xlsx",
        True,
        path=str(output),
        output=str(output),
        sha256=(result or {}).get("sha256"),
        sheets=(result or {}).get("sheets") or [],
        verification={
            "status": status,
            "n_hard_failures": len(report.get("hard_failures") or []),
            "n_review_items": len(report.get("review_items") or []),
        },
        run={**(result or {}).get("run", {}), **run_meta},
        note=None if status == "pass" else "导出成功但校验未完全通过；问题清单 sheet 里是全部差异，需人工确认。",
    )
    return doc, 0


def _batch_output_path(output_dir: Path, image: Path, sha8: str, used: set[str]) -> Path:
    """给同一目录里的多张图生成互不覆盖的 xlsx 路径（重名时追加源图哈希前缀）。"""
    base = image.stem.strip() or "document"
    candidate = output_dir / f"{base}.xlsx"
    if candidate.name.lower() in used:
        candidate = output_dir / f"{base}-{sha8}.xlsx"
    counter = 2
    while candidate.name.lower() in used:
        candidate = output_dir / f"{base}-{sha8}-{counter}.xlsx"
        counter += 1
    used.add(candidate.name.lower())
    return candidate


def _collect_batch_images(directory: Path, pattern: str) -> list[Path]:
    if pattern and pattern not in ("*", "*.*"):
        matches = [
            item
            for item in directory.glob(pattern)
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
        ]
    else:
        matches = [
            item
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
        ]
    # 固定按文件名排序，保证同一目录两次批处理结果顺序一致。
    return sorted(matches, key=lambda item: item.name.lower())


def cmd_batch(payload: dict) -> tuple[dict, int]:
    """一个目录里的多张图 -> 每张图各自的 extraction + 校验 + xlsx。

    刻意**不合并**不同单据的金额：每张图是独立的财务单据，合并会造出一张
    现实中不存在的汇总表，比识别错更难发现。批量只负责"逐张跑 + 汇总状态"。
    """
    directory = Path(str(require(payload, "directory"))).expanduser()
    if not directory.is_absolute():
        raise CliError("BAD_INPUT", "`directory` 必须是绝对路径")
    if not directory.is_dir():
        raise CliError("BAD_INPUT", f"目录不存在：{directory}")

    template = str(payload.get("template") or "auto")
    min_confidence = float(payload.get("min_confidence", 0.5))
    tolerance = str(payload.get("tolerance", "0.01"))
    low_confidence = float(payload.get("low_confidence", 0.90))
    allow_unverified = bool(payload.get("allow_unverified", False))
    pattern = str(payload.get("pattern") or "*")
    max_files = max(1, min(int(payload.get("max_files", 200)), 1000))

    output_raw = payload.get("output_dir")
    output_dir = Path(str(output_raw)).expanduser() if output_raw else directory / "finance-ocr-output"
    if not output_dir.is_absolute():
        raise CliError("BAD_INPUT", "`output_dir` 必须是绝对路径")
    output_dir.mkdir(parents=True, exist_ok=True)

    images = _collect_batch_images(directory, pattern)
    if not images:
        raise CliError(
            "BAD_INPUT",
            f"目录里没有支持的图片（png/jpg/jpeg/bmp/webp，pattern={pattern}）：{directory}",
        )
    truncated = len(images) > max_files
    images = images[:max_files]

    modules = pipeline_modules()
    used_names: set[str] = set()
    results: list[dict] = []
    counts = {"pass": 0, "needs_review": 0, "fail": 0, "no_table": 0, "error": 0}
    n_exported = 0

    for index, image in enumerate(images, 1):
        entry = {
            "file": image.name,
            "image": str(image),
            "status": "error",
            "output": None,
            "output_sha256": None,
            "extraction": None,
            "source_sha256": None,
            "n_rows": 0,
            "n_total_rows": 0,
            "hard_failures": [],
            "review_items": 0,
            "error": None,
        }
        sys.stderr.write(f"[finance-ocr] batch {index}/{len(images)}: {image.name}\n")
        try:
            extraction, _ocr_result, cache_path = run_extraction(
                image, template, min_confidence, modules
            )
            entry["extraction"] = str(cache_path)
            entry["source_sha256"] = (extraction.get("source") or {}).get("sha256")
            rows = extraction.get("rows") or []
            columns = extraction.get("columns") or []
            entry["n_rows"] = len(rows)
            entry["n_total_rows"] = len(extraction.get("total_rows") or [])
            if not columns or not rows:
                entry["status"] = "no_table"
                entry["error"] = {
                    "code": "NO_HEADER",
                    "message": "没有识别到表头或明细行，未导出",
                }
                continue

            report = modules["verify"](
                extraction, tolerance=tolerance, low_confidence=low_confidence
            )
            status = report.get("status", "fail")
            entry["status"] = status
            entry["hard_failures"] = [
                item.get("code") for item in report.get("hard_failures") or []
            ]
            entry["review_items"] = len(report.get("review_items") or [])

            if status == "fail" and not allow_unverified:
                entry["error"] = {
                    "code": "FAILED_VERIFICATION",
                    "message": "校验未通过，未导出（确认要导出未校验台账时传 allow_unverified=true）",
                }
                continue

            output_path = _batch_output_path(
                output_dir, image, str(entry["source_sha256"] or "unknown")[:8], used_names
            )
            run_meta = {
                "plugin_version": PLUGIN_VERSION,
                "engine": (
                    f"{(extraction.get('ocr') or {}).get('engine', 'unknown')} "
                    f"{(extraction.get('ocr') or {}).get('engine_version', '')}"
                ).strip(),
                "input_path": str(image),
                "input_sha256": entry["source_sha256"],
                "tolerance": tolerance,
                "extraction_path": str(cache_path),
                "batch": True,
            }
            export_result = modules["export_xlsx"](
                extraction, report, str(output_path), run_meta, allow_unverified=True
            )
            entry["output"] = str(output_path)
            entry["output_sha256"] = (export_result or {}).get("sha256")
            n_exported += 1
        except Exception as error:  # noqa: BLE001 - 单张图失败不能拖垮整批
            entry["status"] = "error"
            entry["error"] = {
                "code": getattr(error, "code", None) or "BATCH_ITEM_FAILED",
                "message": str(error),
            }
        finally:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            results.append(entry)

    summary = {
        "schema_version": "finance-batch/1",
        "plugin_version": PLUGIN_VERSION,
        "directory": str(directory),
        "output_dir": str(output_dir),
        "template": template,
        "tolerance": tolerance,
        "n_total": len(images),
        "n_truncated": truncated,
        "counts": counts,
        "n_exported": n_exported,
        "results": results,
    }
    summary_path = output_dir / "batch-summary.json"
    atomic_write_json(summary_path, summary)

    doc = envelope(
        "batch",
        True,
        directory=str(directory),
        output_dir=str(output_dir),
        summary_path=str(summary_path),
        n_total=len(images),
        n_truncated=truncated,
        n_exported=n_exported,
        counts=counts,
        results=results,
    )
    return doc, 0


DEFAULT_POLICY_PATH = Path.home() / ".dsh" / "finance-ocr" / "accommodation-policy.json"


def _split_paths(value) -> list[Path]:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = re.split(r"[;,]", value)
    else:
        items = []
    return [Path(item.strip()).expanduser() for item in items if item.strip()]


def _dered_image(image_path: Path) -> Path:
    """去掉红色印章再交给 OCR：印章压住的打印字（房间号/姓名）在消除红色后可被识别。"""
    from PIL import Image

    import numpy as _np

    digest = sha256_file(image_path)[:8]
    out = WORK_ROOT / f"dered-{digest}-{image_path.stem}.png"
    if out.is_file():
        return out
    image = Image.open(image_path).convert("RGB")
    arr = _np.asarray(image).astype(_np.int16)
    red = (
        (arr[:, :, 0] > 110)
        & ((arr[:, :, 0] - arr[:, :, 1]) > 25)
        & ((arr[:, :, 0] - arr[:, :, 2]) > 25)
    )
    arr[red] = [255, 255, 255]
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr.astype(_np.uint8)).save(out)
    return out


def _load_accommodation_policy(payload: dict) -> dict:
    policy: dict = {}
    policy_file = payload.get("policy_file")
    candidates = [Path(str(policy_file)).expanduser()] if policy_file else [DEFAULT_POLICY_PATH]
    for path in candidates:
        if path.is_file():
            loaded = load_json(path)
            if isinstance(loaded, dict):
                policy.update(loaded)
    inline = payload.get("policy")
    if isinstance(inline, dict):
        policy.update(inline)
    directors = payload.get("directors")
    if isinstance(directors, str) and directors.strip():
        policy["directors"] = [item.strip() for item in re.split(r"[,;、]", directors) if item.strip()]
    elif isinstance(directors, list):
        policy["directors"] = [str(item).strip() for item in directors if str(item).strip()]
    if payload.get("year") not in (None, ""):
        policy["year"] = int(payload["year"])
    if payload.get("rate") not in (None, ""):
        policy["rate"] = str(payload["rate"])
    if payload.get("department"):
        policy["department"] = str(payload["department"])
    return policy


def _accommodation_extract_pair(pair: dict, policy: dict) -> tuple[dict, Path]:
    """一组住宿材料（某住宿地点的 1~2 页明细 + 1 页流水）→ 结构化数据 + 落盘路径。"""
    detail_paths = _split_paths(pair.get("detail_images"))
    statement_paths = _split_paths(pair.get("statement_image"))
    if not detail_paths or not statement_paths:
        raise CliError(
            "BAD_INPUT",
            "每组需要 detail_images（1~2 张明细图，逗号分隔）和 statement_image（酒店流水图）",
        )
    for path in detail_paths + statement_paths:
        if not path.is_absolute():
            raise CliError("BAD_INPUT", f"图片必须是绝对路径：{path}")
        if not path.is_file():
            raise CliError("BAD_INPUT", f"图片不存在：{path}")

    accommodation_ocr = import_module("accommodation_ocr")
    ocr_engine = import_module("ocr_engine")
    pages = []
    for path in detail_paths[:2]:
        dered = _dered_image(path)
        pages.append((str(dered), ocr_engine.run_ocr(str(dered))))
    statement_dered = _dered_image(statement_paths[0])
    statement_ocr = ocr_engine.run_ocr(str(statement_dered))
    data = accommodation_ocr.build_dataset(
        pages,
        {
            "source": str(statement_dered),
            "lines": statement_ocr.get("lines") or [],
            "width": statement_ocr.get("width"),
            "height": statement_ocr.get("height"),
        },
        policy=policy,
    )
    data["source_images"] = {
        "detail": [str(path) for path in detail_paths[:2]],
        "statement": str(statement_paths[0]),
    }
    save = pair.get("save_dataset")
    if save:
        target = Path(str(save)).expanduser()
        if not target.is_absolute():
            raise CliError("BAD_INPUT", "`save_dataset` 必须是绝对路径")
    else:
        target = WORK_ROOT / f"accommodation-dataset-{sha256_file(statement_paths[0])[:8]}.json"
    atomic_write_json(target, data)
    return data, target


def _load_roster_optional(payload: dict, policy: dict):
    """人员字典（可选）：payload.roster 优先，其次策略文件 roster；没有就返回 None。"""
    raw = payload.get("roster") or policy.get("roster")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise CliError("BAD_INPUT", "`roster` 必须是绝对路径")
    if not path.is_file():
        raise CliError("BAD_INPUT", f"人员字典不存在：{path}")
    return import_module("roster").load(path)


def _apply_roster(data: dict, roster_data) -> dict | None:
    if roster_data is None:
        return None
    roster_module = import_module("roster")
    name_match = roster_module.correct_dataset(data, roster_data)
    if name_match:
        data["name_match"] = name_match
    return name_match


def _augment_report_with_names(report: dict, name_match: dict | None) -> dict:
    """把姓名检查结果写进报告（只提示，不改名、不影响规则结论）。

    姓名以住宿表格为准；字典只用来提示"疑似 OCR 误识"和"名单外人员"，
    由人工确认后再改数据。
    """
    if not name_match:
        return report
    suggestions = name_match.get("suggestions") or []
    if suggestions:
        report.setdefault("notes", []).append(
            {
                "rule": "NAME_SUGGESTION",
                "message": "疑似 OCR 误识（未自动改名，请人工核对）："
                + "；".join(
                    f"{item['from']}→{item['to']}（相似度 {item['ratio']}）" for item in suggestions
                ),
            }
        )
    cross = name_match.get("cross_department") or []
    if cross:
        report.setdefault("notes", []).append(
            {
                "rule": "NAME_CROSS_DEPARTMENT",
                "message": "以下人员属于其他部门名单（可能是借调，按表格保留）：" + "、".join(cross),
            }
        )
    unknown = name_match.get("unknown") or []
    if unknown:
        report.setdefault("notes", []).append(
            {
                "rule": "NAME_NOT_IN_ROSTER",
                "message": "以下姓名在人员字典中查不到（按住宿表格保留，建议确认是否外包/新入职；已记入待确认名单）："
                + "、".join(unknown),
            }
        )
    unmatched_uncertain = name_match.get("unmatched_uncertain") or []
    if unmatched_uncertain:
        report.setdefault("notes", []).append(
            {
                "rule": "NAME_OCR_UNCERTAIN",
                "message": "以下姓名 OCR 不确定且字典未匹配到（见疑似误识建议，需人工看图核对）："
                + "、".join(unmatched_uncertain),
            }
        )
    outsourced = name_match.get("outsourced") or []
    if outsourced:
        report.setdefault("notes", []).append(
            {
                "rule": "NAME_OUTSOURCED",
                "message": "以下为已登记的外包/临时人员（变动频繁，按表格保留）：" + "、".join(outsourced),
            }
        )
    summary = report.setdefault("summary", {})
    summary["n_warnings"] = len(report.get("warnings") or [])
    summary["n_notes"] = len(report.get("notes") or [])
    return report


def _record_unknown_names(payload: dict, policy: dict, batch: str, department, names: list[str]) -> dict | None:
    """把字典查不到的姓名记到"待确认名单"，方便以后统一补入字典。"""
    raw = payload.get("unknown_register") or policy.get("unknown_register")
    if not raw or not names:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise CliError("BAD_INPUT", "`unknown_register` 必须是绝对路径")
    ledger = import_module("ledger")
    return ledger.record_unknown(path, names, batch, department)


def _accommodation_report(accommodation, data: dict, policy: dict) -> dict:
    if not isinstance(data, dict):
        raise CliError("BAD_INPUT", "住宿费数据结构不正确")
    data.setdefault("schema_version", "finance-accommodation/1")
    if isinstance(data.get("policy"), dict):
        for key in ("ledger", "roster", "unknown_register"):
            data["policy"].pop(key, None)
    if policy:
        # 台账/字典路径是本机配置，不写进会落盘/可能被分享的数据集
        policy_for_data = {
            key: value for key, value in policy.items() if key not in ("ledger", "roster", "unknown_register")
        }
        if policy_for_data:
            data["policy"] = {**(data.get("policy") or {}), **policy_for_data}
    return accommodation.check(data)


def _ledger_target(payload: dict, policy: dict) -> Path | None:
    """台账路径：payload.ledger 优先，其次策略文件里的 ledger；都没有就不记账。"""
    raw = payload.get("ledger") or policy.get("ledger")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise CliError("BAD_INPUT", "`ledger` 必须是绝对路径")
    return path


def _guess_batch(payload: dict, report: dict) -> str:
    batch = payload.get("batch")
    if batch not in (None, ""):
        return str(batch)
    first = (report.get("summary") or {}).get("first_night") or ""
    if len(first) >= 7:
        return first[:7]
    return datetime.now().astimezone().strftime("%Y-%m")


def _source_label(dataset: dict) -> str:
    images = dataset.get("source_images") or {}
    statement = images.get("statement")
    if statement:
        return Path(str(statement)).name
    source = (dataset.get("hotel_statement") or {}).get("source")
    if isinstance(source, (list, tuple)):
        source = source[0] if source else None
    if source:
        return Path(str(source)).name
    detail = (dataset.get("detail_sheet") or {}).get("source")
    if isinstance(detail, (list, tuple)) and detail:
        return Path(str(detail[0])).name
    for sheet in dataset.get("detail_sheets") or []:
        source = sheet.get("source")
        if isinstance(source, (list, tuple)) and source:
            return Path(str(source[0])).name
        if source:
            return Path(str(source)).name
    return "unknown"


def _extract_detail_only(payload: dict, policy: dict) -> dict:
    """机关待遇模式：逐页识别无房间号的明细表，组装成 detail_only 数据集。"""
    accommodation_ocr = import_module("accommodation_ocr")
    ocr_engine = import_module("ocr_engine")
    images = [Path(item) for item in _split_paths(payload.get("detail_images"))]
    if not images:
        raise CliError("BAD_INPUT", "`detail_images` 不能为空")
    pages = []
    for image in images:
        if not image.is_absolute():
            raise CliError("BAD_INPUT", "detail_images 必须是绝对路径")
        if not image.is_file():
            raise CliError("EXTRACTION_NOT_FOUND", f"明细表图片不存在：{image}")
        dered = _dered_image(image)
        pages.append((str(dered), ocr_engine.run_ocr(str(dered))))
    sheets = accommodation_ocr.extract_detail_no_room(pages)
    for sheet in sheets:
        sheet["source_images"] = [str(path) for path in images]
    dataset = accommodation_ocr.build_detail_only_dataset(sheets, policy)
    dataset["source_images"] = {"detail": [str(path) for path in images]}
    save_dataset = payload.get("save_dataset")
    if save_dataset:
        target = Path(str(save_dataset)).expanduser()
        if not target.is_absolute():
            raise CliError("BAD_INPUT", "`save_dataset` 必须是绝对路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dataset, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        dataset["saved_to"] = str(target)
    return dataset


def _run_detail_only(payload: dict, policy: dict, roster_data, accommodation) -> tuple[dict, int]:
    dataset_input = payload.get("dataset")
    dataset_path: Path | None = None
    if isinstance(dataset_input, dict):
        data = dict(dataset_input)
    elif isinstance(dataset_input, str) and dataset_input.strip():
        dataset_path = Path(dataset_input).expanduser()
        if not dataset_path.is_absolute():
            raise CliError("BAD_INPUT", "`dataset` 必须是绝对路径")
        data = load_json(dataset_path)
    else:
        data = _extract_detail_only(payload, policy)
        saved = data.get("saved_to")
        if saved:
            dataset_path = Path(saved)
    if not data.get("detail_only"):
        data["detail_only"] = True
    if payload.get("signature_verified") is not None:
        data["signature_verified"] = bool(payload.get("signature_verified"))
    name_match = _apply_roster(data, roster_data)
    report = _accommodation_report(accommodation, data, policy)
    _augment_report_with_names(report, name_match)
    status = report.get("status", "fail")
    ok = status in ("pass", "needs_review")
    location = str(payload.get("label") or "（机关待遇，不核流水）")
    doc = envelope(
        "accommodation",
        ok,
        mode="detail_only",
        no_statement=True,
        status=status,
        dataset_path=str(dataset_path) if dataset_path else None,
        name_match=name_match,
        dataset=data,
        report=report,
    )
    ledger_info = _append_ledger(
        payload, policy, [(data, report, str(dataset_path) if dataset_path else None, location)]
    )
    if ledger_info is not None:
        doc["ledger"] = ledger_info
    if name_match and (name_match.get("unknown") or []):
        register_info = _record_unknown_names(
            payload,
            policy,
            _guess_batch(payload, report),
            name_match.get("department"),
            name_match.get("unknown") or [],
        )
        if register_info is not None:
            doc["unknown_register"] = register_info
    return doc, 0 if ok else 3


def _append_ledger(payload: dict, policy: dict, items: list[tuple[dict, dict, str | None, str]]) -> dict | None:
    """items: [(dataset, report, dataset_path, location)]，写台账并返回台账摘要。"""
    target = _ledger_target(payload, policy)
    if target is None:
        return None
    ledger = import_module("ledger")
    entries = []
    for data, report, dataset_path, location in items:
        entries.append(
            ledger.build_entry(
                data,
                report,
                batch=_guess_batch(payload, report),
                location=location,
                dataset_path=dataset_path,
                plugin_version=PLUGIN_VERSION,
                source=_source_label(data),
            )
        )
    info = ledger.append(target, entries)
    info["entries"] = len(entries)
    return info


def cmd_accommodation(payload: dict) -> tuple[dict, int]:
    """住宿费核对：单组（dataset 或图片）或多组（多个住宿地点）都能跑。"""
    from decimal import Decimal

    accommodation = import_module("accommodation")
    policy = _load_accommodation_policy(payload)
    roster_module = _load_roster_optional(payload, policy)

    # ---------- 机关待遇/无流水模式：只有明细表（无房间号），不核酒店流水 ----------
    dataset_input = payload.get("dataset")
    detail_only_mode = False
    if isinstance(dataset_input, dict):
        detail_only_mode = bool(dataset_input.get("detail_only"))
    elif isinstance(dataset_input, str) and dataset_input.strip():
        probe = Path(dataset_input).expanduser()
        if probe.is_file():
            detail_only_mode = bool((load_json(probe) or {}).get("detail_only"))
    if not detail_only_mode:
        detail_only_mode = bool(payload.get("no_statement")) or (
            payload.get("detail_images") and not payload.get("statement_image") and not dataset_input
        )
    if detail_only_mode:
        return _run_detail_only(payload, policy, roster_module, accommodation)

    # ---------- 多住宿地点模式：pairs = [{label, detail_images, statement_image, save_dataset}, ...] ----------
    pairs_input = payload.get("pairs")
    if isinstance(pairs_input, str) and pairs_input.strip():
        try:
            pairs_input = json.loads(pairs_input)
        except json.JSONDecodeError as error:
            raise CliError("BAD_INPUT", f"`pairs` 必须是 JSON 数组：{error}")
    if isinstance(pairs_input, list) and pairs_input:
        results = []
        ledger_items = []
        counts = {"pass": 0, "needs_review": 0, "fail": 0}
        for index, pair in enumerate(pairs_input, 1):
            if not isinstance(pair, dict):
                raise CliError("BAD_INPUT", f"pairs[{index}] 必须是对象")
            label = str(pair.get("label") or f"住宿地点{index}")
            try:
                data, dataset_path = _accommodation_extract_pair(pair, policy)
                name_match = _apply_roster(data, roster_module)
                report = _accommodation_report(accommodation, data, policy)
                _augment_report_with_names(report, name_match)
                status = report.get("status", "fail")
                counts[status] = counts.get(status, 0) + 1
                ledger_items.append((data, report, str(dataset_path), label))
                results.append(
                    {
                        "index": index,
                        "label": label,
                        "status": status,
                        "ok": status in ("pass", "needs_review"),
                        "dataset_path": str(dataset_path),
                        "name_match": name_match,
                        "summary": report.get("summary"),
                        "errors": report.get("errors") or [],
                        "warnings": report.get("warnings") or [],
                        "notes": report.get("notes") or [],
                        "rules": report.get("rules") or [],
                    }
                )
            except Exception as error:  # noqa: BLE001 - 单个住宿地点失败不拖垮整批
                counts["fail"] = counts.get("fail", 0) + 1
                results.append(
                    {
                        "index": index,
                        "label": label,
                        "status": "fail",
                        "ok": False,
                        "error": {
                            "code": getattr(error, "code", None) or "PAIR_FAILED",
                            "message": str(error),
                        },
                    }
                )
        ok = all(item["ok"] for item in results)
        aggregate = {
            "n_pairs": len(results),
            "counts": counts,
            "n_pass": counts.get("pass", 0),
            "n_needs_review": counts.get("needs_review", 0),
            "n_fail": counts.get("fail", 0),
            "total_nights": sum(
                int((item.get("summary") or {}).get("total_nights") or 0) for item in results
            ),
            "total_amount": str(
                sum(
                    (
                        Decimal(str((item.get("summary") or {}).get("total_amount") or "0"))
                        for item in results
                    ),
                    Decimal("0"),
                )
            ),
        }
        doc = envelope(
            "accommodation",
            ok,
            mode="pairs",
            n_pairs=len(results),
            pairs=results,
            aggregate=aggregate,
        )
        ledger_info = _append_ledger(payload, policy, ledger_items)
        if ledger_info is not None:
            doc["ledger"] = ledger_info
        unknown_names: list[str] = []
        pair_departments: set[str] = set()
        for item in results:
            match = item.get("name_match") or {}
            for name in match.get("unknown") or []:
                if name not in unknown_names:
                    unknown_names.append(name)
            if match.get("department"):
                pair_departments.add(str(match["department"]))
        if unknown_names:
            first_summary = (results[0].get("summary") or {}) if results else {}
            batch_hint = _guess_batch(payload, {"summary": first_summary})
            register_info = _record_unknown_names(
                payload,
                policy,
                batch_hint,
                "、".join(sorted(pair_departments)) if pair_departments else None,
                unknown_names,
            )
            if register_info is not None:
                doc["unknown_register"] = register_info
        return doc, 0 if ok else 3

    # ---------- 单组模式：已结构化 dataset，或 1 组图片 ----------
    dataset_path: Path | None = None
    dataset_input = payload.get("dataset")
    if isinstance(dataset_input, dict):
        data = dict(dataset_input)
    elif isinstance(dataset_input, str) and dataset_input.strip():
        dataset_path = Path(dataset_input).expanduser()
        if not dataset_path.is_absolute():
            raise CliError("BAD_INPUT", "`dataset` 必须是绝对路径")
        data = load_json(dataset_path)
    else:
        data, dataset_path = _accommodation_extract_pair(
            {
                "detail_images": payload.get("detail_images"),
                "statement_image": payload.get("statement_image"),
                "save_dataset": payload.get("save_dataset"),
            },
            policy,
        )

    name_match = _apply_roster(data, roster_module)
    report = _accommodation_report(accommodation, data, policy)
    _augment_report_with_names(report, name_match)
    status = report.get("status", "fail")
    ok = status in ("pass", "needs_review")
    doc = envelope(
        "accommodation",
        ok,
        status=status,
        dataset_path=str(dataset_path) if dataset_path else None,
        name_match=name_match,
        dataset=data,
        report=report,
    )
    ledger_info = _append_ledger(
        payload,
        policy,
        [(data, report, str(dataset_path) if dataset_path else None, str(payload.get("label") or "住宿地点"))],
    )
    if ledger_info is not None:
        doc["ledger"] = ledger_info
    if name_match and (name_match.get("unknown") or []):
        register_info = _record_unknown_names(
            payload,
            policy,
            _guess_batch(payload, report),
            name_match.get("department"),
            name_match.get("unknown") or [],
        )
        if register_info is not None:
            doc["unknown_register"] = register_info
    return doc, 0 if ok else 3


def cmd_ledger(payload: dict) -> tuple[dict, int]:
    """住宿费核对台账：list / export / archive（清理旧记录，避免台账无限膨胀）。"""
    ledger = import_module("ledger")
    raw = payload.get("ledger")
    if not raw:
        # 没显式给路径时读策略文件里的默认台账
        policy = _load_accommodation_policy(payload)
        raw = policy.get("ledger")
    if not raw:
        raise CliError("BAD_INPUT", "需要 ledger（台账 jsonl 的绝对路径）")
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise CliError("BAD_INPUT", "`ledger` 必须是绝对路径")
    action = str(payload.get("action") or "list")

    if action == "list":
        info = ledger.list_entries(path)
        return envelope("ledger", True, action="list", **info), 0
    if action == "export":
        output = payload.get("output") or str(path.with_suffix(".xlsx"))
        target = Path(str(output)).expanduser()
        if not target.is_absolute():
            raise CliError("BAD_INPUT", "`output` 必须是绝对路径")
        xlsx = ledger.export_xlsx(path, target)
        return (
            envelope("ledger", True, action="export", ledger=str(path), xlsx=xlsx),
            0,
        )
    if action == "archive":
        keep_last = payload.get("keep_last")
        keep_last = int(keep_last) if keep_last not in (None, "") else None
        result = ledger.archive(
            path,
            keep_last=keep_last,
            before=payload.get("before") or None,
            archive_dir=payload.get("archive_dir"),
        )
        return envelope("ledger", True, action="archive", **result), 0
    raise CliError("BAD_INPUT", f"未知的 ledger action：{action}")


def cmd_selftest(payload: dict) -> tuple[dict, int]:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("python", sys.version_info >= (3, 10), sys.version.splitlines()[0])
    check("cli_py", (HERE / "cli.py").is_file(), str(HERE / "cli.py"))
    check("contract", (HERE / "CONTRACT.md").is_file(), "lib/CONTRACT.md")

    for module_name in ("finance_core", "xlsx_export", "ocr_engine", "table_extract", "accommodation", "accommodation_ocr", "ledger", "roster"):
        try:
            importlib.import_module(module_name)
            check(f"module:{module_name}", True, "import ok")
        except Exception as error:  # noqa: BLE001 - 自检要把坏消息也报出来
            check(f"module:{module_name}", False, f"{type(error).__name__}: {error}")

    try:
        import cv2

        check("opencv", True, cv2.__version__)
    except Exception as error:  # noqa: BLE001
        check("opencv", False, f"{type(error).__name__}: {error}")

    engine_version = None
    try:
        ocr_engine = importlib.import_module("ocr_engine")
        getter = getattr(ocr_engine, "engine_info", None)
        if callable(getter):
            info = getter()
            engine_version = info.get("version")
            check("ocr_engine", True, f"{info.get('engine')} {engine_version}")
        else:
            import rapidocr_onnxruntime  # noqa: F401

            engine_version = getattr(rapidocr_onnxruntime, "__version__", "unknown")
            check("ocr_engine", True, f"rapidocr_onnxruntime {engine_version}")
    except Exception as error:  # noqa: BLE001
        check("ocr_engine", False, f"{type(error).__name__}: {error}")

    try:
        import openpyxl

        check("openpyxl", True, openpyxl.__version__)
    except Exception as error:  # noqa: BLE001
        check("openpyxl", False, f"{type(error).__name__}: {error}")

    fonts = [p for p in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simsun.ttc")) if p.is_file()]
    check("cjk_font", bool(fonts), ", ".join(str(p) for p in fonts) or "没有找到中文字体（只影响合成样图）")

    ok = all(item["ok"] for item in checks)
    doc = envelope(
        "selftest",
        ok,
        python=sys.executable,
        plugin_dir=str(PLUGIN_DIR),
        engine="rapidocr_onnxruntime" if engine_version else None,
        engine_version=engine_version,
        checks=checks,
        time=now_iso(),
    )
    return doc, 0 if ok else 3


COMMANDS = {
    "ocr_extract": cmd_ocr_extract,
    "verify": cmd_verify,
    "export_xlsx": cmd_export_xlsx,
    "batch": cmd_batch,
    "accommodation": cmd_accommodation,
    "ledger": cmd_ledger,
    "selftest": cmd_selftest,
}

KNOWN_ERROR_CODES = {
    "BAD_INPUT",
    "SCHEMA_MISMATCH",
    "EXTRACTION_NOT_FOUND",
    "OUTPUT_REFUSED",
    "IO_ERROR",
    "NO_OCR_ENGINE",
    "OCR_FAILED",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-plugin-finance-ocr Python bridge")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--input", required=True, help="输入 JSON 文件路径（可传空对象 {}）")
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径（必写）")
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    started = time.time()
    try:
        input_path = Path(args.input)
        if input_path.is_file():
            payload = load_json(input_path)
            if not isinstance(payload, dict):
                raise CliError("BAD_INPUT", "输入 JSON 必须是对象")
        else:
            payload = {}
        doc, exit_code = COMMANDS[args.command](payload)
    except CliError as error:
        doc = failure(args.command, error.code, error.message)
        doc["traceback"] = traceback.format_exc() if error.code == "INTERNAL_ERROR" else None
        exit_code = error.exit_code
    except Exception as error:  # noqa: BLE001 - 任何异常都要变成可读 JSON
        # 子模块（finance_core / xlsx_export / ocr_engine / table_extract）抛出的
        # 业务异常统一带 .code 属性，这里把它映射成契约里的错误码，而不是吞成 INTERNAL_ERROR。
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in KNOWN_ERROR_CODES:
            message = str(getattr(error, "message", "") or error)
            doc = failure(args.command, code, message)
            doc["traceback"] = None
            exit_code = 3 if code in ("OUTPUT_REFUSED", "SCHEMA_MISMATCH") else 2
        else:
            doc = failure(args.command, "INTERNAL_ERROR", f"{type(error).__name__}: {error}")
            doc["traceback"] = traceback.format_exc()
            exit_code = 3

    doc.setdefault("elapsed_ms", int((time.time() - started) * 1000))
    try:
        atomic_write_json(output_path, doc)
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"[finance-ocr] 无法写出结果文件 {output_path}: {error}\n")
        return 2
    if not doc.get("ok"):
        sys.stderr.write(
            "[finance-ocr] "
            + str((doc.get("error") or {}).get("message", "失败"))
            + "\n"
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
