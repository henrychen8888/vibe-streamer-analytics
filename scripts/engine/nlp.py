"""中文评论 NLP：零依赖兜底 + 可选增强。

设计取舍：SnowNLP 训练语料以商品评论为主，对直播/短视频口语（"上链接"
"家人们""翻车""割韭菜"）判别偏差明显，且对空串/纯 emoji 会抛异常。
因此这里以「领域词典 + 否定/程度规则」为主干，安装 jieba / snownlp 时
仅作为可选增强，并做异常兜底，保证管线永不因单条脏数据中断。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

try:  # 可选增强，缺失不影响运行
    import jieba  # type: ignore
except Exception:
    jieba = None

try:
    from snownlp import SnowNLP  # type: ignore
except Exception:
    SnowNLP = None

# 直播 / 电商 / 内容语境领域词
DOMAIN_TERMS = [
    # 内容 / 直播语境
    "性价比", "回购", "种草", "拔草", "踩雷", "翻车", "割韭菜", "上链接",
    "直播间", "小黄车", "优惠券", "福利", "干货", "教程", "测评", "开箱",
    "成分党", "平替", "贵妇", "学生党", "通勤", "显瘦", "显白", "质感",
    "包装", "物流", "售后", "客服", "退款", "假货", "溢价", "智商税",
    "会员", "专属", "更新", "催更", "画质", "剪辑", "文案", "BGM", "封面",
    "口播", "节奏", "干货满满", "讲得清楚", "听不懂", "太长", "太短",
    "已下单", "下单", "好用", "难用", "收藏", "关注", "取关", "链接",
    "求链接", "下一期", "后续", "推荐", "平价", "油皮", "教程", "硬广",
    "广告", "敷衍", "观望", "不值", "靠谱", "真诚", "高级", "舒服",
    "值回票价", "天花板", "保姆级", "横向测评", "实测", "避坑指南",
    # 常见品类词
    "粉底液", "口红", "精华", "防晒", "眼影", "空气炸锅", "酱料", "家常菜",
    "烘焙", "探店", "耳机", "键盘", "手机", "显示器", "充电宝", "外套",
    "牛仔裤", "通勤包", "球鞋", "配饰", "方法论", "书单", "效率工具",
    "课程", "笔记", "辅食", "纸尿裤", "推车", "绘本", "训练计划",
    "蛋白粉", "瑜伽垫", "跑鞋", "拉伸", "收纳", "香薰", "灯具", "床品",
    "小家电", "套装", "联名款", "入门款",
]
POS_WORDS = {
    "好": 1.0, "赞": 1.2, "喜欢": 1.2, "爱": 1.0, "棒": 1.2, "绝": 1.3,
    "靠谱": 1.3, "实用": 1.2, "干货": 1.0, "清楚": 1.0, "专业": 1.2,
    "回购": 1.5, "种草": 1.2, "性价比": 1.3, "显瘦": 1.2, "显白": 1.2,
    "质感": 1.0, "福利": 1.0, "优惠": 1.0, "精致": 1.1, "舒服": 1.1,
    "治愈": 1.1, "催更": 1.2, "讲得清楚": 1.4, "干货满满": 1.6, "收藏": 0.8,
    "已下单": 1.4, "好用": 1.4, "值": 1.1, "推荐": 1.2, "学到了": 1.4,
    "温柔": 0.8, "高级": 1.0, "真诚": 1.3, "良心": 1.4, "惊喜": 1.2,
}
NEG_WORDS = {
    "差": 1.2, "烂": 1.6, "坑": 1.5, "贵": 1.0, "慢": 0.9, "假": 1.6,
    "踩雷": 1.7, "翻车": 1.7, "割韭菜": 1.9, "智商税": 1.8, "溢价": 1.3,
    "退款": 1.3, "假货": 2.0, "售后": 0.8, "客服": 0.5, "听不懂": 1.3,
    "太长": 0.9, "太短": 0.7, "无聊": 1.2, "水": 0.8, "广告": 0.9,
    "硬广": 1.5, "敷衍": 1.5, "骗": 1.9, "失望": 1.6, "不值": 1.5,
    "掉粉": 1.4, "取关": 1.6, "吐槽": 1.0, "吵": 0.9, "糊": 1.0,
    "拔草": 1.1, "难用": 1.5, "踩坑": 1.5, "套路": 1.4,
}
NEGATORS = {"不", "没", "无", "别", "不太", "并不", "毫无"}
DEGREE = {"很": 1.4, "太": 1.5, "超": 1.6, "巨": 1.5, "非常": 1.7, "有点": 0.7,
          "稍微": 0.6, "特别": 1.6, "真的": 1.3, "最": 1.5, "贼": 1.4}
STOPWORDS = set("""的 了 是 我 你 他 她 它 们 这 那 有 在 就 都 也 还 又 和 与 及
一个 一下 什么 怎么 这个 那个 可以 但是 因为 所以 如果 真的 感觉 觉得 时候
啊 呀 吧 呢 哦 嗯 哈 啦 吗 嘛 哇 哎 唉 以及 一个 一直 已经 还是 而且
主播 博主 视频 内容 作品 东西 大家 家人们 姐妹 兄弟 老师 楼主 分享 看到
""".split()) | {"", " ", "  "}
POS_EMOJI = ("👍", "❤", "😍", "🥰", "👏", "🔥", "✨", "💯", "😂", "🤣")
NEG_EMOJI = ("👎", "😡", "😤", "💩", "🙄", "😒", "😭")

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,}")


def _capabilities() -> dict:
    return {"jieba": jieba is not None, "snownlp": SnowNLP is not None}


def tokenize(text: str) -> list[str]:
    """分词：jieba 可用时用它；否则领域词 + CJK 二元切分兜底。"""
    if not text:
        return []
    text = str(text).lower()
    tokens: list[str] = []

    # 领域词优先整体保留，避免被切成无意义单字
    for term in DOMAIN_TERMS:
        n = text.count(term)
        if n:
            tokens.extend([term] * n)
            text = text.replace(term, " ")

    if jieba is not None:
        try:
            tokens.extend(w.strip() for w in jieba.cut(text))
        except Exception:
            pass
    else:
        # 兜底：领域词已优先抽出，剩余片段只保留 2-4 字整体，
        # 不做滑动 bigram（会产生「得很 / 很舒 / 都好」这类无意义噪声）。
        for seg in _CJK.findall(text):
            if 2 <= len(seg) <= 4 and seg not in STOPWORDS:
                tokens.append(seg)
    tokens.extend(_LATIN.findall(text))
    return [t for t in tokens if len(t) >= 2 and t not in STOPWORDS and not t.isdigit()]


def sentiment(text: str) -> tuple[float, str]:
    """返回 (0..1 情感分, 标签)。规则主干 + 可选 SnowNLP 融合，永不抛异常。"""
    if not text or not str(text).strip():
        return 0.5, "中性"
    s = str(text)
    score = 0.0
    for word, weight in POS_WORDS.items():
        if word not in s:
            continue
        for m in re.finditer(re.escape(word), s):
            prefix = s[max(0, m.start() - 3):m.start()]
            mult = next((v for k, v in DEGREE.items() if k in prefix), 1.0)
            flip = -1.0 if any(n in prefix for n in NEGATORS) else 1.0
            score += weight * mult * flip
    for word, weight in NEG_WORDS.items():
        if word not in s:
            continue
        for m in re.finditer(re.escape(word), s):
            prefix = s[max(0, m.start() - 3):m.start()]
            mult = next((v for k, v in DEGREE.items() if k in prefix), 1.0)
            flip = -0.8 if any(n in prefix for n in NEGATORS) else 1.0
            score -= weight * mult * flip
    score += 0.45 * sum(s.count(e) for e in POS_EMOJI)
    score -= 0.45 * sum(s.count(e) for e in NEG_EMOJI)
    if "!" in s or "！" in s:
        score *= 1.08
    base = 1.0 / (1.0 + math.exp(-score * 0.9))

    if SnowNLP is not None:
        try:
            base = 0.7 * base + 0.3 * float(SnowNLP(s).sentiments)
        except Exception:
            pass
    label = "正面" if base > 0.6 else ("负面" if base < 0.4 else "中性")
    return round(base, 4), label


def is_effective(text: str) -> bool:
    """有效评论：去掉纯表情/纯标点/过短灌水后仍有实质内容。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))
    return len(s) >= 4


def analyze_comments(comments, topn: int = 30) -> dict:
    """评论聚合：情感分布、热词、有效评论率、回复率、争议点。"""
    total = len(comments)
    dist = Counter()
    word_counter: Counter = Counter()
    word_score: dict[str, list[float]] = defaultdict(list)
    negative_by_work: dict[str, list[str]] = defaultdict(list)
    score_sum = 0.0
    effective = 0
    replied = 0

    for c in comments:
        sc, label = sentiment(c.content)
        c.sentiment = sc
        dist[label] += 1
        score_sum += sc
        effective += int(is_effective(c.content))
        replied += int(bool(c.replied))
        for w in set(tokenize(c.content)):
            word_counter[w] += 1
            word_score[w].append(sc)
        if label == "负面":
            negative_by_work[c.work_id].append(c.content)

    keywords = [
        {"word": w, "count": n,
         "sentiment": round(sum(word_score[w]) / len(word_score[w]), 3)}
        for w, n in word_counter.most_common(topn * 3)
        if n >= 2
    ][:topn]

    controversies = []
    for work_id, texts in negative_by_work.items():
        if len(texts) < 2:
            continue
        hot = Counter(w for t in texts for w in set(tokenize(t))).most_common(3)
        controversies.append({
            "work_id": work_id,
            "negative_count": len(texts),
            "topics": [w for w, _ in hot],
            "sample": texts[0][:60],
        })
    controversies.sort(key=lambda x: -x["negative_count"])

    return {
        "total": total,
        "sentiment_dist": {k: dist.get(k, 0) for k in ("正面", "中性", "负面")},
        "avg_sentiment": round(score_sum / total, 4) if total else 0.5,
        "keywords": keywords,
        "effective_rate": round(effective / total, 4) if total else 0,
        "reply_rate": round(replied / total, 4) if total else 0,
        "controversies": controversies[:8],
        "engine": "领域词典规则" + (" + jieba" if jieba else "") + (" + SnowNLP" if SnowNLP else "") + "（纯标准库兜底）",
    }
