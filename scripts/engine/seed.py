"""可复现的演示数据生成器（random.Random 固定种子）。

用途：在没有平台授权数据时把整条分析链路跑通、验证口径、演示 UI。
所有数据均为合成数据，dashboard 会强制标注「演示数据」，禁止外发为真实结论。
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .schema import Blogger, Comment, Dataset, FanSnapshot, Product, Work

BASE_DATE = date(2026, 11, 9)
DEFAULT_SEED = 20261109

INDUSTRIES = {
    "美妆": {"items": ["粉底液", "口红", "精华", "防晒", "眼影"], "tags": ["测评", "教程", "好物", "避坑"], "heat": 1.25},
    "美食": {"items": ["空气炸锅", "酱料", "家常菜", "烘焙", "探店"], "tags": ["教程", "探店", "食谱", "开箱"], "heat": 1.10},
    "科技数码": {"items": ["耳机", "键盘", "手机", "显示器", "充电宝"], "tags": ["测评", "开箱", "对比", "避坑"], "heat": 0.95},
    "穿搭": {"items": ["外套", "牛仔裤", "通勤包", "球鞋", "配饰"], "tags": ["搭配", "开箱", "好物", "教程"], "heat": 1.15},
    "知识教育": {"items": ["方法论", "书单", "效率工具", "课程", "笔记"], "tags": ["干货", "教程", "拆解", "复盘"], "heat": 0.85},
    "母婴": {"items": ["辅食", "纸尿裤", "推车", "绘本", "安抚玩具"], "tags": ["测评", "好物", "避坑", "经验"], "heat": 1.00},
    "健身": {"items": ["训练计划", "蛋白粉", "瑜伽垫", "跑鞋", "拉伸"], "tags": ["教程", "打卡", "测评", "干货"], "heat": 0.90},
    "家居": {"items": ["收纳", "香薰", "灯具", "床品", "小家电"], "tags": ["好物", "开箱", "改造", "避坑"], "heat": 1.05},
}
PLATFORMS = ("抖音", "抖音", "抖音", "小红书")
NICK_A = ["阿", "小", "大", "老", "", "", "一只", "半糖", "柠檬", "栗子", "南山", "北屿", "元气", "硬核", "温柔"]
NICK_B = ["栗", "柚", "橙", "森", "鹿", "丸", "可可", "同学", "研究所", "实验室", "日记", "手记", "清单", "局", "社"]
COVERS = ["真人出镜", "产品特写", "文字封面", "前后对比", "场景实拍"]
CATEGORIES = ["美妆护肤", "食品饮料", "数码配件", "服饰鞋包", "家居日用", "图书教育", "母婴用品", "运动户外"]
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 3, 5, 6, 6, 7, 8, 8, 7, 7, 8, 9, 11, 14, 16, 15, 12, 7, 3]

POSITIVE = [
    "这个{item}真的好用，已下单，性价比拉满 👍", "讲得清楚，干货满满，收藏了",
    "回购第三次了，质感一直在线", "比想象中好太多，显白又显瘦 ✨",
    "主播很真诚，不硬广，关注了", "跟着做成功了，太实用了 💯",
    "催更！这系列请一直做下去", "包装和物流都很快，客服也靠谱",
    "终于有人把这事讲明白了，学到了", "平替里的天花板，学生党狂喜",
    "画质和剪辑都好高级，看得很舒服", "会员专属那期值回票价了",
]
NEUTRAL = [
    "求链接", "多少钱呀", "什么时候更新下一期", "已关注，蹲一个教程",
    "这个适合油皮吗", "有没有平价一点的推荐", "坐标北方能用吗",
    "第几秒提到的那个来着", "蹲一个后续测评", "收藏了，晚点看",
]
NEGATIVE = [
    "翻车了，和直播间说的不一样，准备退款", "感觉有点智商税，溢价太明显",
    "客服半天不回，售后体验差 🙄", "这期有点水，广告太硬了",
    "听不懂，节奏太快了", "买了踩雷，包装还是坏的 😤",
    "最近内容越来越敷衍，取关了", "价格比别家贵不少，不太值",
    "讲得不太清楚，能不能再细一点", "疑似割韭菜，观望一下",
]


def _iso(d: date, hour: int, minute: int = 0) -> str:
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}"


def generate(seed: int = DEFAULT_SEED, bloggers_per_industry: int = 6) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()
    tiers = [1_800_000, 620_000, 340_000, 78_000, 42_000, 16_000]

    for ind_idx, (industry, meta) in enumerate(INDUSTRIES.items()):
        for i in range(bloggers_per_industry):
            bid = f"B{ind_idx + 1:02d}{i + 1:02d}"
            platform = PLATFORMS[(ind_idx + i) % len(PLATFORMS)]
            base_followers = int(tiers[i] * meta["heat"] * rng.uniform(0.72, 1.35))
            drift = rng.uniform(-0.0028, 0.0065)          # 日均自然增速
            if i == 4 and ind_idx in (2, 6):
                drift = rng.uniform(-0.006, -0.003)        # 制造掉粉样本
            nick = rng.choice(NICK_A) + rng.choice(NICK_B) + rng.choice(["", "", "呀", "酱"])
            has_member = int(platform == "抖音" and rng.random() < 0.68)

            # 30 天粉丝快照
            snaps: list[FanSnapshot] = []
            cur = float(base_followers)
            viral_days: set[int] = set()
            for day in range(30, -1, -1):
                d = BASE_DATE - timedelta(days=day)
                daily = drift + rng.gauss(0, 0.0016)
                cur = max(100, cur * (1 + daily))
                snaps.append(FanSnapshot(bid, d.isoformat(), int(cur)))

            n_works = rng.randint(12, 18)
            works: list[Work] = []
            for w in range(n_works):
                days_ago = rng.randint(1, 58)
                d = BASE_DATE - timedelta(days=days_ago)
                hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
                is_viral = rng.random() < 0.11
                cover = rng.choices(COVERS, weights=[38, 22, 14, 16, 10], k=1)[0]
                reach = rng.uniform(0.28, 1.9) * (1.25 if cover == "真人出镜" else 1.0)
                views = int(base_followers * reach * (rng.uniform(4.0, 9.0) if is_viral else 1.0))
                if platform == "小红书":
                    views = int(views * 0.72)
                likes = int(views * rng.uniform(0.028, 0.085))
                comments_n = int(views * rng.uniform(0.0025, 0.009))
                shares = int(views * rng.uniform(0.0018, 0.007))
                item = rng.choice(meta["items"])
                title = rng.choice([
                    f"{item}到底值不值得买？{rng.choice(['实测', '拆解', '避坑指南'])}",
                    f"{rng.choice(['3 个', '5 个', '别再'])}{item}{rng.choice(['冷知识', '误区', '用法'])}",
                    f"用了 30 天{item}，说点真话",
                    f"{item}{rng.choice(['横向测评', '开箱', '保姆级教程'])}｜第 {w + 1} 期",
                ])
                wid = f"{bid}-W{w + 1:02d}"
                works.append(Work(
                    id=wid, blogger_id=bid, title=title, publish_at=_iso(d, hour, rng.randint(0, 59)),
                    duration_s=rng.choices([18, 35, 52, 78, 130], weights=[18, 32, 26, 16, 8], k=1)[0],
                    views=views, likes=likes, comments=comments_n, shares=shares,
                    cover_style=cover, tag=rng.choice(meta["tags"]),
                ))
                # 爆款带来的涨粉跳变（回填到对应日期之后的快照）
                if is_viral and 1 <= days_ago <= 30:
                    idx = 30 - days_ago
                    bump = int(views * rng.uniform(0.004, 0.012))
                    for s in snaps[idx:]:
                        s.followers += bump
                    viral_days.add(days_ago)

            # 评论
            for work in works:
                negative_bias = 0.40 if rng.random() < 0.07 else 0.15   # 争议作品
                n_c = rng.randint(5, 18)
                for ci in range(n_c):
                    r = rng.random()
                    pool = NEGATIVE if r < negative_bias else (POSITIVE if r < 0.78 else NEUTRAL)
                    content = rng.choice(pool).replace("{item}", rng.choice(meta["items"]))
                    ds.comments.append(Comment(
                        id=f"{work.id}-C{ci + 1:02d}", work_id=work.id, blogger_id=bid,
                        content=content, likes=rng.randint(0, 260),
                        created_at=work.publish_at, replied=int(rng.random() < 0.22),
                    ))
            ds.works.extend(works)

            # 橱窗商品
            n_p = rng.randint(3, 8) if platform == "抖音" else rng.randint(1, 3)
            for p in range(n_p):
                price = round(rng.uniform(19, 499), 1)
                ds.products.append(Product(
                    id=f"{bid}-P{p + 1:02d}", blogger_id=bid,
                    name=f"{rng.choice(meta['items'])}{rng.choice(['套装', '精选', 'Pro', '联名款', '入门款'])}",
                    category=rng.choice(CATEGORIES), price=price,
                    sold_display=int(rng.uniform(50, 12000) * (base_followers / 300_000) ** 0.5),
                    rating=round(rng.uniform(4.1, 5.0), 2),
                ))

            final = snaps[-1].followers
            ds.bloggers.append(Blogger(
                id=bid, platform=platform, nickname=nick, industry=industry,
                followers=final, followers_7d=snaps[-8].followers,
                followers_30d=snaps[0].followers,
                total_likes=sum(w.likes for w in works), verified=int(rng.random() < 0.45),
                has_membership=has_member,
                member_price=round(rng.uniform(6, 30), 0) if has_member else 0.0,
                member_count=int(final * rng.uniform(0.002, 0.021)) if has_member else 0,
                member_works=rng.randint(2, 20) if has_member else 0,
                source="synthetic_demo", collected_at=BASE_DATE.isoformat(),
            ))
            ds.fan_snapshots.extend(snaps)
    return ds
