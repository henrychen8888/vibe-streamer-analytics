#!/usr/bin/env python3
"""Vibe Streamer Analytics · 命令行入口

  演示 / 体检
    python3 run.py                     # 合成演示数据全链路
    python3 run.py template [DIR]      # 生成真实数据导入模板 + 字段字典
    python3 run.py validate DIR        # 导入前体检（字段/类型/外键/隐私/新鲜度）
    python3 run.py verify              # 交付前一致性核对
    python3 run.py selftest            # 单元测试

  数据接入
    python3 run.py import DIR          # 自有/授权导出台账：体检 → 脱敏回填 → 分析
    python3 run.py oauth               # 打印抖音官方授权链接
    python3 run.py oauth --code CODE   # 用授权 code 换 token 并写入 config.json
    python3 run.py sync                # 官方 OAuth 拉取已授权账号自身数据 → 分析
    python3 run.py scopes              # 官方 scope 清单与授权边界

  其它
    python3 run.py demo | analyze      # 分步执行
    --seed N --per-industry N --out DIR --db PATH --config PATH
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import adapters, store  # noqa: E402
from engine.analytics import build_report  # noqa: E402
from engine.crawler import make_adapters, load_whitelist  # noqa: E402
from engine.reconcile import reconcile  # noqa: E402
from engine.report import build_dashboard, export_csv, verify_csv_vs_db, write_json  # noqa: E402
from engine.seed import DEFAULT_SEED  # noqa: E402
from engine.validate import FIELD_DICT, format_report, validate_directory  # noqa: E402

OUT = ROOT / "out"
DB = OUT / "analytics.db"
TEMPLATE = ROOT / "web" / "index.template.html"
DASHBOARD = ROOT / "dashboard.html"


def _table(headers, rows) -> str:
    if not rows:
        return "(空)"
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |", line]
    out += ["| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)) + " |" for r in rows]
    out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- 演示数据
def cmd_demo(args) -> int:
    adapter = adapters.DemoAdapter(seed=args.seed, bloggers_per_industry=args.per_industry)
    ds = adapter.fetch_dataset()
    conn = store.connect(args.db)
    counts = store.save_dataset(conn, ds, source=adapter.source)
    store.set_meta(conn, "watermark", str(args.seed))
    store.set_meta(conn, "reconciliation", "")
    conn.close()
    print("[demo] 演示数据已写入 SQLite（固定种子，可复现）")
    print(_table(["实体", "行数"], [[k, f"{v:,}"] for k, v in counts.items()]))
    return 0


# ---------------------------------------------------------------- 模板与体检
def cmd_template(args) -> int:
    d = Path(args.directory) if args.directory else ROOT / "import_template"
    d.mkdir(parents=True, exist_ok=True)
    links = {
        "bloggers": {"id": "EXAMPLE-001"},
        "works": {"id": "EXAMPLE-W001", "blogger_id": "EXAMPLE-001"},
        "comments": {"id": "EXAMPLE-C001", "work_id": "EXAMPLE-W001", "blogger_id": "EXAMPLE-001"},
        "products": {"id": "EXAMPLE-P001", "blogger_id": "EXAMPLE-001"},
        "fan_snapshots": {"blogger_id": "EXAMPLE-001", "date": "2026-11-09"},
    }
    for name, fields in FIELD_DICT.items():
        cols = [f[0] for f in fields]
        row = {f[0]: f[3] for f in fields}
        row.update(links[name])
        with (d / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow(row)
    print(f"[template] 已生成 5 个 CSV 模板 → {d}")
    print("⚠️  每个文件第 2 行是示例（id 以 EXAMPLE- 开头），填写真实数据后请删掉示例行；"
          "validate 会拦截未删除的示例行。\n")
    for name, fields in FIELD_DICT.items():
        print(f"── {name}.csv ──")
        print(_table(["字段", "必填", "说明", "示例"],
                     [[f[0], f[1], f[2], f[3]] for f in fields]))
        print()
    print("最少只需 bloggers.csv；其余 4 个文件缺失时，对应分析维度会自动降级为空态。")
    print(f"填好后执行：python3 run.py validate {d} && python3 run.py import {d}")
    return 0


def cmd_validate(args) -> int:
    if not args.directory:
        print("[validate] 请提供数据目录，例如：python3 run.py validate import_template")
        return 1
    r = validate_directory(args.directory)
    print("[validate] " + format_report(r))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_json(r, out / "validation_report.json")
    print(f"\n完整体检报告：{out / 'validation_report.json'}")
    return 0 if r["ok"] else 1


# ---------------------------------------------------------------- 导入
def cmd_import(args) -> int:
    if not args.directory:
        print("[import] 请提供数据目录；没有台账可先运行 python3 run.py template")
        return 1
    r = validate_directory(args.directory)
    print("[import] 导入前体检：" + ("✅ 通过" if r["ok"] else "🔴 未通过"))
    write_json(r, Path(args.out) / "validation_report.json")
    if not r["ok"]:
        print(format_report(r))
        print("\n已阻断导入。修正后重新执行；完整报告见 out/validation_report.json")
        return 1
    for w in r["warnings"][:8]:
        print(f"  🟡 [{w['file']}] {w['message']}")

    adapter = adapters.LocalExportAdapter(args.directory)
    ds = adapter.fetch_dataset()
    stats = reconcile(ds, source=adapter.source)   # 脱敏 + 确定性回填

    conn = store.connect(args.db)
    counts = store.save_dataset(conn, ds, source=adapter.source)
    store.set_meta(conn, "reconciliation", json.dumps(stats, ensure_ascii=False))
    store.set_meta(conn, "watermark", max((s.date for s in ds.fan_snapshots), default=date.today().isoformat()))
    store.audit(conn, "reconcile", json.dumps(stats, ensure_ascii=False))
    conn.close()
    print(f"\n[import] 已导入 {args.directory}")
    print(_table(["实体", "行数"], [[k, f"{v:,}"] for k, v in counts.items()]))
    print(_table(["回填 / 脱敏", "条数"], [[k, v] for k, v in stats.items()
                                        if isinstance(v, int)]))
    for n in stats["notes"]:
        print("  🟡 " + n)
    return cmd_analyze(args)


# ---------------------------------------------------------------- 官方 OAuth
def _config_path(args) -> Path:
    return Path(args.config) if Path(args.config).is_absolute() else ROOT / args.config


def cmd_oauth(args) -> int:
    p = _config_path(args)
    if not p.exists():
        print(f"[oauth] 缺少 {p}。请先：cp config.example.json config.json 并填入 client_key / client_secret")
        return 1
    cfg = json.loads(p.read_text(encoding="utf-8"))
    adapter = adapters.DouyinOpenApiAdapter.from_config(p)
    if args.code:
        if not adapter.client_key or not adapter.client_secret:
            print("[oauth] config.json 中 client_key / client_secret 未填写")
            return 1
        try:
            d = adapter.exchange_code(args.code)
        except adapters.AdapterError as e:
            print(f"[oauth] 换取 token 失败：{e}")
            return 1
        cfg.update({"access_token": adapter.access_token, "refresh_token": adapter.refresh_token,
                    "open_ids": adapter.open_ids})
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[oauth] ✅ 授权成功，token 已写入 {p}")
        print(f"  已授权账号 open_id：{', '.join(adapter.open_ids) or '(返回中未包含，请手工填入 open_ids)'}")
        if d.get("scope"):
            print("  scope：" + ", ".join(d["scope"].split(",")[:8]))
        print("  下一步：python3 run.py sync")
        return 0
    print("[oauth] 第 1 步 · 让达人在浏览器打开以下链接完成授权：\n")
    print("  " + adapter.authorize_url() + "\n")
    print("  回调地址需与控制台配置一致；回调时务必校验 state 防 CSRF。")
    print("第 2 步 · 用回调里的 code 换取 token：")
    print("  python3 run.py oauth --code 回调中的code")
    print("\n⚠️  该链接只能拿到「授权账号自身」的数据。第三方达人请走星图/服务商渠道或授权导出台账。")
    print("⚠️  config.json 含凭证，请勿提交版本库。")
    return 0


def cmd_sync(args) -> int:
    p = _config_path(args)
    try:
        adapter = adapters.DouyinOpenApiAdapter.from_config(p)
        print(f"[sync] 数据源：{adapter.name} · 已授权账号 {len(adapter.open_ids)} 个")
        print("[sync] " + adapter.describe()["boundary"])
        ds = adapter.fetch_dataset()
    except adapters.AdapterError as e:
        print(f"[sync] 无法同步：{e}")
        print("  排查顺序：config.json 凭证 → run.py oauth 授权 → 控制台权限申请状态 → 网络连通性")
        return 1
    if not ds.bloggers:
        print("[sync] 未返回任何账号数据，已中止，不覆盖现有库")
        return 1
    stats = reconcile(ds, source=adapter.source)
    conn = store.connect(args.db)
    counts = store.save_dataset(conn, ds, source=adapter.source)
    store.set_meta(conn, "reconciliation", json.dumps(stats, ensure_ascii=False))
    store.audit(conn, "oauth_sync", f"accounts={len(adapter.open_ids)}; counts={counts}")
    conn.close()
    print(_table(["实体", "行数"], [[k, f"{v:,}"] for k, v in counts.items()]))
    print("  🟡 官方接口不提供评论/商品/会员明细，且只有本次 1 个粉丝快照点；"
          "相关维度为空态，请用 import 补齐授权导出台账。")
    return cmd_analyze(args)


# ---------------------------------------------------------------- 分析与核对
def cmd_analyze(args) -> int:
    conn = store.connect(args.db)
    if not conn.execute("SELECT 1 FROM bloggers LIMIT 1").fetchone():
        conn.close()
        print("[analyze] 数据库为空，请选一种接入方式：")
        print("  python3 run.py template && python3 run.py import 目录   # 自有/授权台账")
        print("  python3 run.py oauth && python3 run.py sync            # 官方 OAuth")
        print("  python3 run.py demo                                     # 演示数据")
        return 1
    ds = store.load_dataset(conn)
    source = store.get_meta(conn, "last_source", "unknown")
    freshness = store.freshness(conn)
    recon_raw = store.get_meta(conn, "reconciliation")
    conn.close()

    report = build_report(ds, source=source, seed=args.seed if source == "synthetic_demo" else None)
    report["meta"]["freshness"] = freshness
    report["meta"]["reconciliation"] = json.loads(recon_raw) if recon_raw else None
    out = Path(args.out)
    json_path = write_json(report, out / "analytics.json")
    csv_counts = export_csv(ds, out)
    html_path = build_dashboard(report, TEMPLATE, DASHBOARD)

    print("[analyze] 分析完成")
    print(_table(["维度", "结果"], [
        ["数据源", source + ("（合成演示）" if report["meta"]["is_demo"] else "（真实数据）")],
        ["行业 / 博主", f"{len(report['industry'])} / {len(report['bloggers'])}"],
        ["作品 / 评论", f"{len(ds.works):,} / {len(ds.comments):,}"],
        ["中位互动率", f"{report['kpi'][2]['value']}%"],
        ["洞察条数", len(report["insights"])],
    ]))
    print(_table(["交付物", "路径 / 行数"], [
        ["分析 JSON", str(json_path.relative_to(ROOT))],
        ["仪表盘 HTML", str(html_path.relative_to(ROOT)) + f"  ({html_path.stat().st_size / 1024:.0f} KB)"],
        *[[f"CSV · {k}", f"{v:,} 行"] for k, v in csv_counts.items()],
    ]))
    return cmd_verify(args)


def cmd_verify(args) -> int:
    conn = store.connect(args.db)
    ds = store.load_dataset(conn)
    db_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in store.TABLES}
    conn.close()
    out = Path(args.out)
    csv_check = verify_csv_vs_db(ds, out)
    rows, ok = [], True
    for name, c in csv_check.items():
        db_n = db_counts[name]
        good = c["ok"] and c["csv_rows"] == db_n
        ok &= good
        rows.append([name, f"{c['csv_rows']:,}", f"{db_n:,}", "✅ 一致" if good else "🔴 不一致"])
    html = DASHBOARD.read_text(encoding="utf-8") if DASHBOARD.exists() else ""
    external = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html)
    has_payload = "const REPORT =" in html and "/*__PAYLOAD__*/" not in html
    offline = not external and has_payload and len(html) > 10000
    ok &= offline
    rows.append(["HTML 离线自包含", f"外链 {len(external)}", f"内嵌数据 {'有' if has_payload else '无'}",
                 "✅ 通过" if offline else "🔴 失败"])
    print("[verify] 交付前一致性核对")
    print(_table(["检查项", "CSV / 实测", "DB / 期望", "状态"], rows))
    return 0 if ok else 1


def cmd_selftest(args) -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def cmd_scopes(args) -> int:
    a = adapters.DouyinOpenApiAdapter(client_key="YOUR_CLIENT_KEY", redirect_uri="https://example.com/callback")
    d = a.describe()
    print("[scopes] 抖音开放平台权限点（以控制台实际下发为准）")
    print(_table(["scope", "说明"], [[k, v] for k, v in d["scope_desc"].items()]))
    print("\n接口路径（可在 config.json 的 endpoints 覆盖）：")
    print(_table(["键", "路径"], [[k, v] for k, v in d["endpoints"].items()]))
    print("\n授权边界：" + d["boundary"])
    print("\n官方 OAuth 无法提供，禁止在报表中伪造：")
    for x in d["not_available"]:
        print("  - " + x)
    return 0


def cmd_crawl(args) -> int:
    wp = Path(args.whitelist)
    if not wp.exists():
        print(f"[crawl] 缺少 {wp}。请先：cp crawl_whitelist.example.json crawl_whitelist.json 并登记账号")
        return 1
    try:
        cfg = load_whitelist(wp)
    except adapters.AdapterError as e:
        print(f"[crawl] 白名单校验失败：{e}")
        return 1
    adapters_list = make_adapters(cfg)
    if not adapters_list:
        print("[crawl] 白名单中没有可采集的账号（platform 需为 抖音/小红书）")
        return 1
    from engine.schema import Dataset as _DS
    ds = _DS()
    audit_rows = []
    for ad in adapters_list:
        print(f"[crawl] 数据源：{ad.name}")
        try:
            sub = ad.fetch_dataset()
        except adapters.AdapterError as e:
            print(f"  ⚠️  {e}")
            continue
        ds.bloggers.extend(sub.bloggers)
        ds.works.extend(sub.works)
        ds.fan_snapshots.extend(sub.fan_snapshots)
        audit_rows.extend(ad.audit)
    if not ds.bloggers:
        print("[crawl] 未采集到任何账号（可能网络受限或公开页需登录），已中止，不覆盖现有库")
        return 1
    stats = reconcile(ds, source="public_web")
    conn = store.connect(args.db)
    counts = store.save_dataset(conn, ds, source="public_web")
    store.set_meta(conn, "reconciliation", json.dumps(stats, ensure_ascii=False))
    store.set_meta(conn, "crawl_audit", json.dumps(audit_rows, ensure_ascii=False))
    store.audit(conn, "public_crawl",
                f"accounts={len(ds.bloggers)}; risk={sum(1 for r in audit_rows if r['category']=='risk')}")
    conn.close()
    print(_table(["实体", "行数"], [[k, f"{v:,}"] for k, v in counts.items()]))
    print("\n采集审计：")
    print(_table(["平台", "账号", "类别", "粉丝", "作品", "备注"],
                 [[r["platform"], r["handle"], "白名单" if r["category"] == "own" else "风险已确认",
                   f"{r['got_followers']:,}", r["works_found"],
                   r["note"] or "ok"] for r in audit_rows]))
    print("\n  🟡 公开页采集只有本次 1 个粉丝快照点，且无评论/商品/会员明细；"
          "若公开页需登录则粉丝数可能为空，已如实记录不伪造。")
    print("  ⚠️  请确保已确认白名单中竞品账号的风险；crawl_whitelist.json 请勿提交版本库。")
    return cmd_analyze(args)


def main() -> int:
    ap = argparse.ArgumentParser(description="Vibe Streamer Analytics CLI")
    ap.add_argument("command", nargs="?", default="all",
                    choices=["all", "demo", "analyze", "import", "validate", "template",
                             "verify", "selftest", "scopes", "oauth", "sync", "crawl"])
    ap.add_argument("directory", nargs="?", help="数据目录（import / validate / template）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--per-industry", type=int, default=6)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--whitelist", default="crawl_whitelist.json")
    ap.add_argument("--code", default="", help="OAuth 回调 code")
    args = ap.parse_args()

    handlers = {
        "demo": cmd_demo, "analyze": cmd_analyze, "import": cmd_import,
        "validate": cmd_validate, "template": cmd_template, "verify": cmd_verify,
        "selftest": cmd_selftest, "scopes": cmd_scopes,
        "oauth": cmd_oauth, "sync": cmd_sync, "crawl": cmd_crawl,
    }
    if args.command == "all":
        return cmd_demo(args) or cmd_analyze(args)
    if args.command in ("import", "validate") and not args.directory:
        d = ROOT / "import_template"
        if d.is_dir():
            args.directory = str(d)
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
