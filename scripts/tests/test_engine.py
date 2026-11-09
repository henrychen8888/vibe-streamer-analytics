"""标准库 unittest：零第三方依赖，覆盖数据契约、口径、NLP、仓储与交付物。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import adapters, store  # noqa: E402
from engine.analytics import build_report, _median, _winsor  # noqa: E402
from engine.nlp import analyze_comments, is_effective, sentiment, tokenize  # noqa: E402
from engine.report import build_dashboard, export_csv, verify_csv_vs_db  # noqa: E402
from engine.schema import Comment, Dataset  # noqa: E402
from engine.seed import generate  # noqa: E402


class TestMath(unittest.TestCase):
    def test_median_and_winsor(self):
        self.assertEqual(_median([1, 2, 3, 4]), 2.5)
        self.assertEqual(_median([]), 0.0)
        w = _winsor([1, 2, 3, 1000], 0.0, 0.75)
        self.assertLessEqual(max(w), 3)


class TestNLP(unittest.TestCase):
    def test_sentiment_direction(self):
        pos, _ = sentiment("太好用了，性价比拉满，已回购三次 👍")
        neg, _ = sentiment("翻车了，假货还死贵，客服不回，准备退款 😤")
        self.assertGreater(pos, 0.6)
        self.assertLess(neg, 0.4)
        self.assertGreater(pos, neg)

    def test_robust_on_dirty_input(self):
        for text in ("", None, "👍👍", "!!!", "😂", "   "):
            score, label = sentiment(text)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)
            self.assertIn(label, ("正面", "中性", "负面"))

    def test_tokenize_and_effective(self):
        words = tokenize("这个粉底液性价比真的高，回购了")
        self.assertIn("性价比", words)
        self.assertTrue(is_effective("这个粉底液真的好用"))
        self.assertFalse(is_effective("哈哈哈"))

    def test_analyze_comments_shape(self):
        cs = [Comment(id=str(i), work_id="W1", blogger_id="B1", content=t, replied=i % 2)
              for i, t in enumerate(["太好用了，干货满满"] * 6 + ["翻车了，退款"] * 3 + ["求链接"] * 2)]
        r = analyze_comments(cs)
        self.assertEqual(r["total"], 11)
        self.assertEqual(sum(r["sentiment_dist"].values()), 11)
        self.assertGreaterEqual(r["effective_rate"], 0)
        self.assertIsInstance(r["keywords"], list)


class TestSeed(unittest.TestCase):
    def test_reproducible(self):
        a, b = generate(42, 2), generate(42, 2)
        self.assertEqual(a.counts(), b.counts())
        self.assertEqual([x.followers for x in a.bloggers], [x.followers for x in b.bloggers])

    def test_referential_integrity(self):
        ds = generate(7, 2)
        ids = {b.id for b in ds.bloggers}
        wids = {w.id for w in ds.works}
        self.assertTrue(all(w.blogger_id in ids for w in ds.works))
        self.assertTrue(all(c.work_id in wids for c in ds.comments))
        self.assertEqual(len({(s.blogger_id, s.date) for s in ds.fan_snapshots}),
                         len(ds.fan_snapshots))


class TestStore(unittest.TestCase):
    def test_roundtrip_and_audit(self):
        ds = generate(11, 2)
        with tempfile.TemporaryDirectory() as d:
            conn = store.connect(Path(d) / "x.db")
            counts = store.save_dataset(conn, ds, "test")
            loaded = store.load_dataset(conn)
            self.assertEqual(loaded.counts(), counts)
            self.assertEqual(len(conn.execute("SELECT * FROM audit_log").fetchall()), 1)
            new = store.upsert_works(conn, ds.works[:3])
            self.assertEqual(new, 0, "重复作品不应重复插入")
            conn.close()

    def test_local_export_adapter(self):
        ds = generate(3, 2)
        with tempfile.TemporaryDirectory() as d:
            export_csv(ds, d)
            loaded = adapters.LocalExportAdapter(d).fetch_dataset()
            self.assertEqual(loaded.counts(), ds.counts())
            self.assertEqual(loaded.bloggers[0].followers, ds.bloggers[0].followers)


class TestAnalytics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = generate(20261109, 3)
        cls.report = build_report(cls.ds, "synthetic_demo", 20261109)

    def test_seven_dimensions_present(self):
        for key in ("industry", "rankings", "works", "fans", "comments",
                    "shop", "membership", "monetization"):
            self.assertIn(key, self.report, key)
            self.assertTrue(self.report[key], key)

    def test_credibility_marked(self):
        self.assertEqual(set(self.report["meta"]["credibility"]),
                         {"observed", "derived", "estimated"})
        levels = {k["level"] for k in self.report["meta"]["methodology"]}
        self.assertIn("estimated", levels)

    def test_no_fake_metrics(self):
        text = json.dumps(self.report, ensure_ascii=False)
        for forbidden in ("完播率预测值", "真实成交额"):
            self.assertNotIn(forbidden, text)

    def test_kpi_and_insights(self):
        self.assertEqual(len(self.report["kpi"]), 6)
        self.assertGreaterEqual(len(self.report["insights"]), 8)
        self.assertTrue(all(i["evidence"] for i in self.report["insights"]))

    def test_scores_bounded(self):
        self.assertTrue(all(0 <= r["monetization_score"] <= 100
                            for r in self.report["bloggers"]))
        self.assertTrue(all(0 <= r["interaction_rate"] < 100
                            for r in self.report["bloggers"]))

    def test_industry_total_matches(self):
        self.assertEqual(sum(i["bloggers"] for i in self.report["industry"]),
                         len(self.report["bloggers"]))


class TestDeliverables(unittest.TestCase):
    def test_dashboard_offline(self):
        tpl = ROOT / "web" / "index.template.html"
        if not tpl.exists():
            self.skipTest("模板尚未生成")
        report = build_report(generate(5, 2), "synthetic_demo", 5)
        with tempfile.TemporaryDirectory() as d:
            p = build_dashboard(report, tpl, Path(d) / "dash.html")
            html = p.read_text(encoding="utf-8")
            self.assertIn("const REPORT =", html)
            self.assertNotIn("/*__PAYLOAD__*/", html)
            self.assertNotIn('src="http', html)
            self.assertNotIn('href="http', html)

    def test_csv_verify(self):
        ds = generate(9, 2)
        with tempfile.TemporaryDirectory() as d:
            export_csv(ds, d)
            check = verify_csv_vs_db(ds, d)
            self.assertTrue(all(v["ok"] for v in check.values()))


class TestValidate(unittest.TestCase):
    """真实数据接入体检：合法放行、脏数据阻断。"""

    @staticmethod
    def _write(d, name, cols, rows):
        import csv as _csv
        with (Path(d) / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    def _base(self, d, b_extra=None):
        cols = ["id", "platform", "nickname", "industry", "followers"]
        row = dict(id="B1", platform="抖音", nickname="某账号", industry="美妆", followers=10000)
        row.update(b_extra or {})
        self._write(d, "bloggers", cols, [row])

    def test_minimal_valid_file_passes(self):
        from engine.validate import validate_directory
        with tempfile.TemporaryDirectory() as d:
            self._base(d)
            r = validate_directory(d)
            self.assertTrue(r["ok"], r["errors"])
            self.assertTrue(any("LOW_COVERAGE" in w["code"] for w in r["warnings"]))
            self.assertEqual(r["summary"]["bloggers"], 1)

    def test_blocks_template_rows(self):
        from engine.validate import validate_directory
        with tempfile.TemporaryDirectory() as d:
            self._base(d, {"id": "EXAMPLE-001"})
            r = validate_directory(d)
            self.assertFalse(r["ok"])
            self.assertTrue(any(e["code"] == "TEMPLATE_ROWS" for e in r["errors"]))

    def test_blocks_pii_column(self):
        from engine.validate import validate_directory
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "bloggers", ["id", "platform", "nickname", "industry", "followers", "手机号"],
                        [dict(id="B1", platform="抖音", nickname="某账号", industry="美妆",
                              followers=10000, 手机号="13800000000")])
            r = validate_directory(d)
            self.assertFalse(r["ok"])
            self.assertTrue(any(e["code"] == "PII_COLUMN" for e in r["errors"]))

    def test_blocks_broken_fk_and_bad_value(self):
        from engine.validate import validate_directory
        with tempfile.TemporaryDirectory() as d:
            self._base(d)
            self._write(d, "works", ["id", "blogger_id", "title", "publish_at", "views", "likes", "comments", "shares"],
                        [dict(id="W1", blogger_id="NOT_EXIST", title="作品", publish_at="2026-11-01T20:30",
                              views="abc", likes=10, comments=2, shares=1)])
            r = validate_directory(d)
            self.assertFalse(r["ok"])
            codes = {e["code"] for e in r["errors"]}
            self.assertIn("BROKEN_FK", codes)
            self.assertIn("BAD_VALUE", codes)


class TestReconcile(unittest.TestCase):
    """脱敏与确定性回填：只补已有观测，绝不编造。"""

    def test_desensitize(self):
        from engine.reconcile import desensitize
        s, n = desensitize("联系我 13812345678 或 a@b.com，微信：abc_12345")
        self.assertGreaterEqual(n, 2)
        self.assertNotIn("13812345678", s)
        self.assertNotIn("a@b.com", s)

    def test_backfill_from_snapshots(self):
        from datetime import date, timedelta
        from engine.reconcile import reconcile
        from engine.schema import Blogger, FanSnapshot, Dataset
        base = date(2026, 11, 9)
        snaps = [FanSnapshot("B1", (base - timedelta(days=k)).isoformat(), 10000 - k * 10)
                 for k in range(11, -1, -1)]
        ds = Dataset(bloggers=[Blogger(id="B1", platform="抖音", nickname="A", industry="美妆", followers=0)],
                     fan_snapshots=snaps)
        stats = reconcile(ds)
        b = ds.bloggers[0]
        self.assertGreater(b.followers_7d, 0)
        self.assertEqual(b.followers_30d, 0, "不足 30 天时不得编造")
        self.assertEqual(stats["growth_30d_unavailable"], 1)
        self.assertEqual(len(ds.fan_snapshots), 12, "不得新增观测点")

    def test_no_snapshots_stays_empty(self):
        from engine.reconcile import reconcile
        from engine.schema import Blogger, Dataset
        ds = Dataset(bloggers=[Blogger(id="B1", platform="抖音", nickname="A", industry="美妆", followers=100)])
        stats = reconcile(ds)
        self.assertEqual(ds.bloggers[0].followers_7d, 0)
        self.assertEqual(stats["growth_7d_unavailable"], 1)
        self.assertTrue(any("增长曲线为空态" in n for n in stats["notes"]))


class TestOAuthAdapter(unittest.TestCase):
    """官方接口契约：用注入的 mock transport 验证字段映射，不发真实网络请求。"""

    def _adapter(self):
        from engine.adapters import DouyinOpenApiAdapter
        def fake(method, url, headers, body):
            payload = json.loads(body) if body else {}
            if "/oauth/user_info/" in url:
                return {"data": {"error_code": 0, "open_id": "oid-123", "nickname": "授权账号"}}
            if "fans_data/" in url:
                return {"data": {"error_code": 0, "fans_total": 52000}}
            if "video_list/" in url:
                return {"data": {"error_code": 0, "has_more": False, "cursor": 0,
                                 "list": [{"item_id": "i1", "title": "作品A", "create_time": 1730000000}]}}
            if "video_data/" in url:
                return {"data": {"error_code": 0, "list": [{
                    "item_id": "i1", "create_time": 1730000000,
                    "statistics": {"play_count": 10000, "digg_count": 500,
                                   "comment_count": 30, "share_count": 20}}]}}
            return {"data": {"error_code": 0}}

        a = DouyinOpenApiAdapter(client_key="k", client_secret="s", access_token="t",
                                 open_ids=["oid-123"], transport=fake)
        a.limiter.min_interval = 0
        return a

    def test_requires_token(self):
        from engine.adapters import DouyinOpenApiAdapter, AdapterError
        a = DouyinOpenApiAdapter(client_key="k", open_ids=["x"])
        with self.assertRaises(AdapterError):
            a.fetch_dataset()

    def test_boundary_no_open_ids(self):
        from engine.adapters import DouyinOpenApiAdapter, AdapterError
        a = DouyinOpenApiAdapter(client_key="k", access_token="t")
        with self.assertRaises(AdapterError):
            a.fetch_dataset()

    def test_mock_mapping(self):
        ds = self._adapter().fetch_dataset()
        self.assertEqual(len(ds.bloggers), 1)
        self.assertEqual(ds.bloggers[0].followers, 52000)
        self.assertEqual(len(ds.works), 1)
        self.assertEqual(ds.works[0].views, 10000)
        self.assertEqual(ds.works[0].likes, 500)
        self.assertEqual(len(ds.fan_snapshots), 1, "只写本次实测点，不补 30 天历史")
        self.assertEqual(len(ds.comments), 0, "不越权采集评论")
        self.assertEqual(len(ds.products), 0)

    def test_authorize_url_has_bind_scopes(self):
        url = self._adapter().authorize_url("csrf")
        self.assertIn("video.data.bind", url)
        self.assertIn("fans.data.bind", url)
        self.assertIn("state=csrf", url)


class TestCrawler(unittest.TestCase):
    """合规公开页采集器：白名单强制、审计、SSR 解析，全部离线（注入 mock HTML）。"""

    DOUYIN_HTML = (
        '<script id="RENDER_DATA" type="application/json">'
        + urllib.parse.quote(json.dumps({
            "userInfo": {"user": {
                "nickname": "测试抖音号", "followerCount": 128000, "totalFavorited": 3500000,
                "awemeCount": 420, "verificationType": 1,
                "postList": [
                    {"awemeId": "a1", "desc": "第一条作品", "createTime": 1730000000,
                     "duration": 45, "statistics": {"playCount": 120000, "diggCount": 6800,
                                                    "commentCount": 320, "shareCount": 210}},
                    {"awemeId": "a2", "desc": "第二条作品", "createTime": 1729000000,
                     "duration": 30, "statistics": {"playCount": 89000, "diggCount": 4200,
                                                    "commentCount": 180, "shareCount": 95}},
                ]}}})
        ).encode("utf-8").decode("utf-8") + '</script>'
    )
    XHS_HTML = (
        '<script>window.__INITIAL_STATE__=' + json.dumps({
            "user": {"u123": {
                "basicInfo": {"nickname": "测试小红书号", "follows": 56000,
                              "notesCount": 310, "officialVerifyStatus": 1},
                "notes": [
                    {"noteId": "n1", "title": "笔记一", "time": 1730000000000,
                     "interactInfo": {"viewCount": "12.3万", "likedCount": "6800",
                                      "commentCount": "320", "shareCount": "210"}},
                ]}}}) + ';</script>'
    )

    @staticmethod
    def _douyin(transport):
        from engine.crawler import DouyinWebAdapter
        cfg = {"own_accounts": [{"platform": "抖音", "handle": "test_dy", "industry": "美妆"}],
               "acknowledged_risk_accounts": []}
        a = DouyinWebAdapter(cfg, transport=lambda m, u, h: transport)
        a.limiter.min_interval = 0
        return a

    def test_douyin_parse_and_audit(self):
        a = self._douyin(self.DOUYIN_HTML)
        b, works, snap = a.fetch_account(a.cfg["own_accounts"][0])
        self.assertEqual(b.nickname, "测试抖音号")
        self.assertEqual(b.followers, 128000)
        self.assertEqual(b.total_likes, 3500000)
        self.assertEqual(len(works), 2)
        self.assertEqual(works[0].views, 120000)
        self.assertIsNotNone(snap, "粉丝数公开可见时应写 1 个快照点")
        self.assertEqual(a.audit[0]["category"], "own")

    def test_whitelist_refuses_unlisted(self):
        from engine.crawler import WhitelistError
        a = self._douyin(self.DOUYIN_HTML)
        with self.assertRaises(WhitelistError):
            a.fetch_account({"platform": "抖音", "handle": "not_in_list"})

    def test_xhs_parse_with_wan_unit(self):
        from engine.crawler import XiaohongshuWebAdapter
        cfg = {"own_accounts": [{"platform": "小红书", "handle": "u123", "industry": "美妆"}],
               "acknowledged_risk_accounts": []}
        a = XiaohongshuWebAdapter(cfg, transport=lambda m, u, h: self.XHS_HTML)
        a.limiter.min_interval = 0
        b, works, snap = a.fetch_account(cfg["own_accounts"][0])
        self.assertEqual(b.nickname, "测试小红书号")
        self.assertEqual(b.followers, 56000)
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0].views, 123000, "12.3万 应解析为 123000")

    def test_risk_account_requires_ack(self):
        from engine.crawler import load_whitelist, WhitelistError
        import tempfile
        bad = {"own_accounts": [], "acknowledged_risk_accounts": [
            {"platform": "抖音", "handle": "x", "industry": "美妆"}]}  # 缺 ack_by/ack_at
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(WhitelistError):
                load_whitelist(p)

    def test_graceful_when_empty_html(self):
        a = self._douyin("")  # 模拟登录墙/骨架页
        b, works, snap = a.fetch_account(a.cfg["own_accounts"][0])
        self.assertEqual(b.followers, 0, "拿不到粉丝数不得伪造")
        self.assertIsNone(snap)
        self.assertIn("未返回结构化粉丝数", a.audit[0]["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
