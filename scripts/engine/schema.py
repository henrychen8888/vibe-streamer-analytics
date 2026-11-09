"""数据契约：所有指标口径的唯一来源。

可信度分级（credibility）：
  observed  实测：平台授权接口 / 自有导出数据直接给出的字段
  derived   派生：由实测字段经确定性公式计算（互动率、增速等）
  estimated 估算：由代理指标推断（GMV、入会转化、完播率等），禁止当作事实使用
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from typing import Iterable

CREDIBILITY = {
    "observed": {"label": "实测", "hint": "授权接口 / 自有导出原始数据"},
    "derived": {"label": "派生", "hint": "由实测字段按固定公式计算"},
    "estimated": {"label": "估算", "hint": "代理指标推断，仅供横向比较，不可作为事实引用"},
}


@dataclass
class Blogger:
    id: str
    platform: str                 # 抖音 / 小红书
    nickname: str
    industry: str
    followers: int = 0
    followers_7d: int = 0
    followers_30d: int = 0
    total_likes: int = 0
    verified: int = 0
    # 会员体系（抖音）
    has_membership: int = 0
    member_price: float = 0.0
    member_count: int = 0
    member_works: int = 0
    # 数据血缘
    source: str = "official_api"
    collected_at: str = ""

    @property
    def tier(self) -> str:
        if self.followers >= 1_000_000:
            return "头部"
        if self.followers >= 100_000:
            return "腰部"
        return "尾部"


@dataclass
class Work:
    id: str
    blogger_id: str
    title: str
    publish_at: str               # ISO: YYYY-MM-DDTHH:MM
    duration_s: int = 0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    cover_style: str = "真人出镜"
    tag: str = ""                 # 自动打标结果
    source: str = "official_api"

    @property
    def interaction_rate(self) -> float:
        return (self.likes + self.comments + self.shares) / self.views if self.views else 0.0


@dataclass
class Comment:
    id: str
    work_id: str
    blogger_id: str
    content: str
    likes: int = 0
    created_at: str = ""
    replied: int = 0              # 博主是否回复
    sentiment: float = 0.5        # 由 nlp 计算后回填
    source: str = "official_api"


@dataclass
class Product:
    id: str
    blogger_id: str
    name: str
    category: str
    price: float = 0.0
    sold_display: int = 0         # 平台销量标识（区间中值），非真实成交
    rating: float = 0.0
    source: str = "official_api"


@dataclass
class FanSnapshot:
    blogger_id: str
    date: str                     # YYYY-MM-DD
    followers: int
    source: str = "official_api"


@dataclass
class Dataset:
    bloggers: list[Blogger] = field(default_factory=list)
    works: list[Work] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)
    fan_snapshots: list[FanSnapshot] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "bloggers": len(self.bloggers),
            "works": len(self.works),
            "comments": len(self.comments),
            "products": len(self.products),
            "fan_snapshots": len(self.fan_snapshots),
        }


def row_to(cls, row: dict):
    """从 dict 安全构造 dataclass，忽略未知字段。"""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in row.items() if k in names})


def to_dicts(items: Iterable) -> list[dict]:
    return [asdict(x) for x in items]
