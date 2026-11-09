"""真实数据接入前体检：字段、类型、主键、外键、隐私脱敏、新鲜度、覆盖率。

设计原则：宁可拒绝，也不让脏数据进库后污染看板结论。
  error   → 阻断导入
  warning → 允许导入，但在报告与看板里显式降级
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from .adapters import LocalExportAdapter
from .store import TABLES

FIELD_DICT = {
    "bloggers": [
        ("id", "必填", "博主唯一 ID，主键", "B001"),
        ("platform", "必填", "平台：抖音 / 小红书", "抖音"),
        ("nickname", "必填", "昵称（请脱敏）", "某美妆博主"),
        ("industry", "必填", "行业垂类", "美妆"),
        ("followers", "必填", "当前粉丝数", "128000"),
        ("followers_7d", "选填", "7 日前粉丝数；缺省由快照回填", "125000"),
        ("followers_30d", "选填", "30 日前粉丝数；缺省由快照回填", "119000"),
        ("total_likes", "选填", "累计获赞；缺省由作品汇总", "980000"),
        ("verified", "选填", "是否认证 0/1", "1"),
        ("has_membership", "选填", "是否开通会员 0/1", "1"),
        ("member_price", "选填", "会员月费（元）", "12"),
        ("member_count", "选填", "会员人数", "860"),
        ("member_works", "选填", "会员专属内容数", "8"),
        ("source", "选填", "数据来源标识", "authorized_export"),
        ("collected_at", "选填", "采集日期 YYYY-MM-DD", "2026-11-09"),
    ],
    "works": [
        ("id", "必填", "作品唯一 ID，主键", "B001-W001"),
        ("blogger_id", "必填", "外键 → bloggers.id", "B001"),
        ("title", "必填", "作品标题", "粉底液实测"),
        ("publish_at", "必填", "发布时间 YYYY-MM-DDTHH:MM", "2026-11-01T20:30"),
        ("duration_s", "选填", "时长（秒）", "45"),
        ("views", "必填", "播放量", "120000"),
        ("likes", "必填", "点赞数", "6800"),
        ("comments", "必填", "评论数（平台计数）", "320"),
        ("shares", "必填", "分享数", "210"),
        ("cover_style", "选填", "封面类型", "真人出镜"),
        ("tag", "选填", "内容标签", "测评"),
        ("source", "选填", "数据来源标识", "authorized_export"),
    ],
    "comments": [
        ("id", "必填", "评论唯一 ID，主键", "B001-W001-C001"),
        ("work_id", "必填", "外键 → works.id", "B001-W001"),
        ("blogger_id", "必填", "外键 → bloggers.id", "B001"),
        ("content", "必填", "评论正文（请脱敏）", "性价比很高，已回购"),
        ("likes", "选填", "评论点赞", "12"),
        ("created_at", "选填", "评论时间", "2026-11-01T21:02"),
        ("replied", "选填", "博主是否回复 0/1", "0"),
        ("sentiment", "选填", "预留，由引擎重算", "0.5"),
        ("source", "选填", "数据来源标识", "authorized_export"),
    ],
    "products": [
        ("id", "必填", "商品唯一 ID，主键", "B001-P001"),
        ("blogger_id", "必填", "外键 → bloggers.id", "B001"),
        ("name", "必填", "商品名", "粉底液套装"),
        ("category", "必填", "品类", "美妆护肤"),
        ("price", "必填", "标价（元）", "129"),
        ("sold_display", "选填", "平台销量标识（区间中值）", "2400"),
        ("rating", "选填", "评分", "4.8"),
        ("source", "选填", "数据来源标识", "authorized_export"),
    ],
    "fan_snapshots": [
        ("blogger_id", "必填", "外键 → bloggers.id", "B001"),
        ("date", "必填", "快照日期 YYYY-MM-DD", "2026-11-09"),
        ("followers", "必填", "当日粉丝数", "128000"),
        ("source", "选填", "数据来源标识", "authorized_export"),
    ],
}
REQUIRED = {k: [f[0] for f in v if f[1] == "必填"] for k, v in FIELD_DICT.items()}
NUMERIC_INT = {"followers", "followers_7d", "followers_30d", "total_likes", "verified",
               "has_membership", "member_count", "member_works", "duration_s", "views",
               "likes", "comments", "shares", "replied", "sold_display"}
NUMERIC_FLOAT = {"member_price", "price", "rating", "sentiment"}
PK = {"bloggers": "id", "works": "id", "comments": "id", "products": "id",
      "fan_snapshots": None}
FK = {"works": [("blogger_id", "bloggers")],
      "comments": [("work_id", "works"), ("blogger_id", "bloggers")],
      "products": [("blogger_id", "bloggers")],
      "fan_snapshots": [("blogger_id", "bloggers")]}
SENSITIVE = re.compile(r"手机|电话|mobile|phone|微信|wechat|身份证|idcard|邮箱|e?mail|真实姓名|real_?name|open_?id|union_?id", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _err(errors, file, code, msg):
    errors.append({"file": file, "code": code, "message": msg})


def _warn(warnings, file, code, msg):
    warnings.append({"file": file, "code": code, "message": msg})


def validate_directory(directory: str | Path) -> dict:
    d = Path(directory)
    result = {"directory": str(d), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "files": {}, "errors": [], "warnings": [], "coverage": {}, "summary": {}}
    if not d.is_dir():
        _err(result["errors"], "-", "DIR_MISSING", f"目录不存在：{d}")
        result["ok"] = False
        return result

    rows_by: dict[str, list[dict]] = {}
    for name in TABLES:
        info = {"exists": False, "rows": 0, "columns": [], "path": None}
        for ext in ("csv", "json"):
            p = d / f"{name}.{ext}"
            if p.exists():
                info.update({"exists": True, "path": str(p)})
                try:
                    rows = LocalExportAdapter._read(p)
                except Exception as e:
                    _err(result["errors"], name, "PARSE_ERROR", f"解析失败：{e}")
                    rows = []
                info["rows"] = len(rows)
                info["columns"] = list(rows[0].keys()) if rows else []
                rows_by[name] = rows
                break
        result["files"][name] = info
        if not info["exists"]:
            if name == "bloggers":
                _err(result["errors"], name, "FILE_MISSING", "缺少 bloggers.csv / bloggers.json（唯一必需文件）")
            else:
                _warn(result["warnings"], name, "FILE_MISSING",
                      f"缺少 {name}：对应分析维度将降级为空态")
            continue
        rows = rows_by.get(name, [])
        cols = info["columns"]

        # 隐私字段
        for c in cols:
            if SENSITIVE.search(c):
                _err(result["errors"], name, "PII_COLUMN",
                     f"检测到疑似个人信息列「{c}」，请先删除或脱敏（本系统只保留分析所需字段）")
        # 必需字段
        missing = [c for c in REQUIRED[name] if c not in cols]
        if missing:
            _err(result["errors"], name, "MISSING_COLUMN", "缺少必需列：" + "、".join(missing))
        extra = [c for c in cols if c not in [f[0] for f in FIELD_DICT[name]]]
        if extra:
            _warn(result["warnings"], name, "UNKNOWN_COLUMN",
                  "将被忽略的未知列：" + "、".join(extra[:8]))
        if not rows:
            if name == "bloggers":
                _err(result["errors"], name, "EMPTY", " bloggers 表无数据行")
            else:
                _warn(result["warnings"], name, "EMPTY", "文件为空，对应维度降级")
            continue
        # 模板示例行
        if name in ("bloggers", "works", "comments", "products") and "id" in cols:
            n_example = sum(1 for r in rows if str(r.get("id", "")).startswith("EXAMPLE-"))
            if n_example:
                _err(result["errors"], name, "TEMPLATE_ROWS",
                     f"仍有 {n_example} 行模板示例（id 以 EXAMPLE- 开头），请删除后再导入")
        # 主键重复
        pk = PK[name]
        if pk and pk in cols:
            dup = [k for k, v in Counter(str(r.get(pk, "")) for r in rows).items() if v > 1]
            if dup:
                _err(result["errors"], name, "DUPLICATE_PK",
                     f"{pk} 重复 {len(dup)} 个，示例：{'、'.join(dup[:3])}")
        if name == "fan_snapshots":
            dup = [k for k, v in Counter((str(r.get("blogger_id")), str(r.get("date"))) for r in rows).items() if v > 1]
            if dup:
                _err(result["errors"], name, "DUPLICATE_PK",
                     f"(blogger_id,date) 重复 {len(dup)} 组")
        # 数值与格式
        bad_num = 0
        for r in rows:
            for c in cols:
                v = r.get(c)
                if v in (None, ""):
                    continue
                if c in NUMERIC_INT or c in NUMERIC_FLOAT:
                    try:
                        float(v)
                    except (TypeError, ValueError):
                        bad_num += 1
            if name == "fan_snapshots" and r.get("date") and not DATE_RE.match(str(r["date"])):
                bad_num += 1
            if name == "works" and r.get("publish_at") and not DATETIME_RE.match(str(r["publish_at"])[:16]):
                bad_num += 1
        if bad_num:
            _err(result["errors"], name, "BAD_VALUE",
                 f"{bad_num} 个数值/日期无法解析（数值列需纯数字；日期 YYYY-MM-DD；发布时间 YYYY-MM-DDTHH:MM）")
        # 业务合理性
        if name == "bloggers":
            neg = sum(1 for r in rows if _f(r.get("followers")) < 0)
            if neg:
                _warn(result["warnings"], name, "NEGATIVE", f"{neg} 行粉丝数为负")
            unknown_p = sorted({str(r.get("platform")) for r in rows} - {"抖音", "小红书"})
            if unknown_p:
                _warn(result["warnings"], name, "UNKNOWN_PLATFORM",
                      "非标准平台值（会归入其他）：" + "、".join(unknown_p[:5]))
        if name == "works":
            bad = sum(1 for r in rows if _f(r.get("likes")) + _f(r.get("comments")) + _f(r.get("shares"))
                      > max(_f(r.get("views")), 1) * 1.5)
            if bad:
                _warn(result["warnings"], name, "INTERACTION_OUTLIER",
                      f"{bad} 行互动总数超过播放量 1.5 倍，请核对字段是否错位")

    # 外键与覆盖率
    ids = {n: {str(r.get("id")) for r in rows_by.get(n, []) if r.get("id")}
           for n in ("bloggers", "works", "comments", "products")}
    for name, refs in FK.items():
        if name not in rows_by:
            continue
        for col, target in refs:
            bad = sum(1 for r in rows_by[name] if str(r.get(col, "")) not in ids[target])
            if bad:
                _err(result["errors"], name, "BROKEN_FK",
                     f"{bad} 行 {col} 在 {target} 中找不到对应记录")

    b_ids = ids["bloggers"]
    for dim, target in (("作品", "works"), ("评论", "comments"), ("商品", "products"),
                        ("粉丝快照", "fan_snapshots")):
        covered = {str(r.get("blogger_id")) for r in rows_by.get(target, []) if r.get("blogger_id")}
        pct = round(len(covered & b_ids) / len(b_ids) * 100, 1) if b_ids else 0
        result["coverage"][dim] = pct
        if pct < 80:
            _warn(result["warnings"], target, "LOW_COVERAGE",
                  f"仅 {pct}% 的博主有{dim}数据，相关结论只覆盖这部分样本")

    dates = sorted(str(r.get("date")) for r in rows_by.get("fan_snapshots", []) if DATE_RE.match(str(r.get("date", ""))))
    if dates:
        latest = dates[-1]
        try:
            age = (date.today() - date.fromisoformat(latest)).days
            result["summary"]["latest_snapshot"] = latest
            result["summary"]["data_age_days"] = age
            if age > 30:
                _warn(result["warnings"], "fan_snapshots", "STALE",
                      f"最新粉丝快照距今 {age} 天，增长类结论时效性不足")
        except ValueError:
            pass
    result["summary"]["bloggers"] = len(rows_by.get("bloggers", []))
    result["summary"]["works"] = len(rows_by.get("works", []))
    result["summary"]["comments"] = len(rows_by.get("comments", []))
    result["summary"]["products"] = len(rows_by.get("products", []))
    result["summary"]["fan_snapshots"] = len(rows_by.get("fan_snapshots", []))
    result["summary"]["error_count"] = len(result["errors"])
    result["summary"]["warning_count"] = len(result["warnings"])
    result["ok"] = not result["errors"]
    return result


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def format_report(r: dict) -> str:
    lines = ["体检结果：" + ("✅ 通过，可导入" if r["ok"] else "🔴 未通过，已阻断导入"),
             f"目录：{r['directory']}", ""]
    lines.append(f"{'文件':<18}{'行数':>8}  {'状态'}")
    for n, f in r["files"].items():
        lines.append(f"{n:<18}{f['rows']:>8}  {'存在' if f['exists'] else '缺失'}")
    lines.append("")
    if r["coverage"]:
        lines.append("覆盖率：" + "  ".join(f"{k} {v}%" for k, v in r["coverage"].items()))
    if r["summary"].get("latest_snapshot"):
        lines.append(f"最新快照：{r['summary']['latest_snapshot']}（距今 {r['summary']['data_age_days']} 天）")
    for level, key, icon in (("errors", "错误", "🔴"), ("warnings", "提醒", "🟡")):
        if r[level]:
            lines.append(f"\n{icon} {key}（{len(r[level])}）")
            for x in r[level][:20]:
                lines.append(f"  [{x['file']}] {x['message']}")
            if len(r[level]) > 20:
                lines.append(f"  … 另有 {len(r[level]) - 20} 条")
    return "\n".join(lines)
