"""数据采集适配器层：合规优先，官方授权数据 / 自有导出数据为一等公民。

重要边界（原方案未说明，属架构性风险）：
  抖音开放平台 OAuth 接口返回的是「已授权账号自身」的数据。任何第三方达人
  的粉丝画像、作品明细、评论内容，都不能靠开放平台 scope 批量拉取；需要
  走巨量星图 / 精选联盟 / 服务商（含达人授权）等对应渠道，或使用自有台账。
  因此本层不提供任何绕过授权的采集实现，也不包含代理池 / UA 轮换逻辑。
"""
from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from .schema import Dataset, row_to, Blogger, Work, Comment, Product, FanSnapshot
from .seed import generate

SCOPE_CATALOG = {
    # 新版 scope 带 .bind 后缀；旧版同名 scope 已在官方文档中标记为「旧」
    "user_info": "用户基础信息（默认开放）",
    "video.list.bind": "查询授权账号的视频列表（新）",
    "video.data.bind": "查询指定视频的实时数据（新）",
    "fans.data.bind": "粉丝画像数据（新）",
    "data.external.user": "用户数据：获赞/评论/分享/主页访问（T+1）",
    "data.external.item": "作品数据：获赞/评论/分享（T+1）",
    "item.comment": "评论管理（普通用户）",
    "video.comment": "评论管理（企业号）",
    "video.search": "关键词视频管理（特殊权限，需单独申请）",
}
# 官方未提供给普通第三方应用的能力，禁止在产品设计里当作可采集字段
NOT_AVAILABLE_VIA_OAUTH = [
    "第三方达人（未授权）的粉丝画像与作品明细",
    "完播率 / 播放完成度（无对外接口）",
    "真实成交 GMV（销量标识仅为区间展示值）",
    "私信、手机号、非公开评论等隐私数据",
    "会员入会明细与续费率（需达人自有后台导出）",
]


class AdapterError(RuntimeError):
    pass


class RateLimiter:
    """每平台独立限速队列：最小间隔 + 令牌桶，只控制我方请求节奏。"""

    def __init__(self, min_interval: float = 0.35, burst: int = 5):
        self.min_interval = min_interval
        self.burst = burst
        self._last = 0.0
        self._tokens = float(burst)
        self._tick = time.monotonic()

    def wait(self):
        if self.min_interval <= 0:   # 离线契约测试 / 本地回放时允许关闭限速
            return
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._tick) / self.min_interval)
        self._tick = now
        if self._tokens < 1:
            time.sleep(self.min_interval * (1 - self._tokens))
            self._tokens = 0.0
        else:
            self._tokens -= 1
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


class PlatformAdapter(ABC):
    name = "base"
    source = "unknown"
    credibility = "observed"

    def __init__(self):
        self.limiter = RateLimiter()

    @abstractmethod
    def fetch_dataset(self) -> Dataset:
        """返回标准化 Dataset。禁止返回未脱敏的个人信息。"""
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "source": self.source,
                "credibility": self.credibility, "class": type(self).__name__}


class DemoAdapter(PlatformAdapter):
    """合成演示数据。固定随机种子，任何数字都只在演示语境下有意义。"""

    name = "演示数据（合成）"
    source = "synthetic_demo"
    credibility = "estimated"

    def __init__(self, seed: int = 20261109, bloggers_per_industry: int = 6):
        super().__init__()
        self.seed = seed
        self.n = bloggers_per_industry

    def fetch_dataset(self) -> Dataset:
        return generate(self.seed, self.n)


class LocalExportAdapter(PlatformAdapter):
    """自有 / 达人授权导出的 CSV 或 JSON 台账，零网络请求，最稳妥的落地入口。

    目录内按实体名放置文件，支持 .json（对象数组）或 .csv（首行表头）：
      bloggers.json / works.json / comments.json / products.json / fan_snapshots.json
    字段以 engine/schema.py 的数据契约为准，多余字段自动忽略。
    """

    name = "本地授权导出"
    source = "authorized_export"
    credibility = "observed"

    ENTITIES = (
        ("bloggers", "bloggers", Blogger),
        ("works", "works", Work),
        ("comments", "comments", Comment),
        ("products", "products", Product),
        ("fan_snapshots", "fan_snapshots", FanSnapshot),
    )

    def __init__(self, directory: str | Path):
        super().__init__()
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise AdapterError(f"目录不存在：{self.directory}")

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else data.get("data", [])
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        return []

    def fetch_dataset(self) -> Dataset:
        ds = Dataset()
        for key, attr, cls in self.ENTITIES:
            rows: list[dict] = []
            for ext in ("json", "csv"):
                p = self.directory / f"{key}.{ext}"
                if p.exists():
                    rows = self._read(p)
                    break
            target = getattr(ds, attr)
            for r in rows:
                obj = row_to(cls, r)
                # 数字字段做容错转换（CSV 读出来的都是字符串）
                for fname, ftype in cls.__annotations__.items():
                    value = getattr(obj, fname)
                    if ftype in ("int", "float") and not isinstance(value, (int, float)):
                        try:
                            value = float(value)
                            setattr(obj, fname, int(value) if ftype == "int" else value)
                        except (TypeError, ValueError):
                            setattr(obj, fname, 0)
                target.append(obj)
        if not ds.bloggers:
            raise AdapterError(f"{self.directory} 内未找到有效的 bloggers.json / bloggers.csv")
        return ds


class DouyinOpenApiAdapter(PlatformAdapter):
    """抖音开放平台官方接口骨架（需企业资质 + 达人 OAuth 授权）。

    刻意不实现「任意博主数据抓取」：官方 OAuth 只返回授权账号自身数据。
    真实接入时按控制台实际下发的权限点替换 REQUIRED_SCOPES，并补齐字段映射；
    电商/会员数据需另接对应服务商渠道或由达人后台导出（LocalExportAdapter）。
    """

    name = "抖音开放平台（官方授权）"
    source = "douyin_open_api"
    credibility = "observed"
    REQUIRED_SCOPES = (
        "user_info", "video.list.bind", "video.data.bind",
        "fans.data.bind", "data.external.user", "data.external.item",
    )
    AUTH_URL = "https://open.douyin.com/platform/oauth/connect/"
    API_BASE = "https://open.douyin.com"
    # 接口路径以开放平台控制台 / 官方文档为准；企业应用若有专属网关，
    # 可在 config.json 的 endpoints 中逐项覆盖，避免改代码。
    ENDPOINTS = {
        "token": "/oauth/access_token/",
        "refresh": "/oauth/refresh_token/",
        "user_info": "/oauth/user_info/",
        "video_list": "/api/douyin/v1/video/video_list/",
        "video_data": "/api/douyin/v1/video/video_data/",
        "fans_data": "/api/douyin/v1/fans/fans_data/",
    }

    def __init__(self, client_key: str = "", client_secret: str = "",
                 redirect_uri: str = "", access_token: str = "",
                 refresh_token: str = "", open_ids: list | None = None,
                 endpoints: dict | None = None, timeout: float = 12.0,
                 transport=None):
        super().__init__()
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.open_ids = open_ids or []
        self.endpoints = {**self.ENDPOINTS, **(endpoints or {})}
        self.timeout = timeout
        # 测试注入点：callable(method, url, headers, body_bytes) -> dict
        # 生产为 None，走真实 urllib；不提供任何绕过授权的能力。
        self._transport = transport

    @classmethod
    def from_config(cls, path: str | Path = "config.json"):
        p = Path(path)
        if not p.exists():
            raise AdapterError(f"缺少 {p}，请复制 config.example.json 并填入企业应用凭证")
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in cfg.items()
                      if k in ("client_key", "client_secret", "redirect_uri", "access_token",
                               "refresh_token", "open_ids", "endpoints")})

    def authorize_url(self, state: str = "state_csrf_token") -> str:
        """生成达人授权链接。state 必须做 CSRF 校验后再换取 token。"""
        if not self.client_key:
            raise AdapterError("缺少 client_key，请先在开放平台控制台创建应用")
        return self.AUTH_URL + "?" + urlencode({
            "client_key": self.client_key, "response_type": "code",
            "scope": ",".join(self.REQUIRED_SCOPES),
            "redirect_uri": self.redirect_uri, "state": state,
        })

    def describe(self) -> dict:
        d = super().describe()
        d.update({"scopes": list(self.REQUIRED_SCOPES),
                  "scope_desc": {k: SCOPE_CATALOG.get(k, "以控制台为准") for k in self.REQUIRED_SCOPES},
                  "endpoints": dict(self.endpoints),
                  "authorized_accounts": len(self.open_ids),
                  "not_available": NOT_AVAILABLE_VIA_OAUTH,
                  "boundary": "OAuth 仅返回已授权账号自身数据；第三方达人需星图/服务商渠道或授权导出"})
        return d

    def _request(self, method: str, path: str, params: dict | None = None,
                 payload: dict | None = None, auth: bool = True) -> dict:
        """统一出口：限速 + token 注入 + 错误处理。固定 UA，不做 UA 轮换。"""
        self.limiter.wait()
        if auth and not self.access_token:
            raise AdapterError("尚未完成达人 OAuth 授权（access_token 为空）")
        url = self.API_BASE + path
        if method == "GET" and params:
            url += "?" + urlencode(params)
        headers = {"Content-Type": "application/json",
                   "User-Agent": "VibeStreamerAnalytics/2.0 (+authorized-oauth-client)"}
        if auth:
            headers["access-token"] = self.access_token
        body = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
        try:
            if self._transport:
                resp = self._transport(method, url, headers, body)
            else:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise AdapterError(f"接口 {path} 返回 HTTP {e.code}：{e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise AdapterError(f"接口 {path} 网络失败：{e}") from e
        except (ValueError, json.JSONDecodeError) as e:
            raise AdapterError(f"接口 {path} 返回非 JSON：{e}") from e
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        code = data.get("error_code", resp.get("error_code", 0)) if isinstance(resp, dict) else -1
        if code not in (0, None):
            desc = data.get("description", resp.get("description", "无描述"))
            raise AdapterError(f"接口 {path} 业务错误 error_code={code}：{desc}")
        return resp

    def _post(self, key_or_path: str, payload: dict) -> dict:
        path = self.endpoints.get(key_or_path, key_or_path)
        return self._request("POST", path, payload=payload)

    def exchange_code(self, code: str) -> dict:
        """用授权 code 换 access_token。state 校验应在回调层完成。"""
        r = self._request("POST", self.endpoints["token"], auth=False, payload={
            "client_key": self.client_key, "client_secret": self.client_secret,
            "code": code, "grant_type": "authorization_code",
        })
        d = r.get("data", {})
        self.access_token = d.get("access_token", "")
        self.refresh_token = d.get("refresh_token", "")
        oid = d.get("open_id")
        if oid and oid not in self.open_ids:
            self.open_ids.append(oid)
        return d

    def refresh_access_token(self) -> dict:
        r = self._request("POST", self.endpoints["refresh"], auth=False, payload={
            "client_key": self.client_key, "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        })
        d = r.get("data", {})
        self.access_token = d.get("access_token", self.access_token)
        self.refresh_token = d.get("refresh_token", self.refresh_token)
        return d

    @staticmethod
    def _first(d: dict, *keys, default=0):
        for k in keys:
            v = d.get(k)
            if v not in (None, ""):
                return v
        return default

    def fetch_dataset(self) -> Dataset:
        """拉取**已授权账号自身**的数据。绝不遍历第三方达人。

        诚实约束：只写入本次真实拿到的 1 个粉丝快照点，不补历史 30 天；
        评论、商品、会员明细需另走授权导出，本方法不越权采集。
        """
        if not self.access_token:
            raise AdapterError("access_token 为空，请先执行 python3 run.py oauth 完成达人授权")
        if not self.open_ids:
            raise AdapterError("config.json 的 open_ids 为空：OAuth 只返回已授权账号自身数据")
        ds = Dataset()
        today = date.today().isoformat()
        for open_id in self.open_ids:
            info = self._request("GET", self.endpoints["user_info"],
                                 params={"open_id": open_id}).get("data", {})
            bid = "DY-" + str(open_id)[-10:]
            nickname = str(self._first(info, "nickname", "display_name", default=bid))
            followers = 0
            try:
                fans = self._post("fans_data", {"open_id": open_id}).get("data", {})
                followers = int(self._first(fans, "fans_total", "follower_count", "total", default=0))
            except AdapterError:
                pass  # 未申请该权限时留空，不猜测
            followers = followers or int(self._first(info, "follower_count", "fans_count", default=0))

            item_ids: list[str] = []
            titles: dict[str, str] = {}
            cursor, pages = 0, 0
            while pages < 20:
                d = self._post("video_list", {"open_id": open_id, "cursor": cursor, "count": 20}).get("data", {})
                for it in d.get("list", []) or []:
                    iid = str(self._first(it, "item_id", "video_id", default=""))
                    if not iid:
                        continue
                    item_ids.append(iid)
                    titles[iid] = str(self._first(it, "title", "desc", default="无标题"))
                cursor = int(self._first(d, "cursor", default=0))
                pages += 1
                if not d.get("has_more") or not d.get("list"):
                    break

            works: list[Work] = []
            for i in range(0, len(item_ids), 20):
                chunk = item_ids[i:i + 20]
                d = self._post("video_data", {"open_id": open_id, "item_ids": chunk}).get("data", {})
                for it in d.get("list", []) or []:
                    iid = str(self._first(it, "item_id", "video_id", default=""))
                    st = it.get("statistics", {}) if isinstance(it, dict) else {}
                    ts = int(self._first(it, "create_time", default=0))
                    when = date.fromtimestamp(ts).isoformat() + "T00:00" if ts else today + "T00:00"
                    works.append(Work(
                        id=f"{bid}-{iid}", blogger_id=bid, title=titles.get(iid, "无标题"),
                        publish_at=when, views=int(self._first(st, "play_count", "view_count", default=0)),
                        likes=int(self._first(st, "digg_count", "like_count", default=0)),
                        comments=int(self._first(st, "comment_count", default=0)),
                        shares=int(self._first(st, "share_count", default=0)),
                        source="douyin_open_api",
                    ))
            ds.works.extend(works)
            ds.bloggers.append(Blogger(
                id=bid, platform="抖音", nickname=nickname, industry="待归类",
                followers=followers, total_likes=sum(w.likes for w in works),
                source="douyin_open_api", collected_at=today,
            ))
            if followers:
                ds.fan_snapshots.append(FanSnapshot(bid, today, followers, source="douyin_open_api"))
        return ds


def registry() -> dict:
    """已注册适配器清单（供 CLI / 文档生成，避免手写清单与实际实现漂移）。"""
    return {
        "demo": DemoAdapter().describe(),
        "local_export": {"name": "本地授权导出", "source": "authorized_export",
                         "credibility": "observed", "class": "LocalExportAdapter"},
        "douyin_open_api": DouyinOpenApiAdapter().describe(),
    }
