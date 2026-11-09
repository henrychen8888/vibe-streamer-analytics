"""真实数据入库前的确定性回填与脱敏。

严格边界：只使用已有观测值做回填（如用粉丝快照补 7 日前粉丝数），
绝不生成任何不存在的观测点。无法回填的字段保持缺省，由看板降级展示并在
data_quality 中告警 —— 这比「用均值补齐」诚实。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from .schema import Dataset

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WECHAT = re.compile(r"(?:微信|vx|VX|weixin|wechat)\s*[:：]?\s*[A-Za-z0-9_-]{6,20}")
PATTERNS = [(PHONE, "[手机号已脱敏]"), (IDCARD, "[证件号已脱敏]"),
            (EMAIL, "[邮箱已脱敏]"), (WECHAT, "[联系方式已脱敏]")]


def desensitize(text: str) -> tuple[str, int]:
    if not text:
        return text or "", 0
    s = str(text)
    hits = 0
    for pattern, repl in PATTERNS:
        s, n = pattern.subn(repl, s)
        hits += n
    return s, hits


def reconcile(ds: Dataset, source: str = "authorized_export") -> dict:
    """返回回填统计；任何一条回填都可追溯到具体来源字段。"""
    stats = {"followers_filled": 0, "followers_7d_filled": 0, "followers_30d_filled": 0,
             "total_likes_filled": 0, "source_filled": 0, "collected_at_filled": 0,
             "desensitized_fields": 0, "desensitized_hits": 0,
             "growth_7d_unavailable": 0, "growth_30d_unavailable": 0, "notes": []}

    snaps: dict[str, list] = defaultdict(list)
    for s in ds.fan_snapshots:
        snaps[s.blogger_id].append(s)
    for lst in snaps.values():
        lst.sort(key=lambda x: x.date)

    works: dict[str, list] = defaultdict(list)
    for w in ds.works:
        works[w.blogger_id].append(w)

    all_dates = sorted({s.date for s in ds.fan_snapshots})

    def _pick(lst, target: str):
        """取日期不晚于 target 且与 target 最接近的一条快照。"""
        cand = [s for s in lst if s.date <= target]
        return cand[-1] if cand else None

    for b in ds.bloggers:
        lst = snaps.get(b.id, [])
        if not b.source:
            b.source = source
            stats["source_filled"] += 1
        if not b.collected_at:
            b.collected_at = lst[-1].date if lst else date.today().isoformat()
            stats["collected_at_filled"] += 1

        if lst:
            if not b.followers:
                b.followers = lst[-1].followers
                stats["followers_filled"] += 1
            latest = date.fromisoformat(lst[-1].date)
            for days, attr, key in ((7, "followers_7d", "followers_7d_filled"),
                                    (30, "followers_30d", "followers_30d_filled")):
                if not getattr(b, attr):
                    snap = _pick(lst, (latest - timedelta(days=days)).isoformat())
                    if snap and snap.followers:
                        setattr(b, attr, snap.followers)
                        stats[key] += 1
        if not b.total_likes and works.get(b.id):
            b.total_likes = sum(w.likes for w in works[b.id])
            stats["total_likes_filled"] += 1
        clean, n = desensitize(b.nickname)
        if n:
            b.nickname = clean
            stats["desensitized_fields"] += 1
            stats["desensitized_hits"] += n

    stats["growth_7d_unavailable"] = sum(1 for b in ds.bloggers if not b.followers_7d)
    stats["growth_30d_unavailable"] = sum(1 for b in ds.bloggers if not b.followers_30d)

    if stats["growth_30d_unavailable"]:
        stats["notes"].append(
            f"{stats['growth_30d_unavailable']} 位博主缺少 30 日前粉丝数，"
            "30 日增速与涨粉转化效率按不可用处理（不补均值、不编造历史）")
    if stats["growth_7d_unavailable"]:
        stats["notes"].append(
            f"{stats['growth_7d_unavailable']} 位博主缺少 7 日前粉丝数，7 日增速按不可用处理")

    for w in ds.works:
        if not w.source:
            w.source = source
        clean, n = desensitize(w.title)
        if n:
            w.title = clean
            stats["desensitized_fields"] += 1
            stats["desensitized_hits"] += n
    for c in ds.comments:
        if not c.source:
            c.source = source
        clean, n = desensitize(c.content)
        if n:
            c.content = clean
            stats["desensitized_fields"] += 1
            stats["desensitized_hits"] += n
    for p in ds.products:
        if not p.source:
            p.source = source
    for s in ds.fan_snapshots:
        if not s.source:
            s.source = source

    if not all_dates:
        stats["notes"].append("无粉丝快照：增长曲线为空态，其余维度正常")
    if stats["desensitized_hits"]:
        stats["notes"].append(f"已对 {stats['desensitized_hits']} 处疑似个人信息做脱敏替换")
    return stats
