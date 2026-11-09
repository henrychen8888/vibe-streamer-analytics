"""合规公开页采集器（白名单控制，无反爬规避）。

设计约束（与 README 合规边界一致，刻意不做）：
  · 固定、可识别的 User-Agent，不轮换 UA、不伪装浏览器指纹；
  · 不接入任何 IP 代理池；请求被拦即停止并上报，不尝试绕过；
  · 不破解平台签名参数（如抖音 X-Bogus、小红书 x-s/x-t）、不接验证码打码；
  · 只取页面「公开可见」指标，绝不采集私信 / 手机号 / 非公开评论等个人信息；
  · 默认只跑白名单（自有账号），竞品账号需显式登记并确认风险。

技术现实（如实告知）：
  抖音 / 小红书的公开 Web 页大量内容需登录态或 JS 渲染，纯 HTTP 拉到的常是
  骨架页，结构化数据多嵌在 SSR 的 RENDER_DATA / __INITIAL_STATE__ 中且可能为空。
  本模块按已知结构尽力解析，拿不到就如实记录并降级，绝不伪造。要拿全量，
  仍建议走官方 OAuth（自有账号）或授权导出台账（已支持）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import abstractmethod
from datetime import date
from pathlib import Path

from .adapters import PlatformAdapter, RateLimiter, AdapterError
from .schema import Dataset, Blogger, Work, FanSnapshot

# 固定、可识别的 UA。刻意不使用随机 UA 池——这是合规姿态的一部分。
USER_AGENT = "VibeStreamerAnalytics/2.0 (+compliant-public-collector; contact: operator)"
# 公开页采集的全局最小间隔（秒）。原方案建议 3-5 秒，这里取上限偏保守。
MIN_INTERVAL = 4.0

# 白名单文件里「竞品/风险账号」必须携带的确认字段
RISK_ACK_KEYS = ("ack_by", "ack_at")


class WhitelistError(AdapterError):
    pass


def load_whitelist(path: str | Path) -> dict:
    """读取白名单配置。缺失或非法时抛出，避免静默跑全量。"""
    p = Path(path)
    if not p.exists():
        raise WhitelistError(f"缺少白名单 {p}，请复制 crawl_whitelist.example.json 并登记账号")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for acct in cfg.get("acknowledged_risk_accounts", []):
        missing = [k for k in RISK_ACK_KEYS if not acct.get(k)]
        if missing:
            raise WhitelistError(
                f"竞品/风险账号 {acct.get('handle', '?')} 缺少确认字段 {missing}，"
                f"必须显式登记 ack_by / ack_at 以示已知风险")
    return cfg


def classify_account(cfg: dict, platform: str, handle: str) -> str:
    """返回 'own' / 'risk' / ''（不在白名单，默认拒绝）。"""
    for a in cfg.get("own_accounts", []):
        if a.get("platform") == platform and a.get("handle") == handle:
            return "own"
    for a in cfg.get("acknowledged_risk_accounts", []):
        if a.get("platform") == platform and a.get("handle") == handle:
            return "risk"
    return ""


def _json_in_script(html: str, marker: str) -> dict:
    """从 <script> 中提取 marker 后的 JSON 对象。兼容抖音 RENDER_DATA（URL 编码）
    与小红书 window.__INITIAL_STATE__=（裸 JSON）。解析失败返回 {}，绝不抛错伪造。

    marker 后若紧跟 '='（如 __INITIAL_STATE__=），JSON 直接在其后；否则 marker 位于
    标签属性中（如 id="RENDER_DATA"），JSON 在下一个 '>' 之后。
    """
    idx = html.find(marker)
    if idx < 0:
        return {}
    after = idx + len(marker)
    if marker.endswith("="):
        start = after
    else:
        gt = html.find(">", after)
        if gt < 0:
            return {}
        start = gt + 1
    end = html.find("</script>", start)
    if end < 0:
        end = len(html)
    blob = html[start:end].strip().rstrip(";").strip()
    if not blob:
        return {}
    try:
        blob = urllib.parse.unquote(blob)  # 处理 RENDER_DATA 的百分号编码
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        pass
    # 退路：从第一个 { 做括号平衡截取，避免尾随脚本片段干扰
    i = blob.find("{")
    if i < 0:
        return {}
    depth, out = 0, []
    for ch in blob[i:]:
        out.append(ch)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
    try:
        return json.loads("".join(out))
    except (ValueError, json.JSONDecodeError):
        return {}


def _num(v, default=0) -> int:
    """容忍字符串/千分位/万为单位。拿不到原样回退 default。"""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").replace(" ", "")
    m = re.search(r"([\d.]+)", s)
    if not m:
        return default
    val = float(m.group(1))
    if "万" in s:
        val *= 10_000
    elif "亿" in s:
        val *= 100_000_000
    return int(val)


class PublicWebAdapter(PlatformAdapter):
    """公开页采集适配器基类。子类实现目标 URL 与解析。

    credibility='observed' 仅适用于「页面确实公开返回」的字段；若字段在公开页
    不存在而留空（0），下游分析会按缺失处理，不做任何估算填充。
    """

    source = "public_web"
    credibility = "observed"

    def __init__(self, cfg: dict, timeout: float = 15.0, transport=None):
        super().__init__()
        self.cfg = cfg
        self.timeout = timeout
        self._transport = transport  # 离线测试注入：(method, url, headers) -> html
        self.limiter = RateLimiter(min_interval=MIN_INTERVAL, burst=1)
        self.audit: list[dict] = []

    @abstractmethod
    def build_url(self, account: dict) -> str:
        ...

    @abstractmethod
    def parse_profile(self, html: str, account: dict) -> dict:
        """返回 {nickname, followers, total_likes, works_count, verified}。拿不到的留 0。"""
        ...

    @abstractmethod
    def parse_works(self, html: str, account: dict, blogger_id: str) -> list[Work]:
        ...

    def _fetch(self, url: str) -> str:
        self.limiter.wait()
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        try:
            if self._transport:
                return self._transport("GET", url, headers)
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raise AdapterError(f"采集被拒 HTTP {e.code} @ {url}（建议降低频率或改用授权接口）") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise AdapterError(f"采集网络失败 @ {url}：{e}") from e

    def fetch_account(self, account: dict) -> tuple[Blogger, list[Work], FanSnapshot | None]:
        platform = account["platform"]
        handle = account["handle"]
        cat = classify_account(self.cfg, platform, handle)
        if not cat:
            raise WhitelistError(
                f"账号 {platform}/{handle} 不在白名单，已拒绝（默认只采集白名单内账号）")
        url = self.build_url(account)
        html = self._fetch(url)
        prof = self.parse_profile(html, account)
        bid = f"{'DY' if platform == '抖音' else 'XHS'}-{handle}"
        today = date.today().isoformat()
        blogger = Blogger(
            id=bid, platform=platform, nickname=prof.get("nickname") or handle,
            industry=account.get("industry", "待归类"),
            followers=_num(prof.get("followers")),
            total_likes=_num(prof.get("total_likes")),
            verified=1 if prof.get("verified") else 0,
            source=self.source, collected_at=today,
        )
        works = self.parse_works(html, account, bid)
        snap = FanSnapshot(bid, today, blogger.followers, source=self.source) if blogger.followers else None
        self.audit.append({
            "platform": platform, "handle": handle, "category": cat,
            "url": url, "collected_at": today,
            "got_nickname": bool(prof.get("nickname")),
            "got_followers": blogger.followers, "works_found": len(works),
            "note": "" if blogger.followers else "公开页未返回结构化粉丝数（需登录/JS 渲染）",
        })
        return blogger, works, snap

    def fetch_dataset(self) -> Dataset:
        ds = Dataset()
        targets = [a for a in self.cfg.get("own_accounts", [])
                   if a.get("platform") == self.platform_label] + \
                  [a for a in self.cfg.get("acknowledged_risk_accounts", [])
                   if a.get("platform") == self.platform_label]
        if not targets:
            raise AdapterError(f"白名单中无 {self.platform_label} 账号")
        for acct in targets:
            b, works, snap = self.fetch_account(acct)
            ds.bloggers.append(b)
            ds.works.extend(works)
            if snap:
                ds.fan_snapshots.append(snap)
        return ds


class DouyinWebAdapter(PublicWebAdapter):
    name = "抖音公开页采集"
    platform_label = "抖音"

    def build_url(self, account: dict) -> str:
        if account.get("sec_uid"):
            return f"https://www.douyin.com/user/{account['sec_uid']}"
        return f"https://www.douyin.com/@{account['handle']}"

    def parse_profile(self, html: str, account: dict) -> dict:
        state = _json_in_script(html, "RENDER_DATA") or _json_in_script(html, "__INITIAL_STATE__")
        # 抖音 SSR 结构深且不稳，逐层容错取值
        user = {}
        try:
            user = state["userInfo"]["user"] or {}
        except (KeyError, TypeError):
            try:
                user = state["user"]["userInfo"] or {}
            except (KeyError, TypeError):
                user = {}
        followers = user.get("followerCount") or user.get("fansCount") or 0
        likes = user.get("totalFavorited") or user.get("favoritedCount") or 0
        return {
            "nickname": user.get("nickname"),
            "followers": _num(followers),
            "total_likes": _num(likes),
            "works_count": _num(user.get("awemeCount") or user.get("videoCount")),
            "verified": bool(user.get("verificationType") or user.get("enterpriseVerifyReason")),
        }

    def parse_works(self, html: str, account: dict, blogger_id: str) -> list[Work]:
        state = _json_in_script(html, "RENDER_DATA") or _json_in_script(html, "__INITIAL_STATE__")
        items = []
        try:
            posts = state["userInfo"]["user"]["postList"] or []
        except (KeyError, TypeError):
            posts = []
        for it in posts:
            st = it.get("statistics", {}) or {}
            ts = it.get("createTime") or 0
            when = date.fromtimestamp(ts).isoformat() + "T00:00" if ts else date.today().isoformat() + "T00:00"
            items.append(Work(
                id=f"{blogger_id}-{it.get('awemeId', it.get('id', len(items)))}",
                blogger_id=blogger_id, title=it.get("desc", "")[:60] or "无标题",
                publish_at=when, duration_s=_num(it.get("duration")),
                views=_num(st.get("playCount")), likes=_num(st.get("diggCount")),
                comments=_num(st.get("commentCount")), shares=_num(st.get("shareCount")),
                source=self.source))
        return items


class XiaohongshuWebAdapter(PublicWebAdapter):
    name = "小红书公开页采集"
    platform_label = "小红书"

    def build_url(self, account: dict) -> str:
        uid = account.get("user_id") or account["handle"]
        return f"https://www.xiaohongshu.com/user/profile/{uid}"

    def parse_profile(self, html: str, account: dict) -> dict:
        state = _json_in_script(html, "window.__INITIAL_STATE__=")
        info = {}
        try:
            uid = account.get("user_id") or account["handle"]
            info = state["user"].get(uid, {}).get("basicInfo", {}) or \
                   state.get("user", {}).get("basicInfo", {}) or {}
        except (KeyError, TypeError, AttributeError):
            info = {}
        followers = info.get("follows") or info.get("fans") or info.get("red_id_fans") or 0
        return {
            "nickname": info.get("nickname"),
            "followers": _num(followers),
            "total_likes": _num(info.get("likedNotesCount") or info.get("likes")),
            "works_count": _num(info.get("notesCount")),
            "verified": bool(info.get("officialVerifyStatus") or info.get("verifyStatus")),
        }

    def parse_works(self, html: str, account: dict, blogger_id: str) -> list[Work]:
        state = _json_in_script(html, "window.__INITIAL_STATE__=")
        items = []
        try:
            uid = account.get("user_id") or account["handle"]
            notes = state["user"][uid]["notes"] or []
        except (KeyError, TypeError, AttributeError):
            notes = []
        for n in notes:
            inter = n.get("interactInfo", {}) or {}
            ts = n.get("time") or n.get("createTime") or 0
            when = date.fromtimestamp(ts / 1000).isoformat() + "T00:00" if ts else date.today().isoformat() + "T00:00"
            items.append(Work(
                id=f"{blogger_id}-{n.get('noteId', n.get('id', len(items)))}",
                blogger_id=blogger_id, title=(n.get("title") or n.get("desc", ""))[:60] or "无标题",
                publish_at=when, views=_num(inter.get("viewCount")),
                likes=_num(inter.get("likedCount")), comments=_num(inter.get("commentCount")),
                shares=_num(inter.get("shareCount")), source=self.source))
        return items


def make_adapters(cfg: dict, transport=None) -> list[PublicWebAdapter]:
    out = []
    platforms = {a["platform"] for a in cfg.get("own_accounts", []) +
                 cfg.get("acknowledged_risk_accounts", [])}
    if "抖音" in platforms:
        out.append(DouyinWebAdapter(cfg, transport=transport))
    if "小红书" in platforms:
        out.append(XiaohongshuWebAdapter(cfg, transport=transport))
    return out
