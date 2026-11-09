"""规则化洞察引擎：无 LLM 也能出结论，且每条结论都带可核对的证据数字。

可插拔：设置环境变量 STREAMER_LLM_ENDPOINT 后可把 insights 作为 prompt 上下文
交给 DeepSeek / 通义等模型做二次润色，但事实与数字仍以本模块输出为准。
"""
from __future__ import annotations

import statistics


def _fmt(v, unit="", nd=1):
    if isinstance(v, float):
        return f"{v:,.{nd}f}{unit}"
    return f"{v:,}{unit}" if isinstance(v, int) else f"{v}{unit}"


def generate_insights(report: dict) -> list[dict]:
    out = []

    def add(level, dimension, title, text, evidence):
        out.append({"level": level, "dimension": dimension, "title": title,
                    "text": text, "evidence": evidence})

    ind = report["industry"]
    works = report["works"]
    fans = report["fans"]
    comments = report["comments"]
    shop = report["shop"]
    membership = report["membership"]
    monet = report["monetization"]
    rows = report["bloggers"]

    # 1 行业集中度
    if ind:
        top = ind[0]
        total = sum(i["followers"] for i in ind)
        share = top["followers"] / total * 100 if total else 0
        add("info", "行业榜", f"{top['name']}占据粉丝规模首位",
            f"该行业 {top['bloggers']} 位博主合计 {_fmt(top['followers']/10000, ' 万', 1)} 粉丝，"
            f"占全样本 {share:.1f}%；中位互动率 {top['avg_interaction']:.2f}%。"
            "建议结合行业基准判断是整体红利还是头部拉动。",
            f"followers={top['followers']:,} / total={total:,}")

    # 2 高增长
    growth = report["rankings"]["growth"]
    if growth:
        g = growth[0]
        add("opportunity", "增长", f"{g['nickname']} 7 日增速领先",
            f"{g['industry']}·{g['platform']}，7 日涨粉 {g['value']:.2f}%，"
            f"当前 {_fmt(g['followers']/10000, ' 万', 1)} 粉丝。建议核对其近 7 日作品，"
            "确认增速由爆款还是投放带来，再决定是否纳入对标池。",
            f"growth_7d={g['value']}%")

    # 3 掉粉预警
    risk = report["rankings"]["risk"]
    if risk:
        names = "、".join(r["nickname"] for r in risk[:3])
        worst = risk[0]
        add("risk", "预警", f"{len(risk)} 位博主 30 日净掉粉",
            f"其中 {names} 等最明显，最低 {worst['value']:.2f}%。"
            "优先检查发布频次、内容标签漂移与负面评论占比三项，避免直接归因为平台限流。",
            f"worst_growth_30d={worst['value']}%")

    # 4 最佳发布时段
    slots = works["best_slots"]
    if slots:
        s = slots[0]
        add("opportunity", "作品", f"每日 {s['hour']:02d}:00 前后是高效时段",
            f"该时段样本 {s['samples']} 条作品，平均互动率 {s['avg_interaction']:.2f}%，"
            "在 24 个时段中排首位（已剔除样本不足 10 条的时段）。"
            "注意这是相关性而非因果，建议做 2 周 A/B 再固化排期。",
            f"samples={s['samples']}, avg_interaction={s['avg_interaction']}%")

    # 5 爆款特征
    viral = works["viral"]
    if viral["top_cover"]:
        cover, n = viral["top_cover"][0]
        add("info", "作品", f"高互动作品更常用「{cover}」封面",
            f"Top 10% 高互动作品共 {viral['count']} 条，其中 {n} 条为{cover}，"
            f"平均时长 {viral['avg_duration']:.0f}s；高频标签："
            + "、".join(t for t, _ in viral["top_tag"][:3]) + "。",
            f"viral_threshold={viral['threshold_interaction']}%")

    # 6 评论情感
    dist = comments["sentiment_dist"]
    total_c = max(1, comments["total"])
    neg_pct = dist["负面"] / total_c * 100
    add("risk" if neg_pct > 20 else "info", "评论",
        f"负面评论占比 {neg_pct:.1f}%",
        f"平均情感分 {comments['avg_sentiment']:.3f}，有效评论率 "
        f"{comments['effective_rate']*100:.1f}%，博主回复率 {comments['reply_rate']*100:.1f}%。"
        + ("负面偏高，建议先定位争议作品再结论。" if neg_pct > 20 else "整体口碑平稳。"),
        f"engine={comments['engine']}")
    if comments["controversies"]:
        c = comments["controversies"][0]
        add("risk", "评论", "存在集中负面讨论的作品",
            f"作品 {c['work_id']} 采集到 {c['negative_count']} 条负面评论，"
            f"高频话题：{'、'.join(c['topics']) or '无显著词'}。示例：{c['sample']}",
            f"negative_count={c['negative_count']}")

    # 7 电商
    if shop["categories"]:
        cat = shop["categories"][0]
        add("info", "橱窗", f"{cat['name']}贡献最高估算 GMV",
            f"{cat['count']} 个 SKU，均价 ¥{cat['avg_price']}，估算 GMV ¥{_fmt(cat['gmv_est'], '', 0)}。"
            "该数字基于平台销量标识区间中值，不含退款与佣金，只能用于同批次横向排序。",
            f"gmv_est={cat['gmv_est']}")

    # 8 会员
    if membership["enabled_accounts"]:
        add("opportunity", "会员", f"会员开通率 {membership['enabled_rate']:.1f}%",
            f"{membership['enabled_accounts']} 个账号开通会员，覆盖 "
            f"{_fmt(membership['total_members']/10000, ' 万', 2)} 人，"
            f"月收入估算 ¥{_fmt(membership['revenue_est'], '', 0)}。"
            "入会转化率按会员数/粉丝数近似，真实值需达人后台导出。",
            f"accounts={membership['enabled_accounts']}")

    # 9 变现
    if monet["top10"]:
        t = monet["top10"][0]
        med = statistics.median(r["monetization_score"] for r in rows)
        add("info", "变现", f"{t['nickname']} 变现综合评分 {t['monetization_score']} 分",
            f"全样本中位 {med:.1f} 分。评分权重：广告 {int(monet['weights']['广告价值']*100)}% / "
            f"带货 {int(monet['weights']['带货潜力']*100)}% / 会员 {int(monet['weights']['会员收入']*100)}%，"
            "属于排序型指标而非收入预测。",
            f"top={t['monetization_score']}, median={med:.1f}")

    # 10 数据质量
    cov = report["data_quality"]["coverage"]
    add("info", "数据质量", "口径与可信度自检",
        f"作品覆盖率 {cov['blogger_has_works']:.1f}%、评论覆盖率 {cov['work_has_comments']:.1f}%、"
        f"粉丝快照覆盖率 {cov['blogger_has_snapshots']:.1f}%。"
        f"完播率、真实 GMV、第三方达人画像均无合规数据源，本报表未编造这些指标。",
        "credibility=observed/derived/estimated 三态标注")
    return out
