"""分析引擎核心：七大维度 + 统一指标口径 + 可信度标注。

原则：
  1. 所有比率使用「中位数 + 5/95 缩尾」，避免单个爆款把均值拉飞；
  2. 每个指标带 credibility 标签，估算值不参与对外结论的强断言；
  3. 官方接口拿不到的字段（完播率、真实 GMV、入会明细）明确列入 limitations，
     不用「预估模型」包装成可交付指标。
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime

from .adapters import NOT_AVAILABLE_VIA_OAUTH, SCOPE_CATALOG
from .nlp import analyze_comments
from .schema import CREDIBILITY, Dataset

WEIGHTS = {"广告价值": 0.40, "带货潜力": 0.35, "会员收入": 0.25}
METHODOLOGY = [
    ("互动率", "(点赞+评论+分享)/播放量；按博主取中位数，行业值取博主中位数", "derived"),
    ("7 日涨粉速率", "(当前粉丝-7日前粉丝)/7日前粉丝×100%", "derived"),
    ("涨粉转化效率", "近30日净增粉丝 / 近30日作品总播放量 × 10000（每万播放涨粉）", "derived"),
    ("有效评论率", "去表情/标点后≥4字的评论占比", "derived"),
    ("情感分", "领域词典+否定/程度规则，可选融合 jieba/SnowNLP，输出 0~1", "estimated"),
    ("橱窗 GMV 估算", "Σ(标价 × 平台销量标识区间中值)，不含退款/佣金/实际成交", "estimated"),
    ("变现综合评分", "0.40×广告价值 + 0.35×带货潜力 + 0.25×会员收入，全局 log 归一后映射 0~100", "estimated"),
    ("缩尾处理", "所有跨博主聚合比率先做 P5/P95 缩尾再取中位数", "derived"),
]
LIMITATIONS = list(NOT_AVAILABLE_VIA_OAUTH) + [
    "演示数据为固定随机种子合成的仿真样本，不代表任何真实账号",
    "情感分、GMV、变现评分属估算，仅支持同批次内横向比较，不可作绝对数值引用",
]


# ---------- 数值工具 ----------
def _median(xs):
    return round(statistics.median(xs), 6) if xs else 0.0


def _winsor(xs, lo=0.05, hi=0.95):
    if not xs:
        return []
    s = sorted(xs)
    a = s[int((len(s) - 1) * lo)]
    b = s[int((len(s) - 1) * hi)]
    return [min(max(x, a), b) for x in xs]


def _rate(a, b, nd=4):
    return round(a / b, nd) if b else 0.0


def _pct(a, b, nd=2):
    return round(a / b * 100, nd) if b else 0.0


def _lognorm(v):
    return math.log10(1 + max(0, v))


def _minmax_map(values: dict) -> dict:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        # 全无数据 / 全同值 → 该维度没有区分度，记 0 分而不是「中等的 50 分」
        return {k: 0.0 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _top(items, key, n=10, reverse=True):
    return sorted(items, key=key, reverse=reverse)[:n]


def _hour(iso: str) -> int:
    try:
        return int(iso[11:13])
    except (ValueError, IndexError):
        return 0


def _weekday(iso: str) -> int:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").weekday()
    except (ValueError, IndexError):
        return 0


# ---------- 博主级基础指标 ----------
def _blogger_metrics(ds: Dataset) -> list[dict]:
    works_by_b = defaultdict(list)
    products_by_b = defaultdict(list)
    comments_by_b = defaultdict(list)
    for w in ds.works:
        works_by_b[w.blogger_id].append(w)
    for p in ds.products:
        products_by_b[p.blogger_id].append(p)
    for c in ds.comments:
        comments_by_b[c.blogger_id].append(c)

    rows = []
    for b in ds.bloggers:
        ws = works_by_b[b.id]
        ps = products_by_b[b.id]
        cs = comments_by_b[b.id]
        irs = _winsor([w.interaction_rate for w in ws if w.views > 0])
        med_ir = _median(irs)
        views = sum(w.views for w in ws)
        gmv = sum(p.price * p.sold_display for p in ps)
        member_rev = b.member_count * b.member_price
        net30 = b.followers - b.followers_30d
        g7 = _pct(b.followers - b.followers_7d, b.followers_7d)
        g30 = _pct(net30, b.followers_30d)
        rows.append({
            "id": b.id, "nickname": b.nickname, "platform": b.platform,
            "industry": b.industry, "tier": b.tier, "followers": b.followers,
            "verified": bool(b.verified),
            "growth_7d": g7, "growth_30d": g30, "net_30d": net30,
            "interaction_rate": round(med_ir * 100, 3),
            "works": len(ws), "total_views": views, "total_likes": b.total_likes,
            "comments_collected": len(cs),
            "gmv_est": round(gmv, 2), "products": len(ps),
            "avg_price": round(statistics.mean([p.price for p in ps]), 1) if ps else 0,
            "conversion_per_10k": round(net30 / views * 10000, 2) if views else 0,
            "has_membership": bool(b.has_membership), "member_price": b.member_price,
            "member_count": b.member_count, "member_works": b.member_works,
            "member_rev_est": round(member_rev, 2),
            "_ad_raw": _lognorm(b.followers) * 0.6 + med_ir * 12,
            "_shop_raw": _lognorm(gmv),
            "_member_raw": _lognorm(member_rev),
            "_content_raw": med_ir * 100,
            "_fan_raw": _lognorm(max(0, net30)) + med_ir * 8,
        })
    # 全局 log 归一 → 0~100
    for key in ("_ad_raw", "_shop_raw", "_member_raw", "_content_raw", "_fan_raw"):
        m = _minmax_map({r["id"]: r[key] for r in rows})
        short = {"_ad_raw": "ad", "_shop_raw": "shop", "_member_raw": "member",
                 "_content_raw": "content", "_fan_raw": "fan"}[key]
        for r in rows:
            r[short] = round(m[r["id"]] * 100, 1)
    for r in rows:
        r["monetization_score"] = round(
            WEIGHTS["广告价值"] * r["ad"] + WEIGHTS["带货潜力"] * r["shop"]
            + WEIGHTS["会员收入"] * r["member"], 1)
        for k in ("_ad_raw", "_shop_raw", "_member_raw", "_content_raw", "_fan_raw"):
            r.pop(k, None)
    return rows


# ---------- 七大维度 ----------
def _industry(rows) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[r["industry"]].append(r)
    out = []
    for name, rs in by.items():
        tiers = Counter(r["tier"] for r in rs)
        out.append({
            "name": name, "bloggers": len(rs),
            "followers": sum(r["followers"] for r in rs),
            "avg_interaction": _median(_winsor([r["interaction_rate"] for r in rs])),
            "growth_7d": _median(_winsor([r["growth_7d"] for r in rs])),
            "gmv_est": round(sum(r["gmv_est"] for r in rs), 2),
            "monetization_score": round(statistics.mean(r["monetization_score"] for r in rs), 1),
            "tier_mix": {t: tiers.get(t, 0) for t in ("头部", "腰部", "尾部")},
        })
    return sorted(out, key=lambda x: -x["followers"])


def _rankings(rows) -> dict:
    def slim(r, field, label):
        return {"id": r["id"], "nickname": r["nickname"], "platform": r["platform"],
                "industry": r["industry"], "followers": r["followers"],
                "value": r[field], "label": label(r)}
    return {
        "growth": [slim(r, "growth_7d", lambda x: f"+{x['growth_7d']}%")
                   for r in _top(rows, lambda x: x["growth_7d"], 10)],
        "risk": [slim(r, "growth_30d", lambda x: f"{x['growth_30d']}%")
                 for r in _top([r for r in rows if r["growth_30d"] < 0],
                               lambda x: x["growth_30d"], 10, reverse=False)],
        "interaction": [slim(r, "interaction_rate", lambda x: f"{x['interaction_rate']}%")
                        for r in _top(rows, lambda x: x["interaction_rate"], 10)],
        "monetization": [slim(r, "monetization_score", lambda x: f"{x['monetization_score']} 分")
                         for r in _top(rows, lambda x: x["monetization_score"], 10)],
    }


def _works(ds: Dataset) -> dict:
    ws = [w for w in ds.works if w.views > 0]
    hour_counts = [0] * 24
    heat = [[0] * 24 for _ in range(7)]
    by_hour_ir = defaultdict(list)
    by_tag = defaultdict(list)
    by_cover = defaultdict(list)
    by_slot = defaultdict(list)
    dur_bins = Counter()
    dur_ir = defaultdict(list)

    for w in ws:
        h, wd = _hour(w.publish_at), _weekday(w.publish_at)
        hour_counts[h] += 1
        heat[wd][h] += 1
        by_hour_ir[h].append(w.interaction_rate)
        by_tag[w.tag].append(w.interaction_rate)
        by_cover[w.cover_style].append(w.interaction_rate)
        by_slot[(wd, h)].append(w.interaction_rate)
        d = w.duration_s
        band = "<30s" if d < 30 else ("30-60s" if d < 60 else ("60-90s" if d < 90 else "≥90s"))
        dur_bins[band] += 1
        dur_ir[band].append(w.interaction_rate)

    irs = sorted(w.interaction_rate for w in ws)
    cut = irs[max(0, int(len(irs) * 0.9) - 1)] if irs else 0
    viral = [w for w in ws if w.interaction_rate >= cut]
    # 按时段聚合（每桶样本量充足）而非「星期×小时」细格：后者在百级样本下
    # 每格个位数作品，排出来的「最佳时段」几乎全是噪声。
    slots = [{"weekday": -1, "hour": h, "samples": len(v),
              "avg_interaction": round(statistics.mean(_winsor(v)) * 100, 3)}
             for h, v in by_hour_ir.items() if len(v) >= 10]
    return {
        "total": len(ws),
        "hour_dist": [{"hour": h, "count": hour_counts[h],
                       "avg_interaction": round(statistics.mean(_winsor(by_hour_ir[h])) * 100, 3)
                       if by_hour_ir[h] else 0} for h in range(24)],
        "heat": heat,
        "tag_dist": sorted([{"name": k, "count": len(v),
                             "avg_interaction": round(statistics.mean(_winsor(v)) * 100, 3)}
                            for k, v in by_tag.items()], key=lambda x: -x["count"]),
        "duration_bins": [{"label": b, "count": dur_bins[b],
                           "avg_interaction": round(statistics.mean(_winsor(dur_ir[b])) * 100, 3)
                           if dur_ir[b] else 0}
                          for b in ("<30s", "30-60s", "60-90s", "≥90s")],
        "cover_perf": sorted([{"name": k, "count": len(v),
                               "avg_interaction": round(statistics.mean(_winsor(v)) * 100, 3)}
                              for k, v in by_cover.items()], key=lambda x: -x["avg_interaction"]),
        "best_slots": _top(slots, lambda x: x["avg_interaction"], 6),
        "viral": {
            "count": len(viral),
            "threshold_interaction": round(cut * 100, 3),
            "avg_duration": round(statistics.mean([w.duration_s for w in viral]), 1) if viral else 0,
            "top_cover": Counter(w.cover_style for w in viral).most_common(3),
            "top_tag": Counter(w.tag for w in viral).most_common(3),
            "top_hour": Counter(_hour(w.publish_at) for w in viral).most_common(4),
            "samples": [{"title": w.title, "interaction": round(w.interaction_rate * 100, 2),
                         "views": w.views, "cover": w.cover_style}
                        for w in _top(viral, lambda x: x.interaction_rate, 6)],
        },
        "cadence_per_week": round(len(ws) / max(1, len({w.blogger_id for w in ws})) / 8, 2),
    }


def _fans(ds: Dataset, rows) -> dict:
    by_date = defaultdict(int)
    by_ind_date = defaultdict(lambda: defaultdict(int))
    ind_of = {b.id: b.industry for b in ds.bloggers}
    for s in ds.fan_snapshots:
        by_date[s.date] += s.followers
        by_ind_date[ind_of.get(s.blogger_id, "未知")][s.date] += s.followers
    series = [{"date": d, "value": v} for d, v in sorted(by_date.items())]
    inflections = []
    for i in range(1, len(series)):
        delta = series[i]["value"] - series[i - 1]["value"]
        inflections.append({"date": series[i]["date"], "delta": delta,
                            "pct": _pct(delta, series[i - 1]["value"], 3)})
    return {
        "series": series,
        "by_industry": sorted([{"name": k, "series": [{"date": d, "value": v}
                                                      for d, v in sorted(inner.items())]}
                               for k, inner in by_ind_date.items()], key=lambda x: x["name"]),
        "tiers": [{"name": t, "count": sum(1 for r in rows if r["tier"] == t),
                   "followers": sum(r["followers"] for r in rows if r["tier"] == t)}
                  for t in ("头部", "腰部", "尾部")],
        "conversion": [{"id": r["id"], "nickname": r["nickname"], "industry": r["industry"],
                        "value": r["conversion_per_10k"]}
                       for r in _top(rows, lambda x: x["conversion_per_10k"], 10)],
        "inflections": _top(inflections, lambda x: abs(x["delta"]), 5),
    }


def _shop(ds: Dataset, rows) -> dict:
    by_cat = defaultdict(list)
    by_price = Counter()
    for p in ds.products:
        by_cat[p.category].append(p)
        band = "<¥50" if p.price < 50 else ("¥50-150" if p.price < 150 else
               ("¥150-300" if p.price < 300 else "≥¥300"))
        by_price[band] += 1
    by_ind = defaultdict(float)
    ind_of = {r["id"]: r["industry"] for r in rows}
    for p in ds.products:
        by_ind[ind_of.get(p.blogger_id, "未知")] += p.price * p.sold_display
    return {
        "total_products": len(ds.products),
        "gmv_est": round(sum(p.price * p.sold_display for p in ds.products), 2),
        "categories": sorted([{"name": k, "count": len(v),
                               "gmv_est": round(sum(p.price * p.sold_display for p in v), 2),
                               "avg_price": round(statistics.mean(p.price for p in v), 1)}
                              for k, v in by_cat.items()], key=lambda x: -x["gmv_est"]),
        "price_bands": [{"name": b, "count": by_price[b]}
                        for b in ("<¥50", "¥50-150", "¥150-300", "≥¥300")],
        "gmv_by_industry": sorted([{"name": k, "value": round(v, 2)}
                                   for k, v in by_ind.items()], key=lambda x: -x["value"]),
        "top_products": [{"name": p.name, "category": p.category, "price": p.price,
                          "sold_display": p.sold_display, "rating": p.rating,
                          "gmv_est": round(p.price * p.sold_display, 2)}
                         for p in _top(ds.products,
                                       lambda x: x.price * x.sold_display, 8)],
    }


def _membership(ds: Dataset, rows) -> dict:
    ms = [b for b in ds.bloggers if b.has_membership]
    bands = Counter()
    for b in ms:
        p = b.member_price
        band = "<¥10" if p < 10 else ("¥10-20" if p < 20 else "≥¥20")
        bands[band] += 1
    by_platform = defaultdict(lambda: {"accounts": 0, "members": 0, "revenue_est": 0.0})
    for b in ds.bloggers:
        d = by_platform[b.platform]
        d["accounts"] += 1
        if b.has_membership:
            d["members"] += b.member_count
            d["revenue_est"] += b.member_count * b.member_price
    penetration = [{"id": b.id, "nickname": b.nickname, "industry": b.industry,
                    "rate": _pct(b.member_count, b.followers, 3), "price": b.member_price,
                    "members": b.member_count}
                   for b in ms]
    return {
        "enabled_accounts": len(ms),
        "enabled_rate": _pct(len(ms), len(ds.bloggers)),
        "total_members": sum(b.member_count for b in ms),
        "revenue_est": round(sum(b.member_count * b.member_price for b in ms), 2),
        "price_bands": [{"name": b, "count": bands[b]} for b in ("<¥10", "¥10-20", "≥¥20")],
        "by_platform": [{"name": k, **v, "revenue_est": round(v["revenue_est"], 2)}
                        for k, v in by_platform.items()],
        "penetration": _top(penetration, lambda x: x["rate"], 10),
        "content_freq": [{"id": b.id, "nickname": b.nickname, "works": b.member_works,
                          "price": b.member_price} for b in _top(ms, lambda x: x.member_works, 8)],
    }


def _monetization(rows, industry) -> dict:
    dims = ("ad", "shop", "member", "content", "fan")
    labels = {"ad": "广告价值", "shop": "带货潜力", "member": "会员收入",
              "content": "内容效率", "fan": "粉丝质量"}
    top10 = _top(rows, lambda x: x["monetization_score"], 10)
    return {
        "dimensions": list(labels.values()),
        "radar": [{"name": labels[d],
                   "top": round(statistics.mean(r[d] for r in top10), 1),
                   "all": round(statistics.mean(r[d] for r in rows), 1)} for d in dims],
        "top10": top10,
        "weights": WEIGHTS,
        "industry_efficiency": sorted([{"name": i["name"], "score": i["monetization_score"],
                                        "gmv_est": i["gmv_est"]}
                                       for i in industry], key=lambda x: -x["score"]),
    }


def _data_quality(ds: Dataset, report: dict) -> dict:
    b_with_works = len({w.blogger_id for w in ds.works})
    w_with_comments = len({c.work_id for c in ds.comments})
    b_with_snaps = len({s.blogger_id for s in ds.fan_snapshots})
    no_views = sum(1 for w in ds.works if w.views <= 0)
    no_30d = sum(1 for b in ds.bloggers if not b.followers_30d)
    no_7d = sum(1 for b in ds.bloggers if not b.followers_7d)
    warnings = []
    if no_views:
        warnings.append(f"{no_views} 条作品播放量为 0，已从互动率计算中剔除")
    if no_30d:
        warnings.append(f"{no_30d} 位博主缺少 30 日前粉丝数，30 日增速与涨粉转化效率按不可用处理")
    if no_7d:
        warnings.append(f"{no_7d} 位博主缺少 7 日前粉丝数，7 日增速按不可用处理")
    if not ds.products:
        warnings.append("无橱窗商品台账，电商维度为空态（GMV 估算不参与评分）")
    if not ds.comments:
        warnings.append("无评论台账，评论口碑维度为空态")
    if len({s.date for s in ds.fan_snapshots}) < 7:
        warnings.append("粉丝快照不足 7 个日期点，增长曲线只反映可用观测点")
    if report["comments"]["avg_sentiment"] > 0.75 or report["comments"]["avg_sentiment"] < 0.25:
        warnings.append("情感分布明显偏斜，建议抽检词典命中情况")
    if ds.bloggers and ds.bloggers[0].source == "synthetic_demo":
        warnings.append("当前为合成演示数据，所有结论仅用于验证分析口径与界面")
    return {
        "coverage": {
            "blogger_has_works": _pct(b_with_works, len(ds.bloggers), 1),
            "work_has_comments": _pct(w_with_comments, len(ds.works), 1),
            "blogger_has_snapshots": _pct(b_with_snaps, len(ds.bloggers), 1),
        },
        "missing_views": no_views,
        "warnings": warnings,
        "limitations": LIMITATIONS,
    }


def build_report(ds: Dataset, source: str = "synthetic_demo", seed: int | None = None) -> dict:
    if not ds.bloggers:
        raise ValueError("空数据集：请先用适配器写入数据")
    rows = _blogger_metrics(ds)
    industry = _industry(rows)
    works = _works(ds)
    fans = _fans(ds, rows)
    comments = analyze_comments(ds.comments)
    shop = _shop(ds, rows)
    membership = _membership(ds, rows)
    monetization = _monetization(rows, industry)
    rankings = _rankings(rows)

    total_followers = sum(b.followers for b in ds.bloggers)
    net30 = sum(b.followers - b.followers_30d for b in ds.bloggers)
    kpi = [
        {"key": "bloggers", "label": "监控博主", "value": len(ds.bloggers), "unit": "位",
         "sub": f"覆盖 {len(industry)} 个行业", "credibility": "observed",
         "spark": []},
        {"key": "followers", "label": "合计粉丝", "value": total_followers, "unit": "",
         "sub": f"30 日净增 {net30 / 10000:+,.1f} 万", "credibility": "observed",
         "spark": [x["value"] for x in fans["series"]]},
        {"key": "interaction", "label": "中位互动率", "value": _median(_winsor([r["interaction_rate"] for r in rows])),
         "unit": "%", "sub": "P5/P95 缩尾后中位数", "credibility": "derived", "spark": []},
        {"key": "works", "label": "分析作品", "value": len(ds.works), "unit": "条",
         "sub": f"人均 {round(len(ds.works) / len(ds.bloggers), 1)} 条", "credibility": "observed", "spark": []},
        {"key": "comments", "label": "评论样本", "value": len(ds.comments), "unit": "条",
         "sub": f"正面占比 {_pct(comments['sentiment_dist']['正面'], len(ds.comments), 1)}%",
         "credibility": "observed", "spark": []},
        {"key": "gmv", "label": "橱窗 GMV 估算", "value": shop["gmv_est"], "unit": "¥",
         "sub": "销量标识推算 · 低置信度", "credibility": "estimated", "spark": []},
    ]

    report = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_source": source, "is_demo": source == "synthetic_demo",
            "seed": seed, "counts": ds.counts(), "credibility": CREDIBILITY,
            "methodology": [{"name": a, "formula": b, "level": c} for a, b, c in METHODOLOGY],
            "scope_catalog": SCOPE_CATALOG,
            "version": "2.0.0",
        },
        "kpi": kpi, "industry": industry, "rankings": rankings,
        "works": works, "fans": fans, "comments": comments,
        "shop": shop, "membership": membership, "monetization": monetization,
        "bloggers": rows, "insights": [],
    }
    report["data_quality"] = _data_quality(ds, report)
    from .insights import generate_insights
    report["insights"] = generate_insights(report)
    return report
