"""交付物输出：CSV 快照 / 分析 JSON / 自包含 HTML 仪表盘。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .schema import Dataset, to_dicts
from .store import TABLES

EXPORTS = {
    "bloggers": lambda ds: ds.bloggers,
    "works": lambda ds: ds.works,
    "comments": lambda ds: ds.comments,
    "products": lambda ds: ds.products,
    "fan_snapshots": lambda ds: ds.fan_snapshots,
}


def export_csv(ds: Dataset, outdir: str | Path) -> dict[str, int]:
    """按数据契约导出 CSV 快照；行数可与 SQLite 逐表核对。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, getter in EXPORTS.items():
        rows = to_dicts(getter(ds))
        path = out / f"{name}.csv"
        cols = list(TABLES[name])
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        counts[name] = len(rows)
    return counts


def write_json(report: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def build_dashboard(report: dict, template: str | Path, out: str | Path) -> Path:
    """把分析结果内嵌进 HTML 模板，产出零外链、可离线双击打开的单文件。

    注入前对 `</` 做转义，避免数据里的字符串提前闭合 <script> 标签。
    """
    tpl = Path(template).read_text(encoding="utf-8")
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    marker = "/*__PAYLOAD__*/"
    if marker not in tpl:
        raise ValueError(f"模板缺少占位符 {marker}")
    html = tpl.replace(marker, f"const REPORT = {payload};")
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p


def verify_csv_vs_db(ds: Dataset, outdir: str | Path) -> dict:
    """交付前一致性核对：CSV 行数、字段数与内存数据集一致。"""
    out = Path(outdir)
    result = {}
    for name, getter in EXPORTS.items():
        path = out / f"{name}.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        expected = len(getter(ds))
        result[name] = {
            "csv_rows": len(rows), "dataset_rows": expected,
            "columns": len(rows[0]) if rows else 0,
            "ok": len(rows) == expected and (not rows or len(rows[0]) == len(TABLES[name])),
        }
    return result
