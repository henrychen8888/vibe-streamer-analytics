# Vibe Streamer Analytics · 使用与合规指南

本文件是 skill 的详细参考，覆盖架构、指标口径、数据来源合规矩阵、导入字段字典与已知边界。
快速上手见 `SKILL.md`。所有路径均以本地部署后的 `vsa/` 为基准（即 `scripts/` 复制后的目录）。

---

## 一、系统架构

```
CLI (run.py)
  ├─ 数据采集层   demo(合成) / import(台账) / crawl(公开页) / oauth+sync(官方)
  ├─ 仓储层       SQLite(analytics.db) + 审计表 + 数据水位
  ├─ 分析引擎     七大维度 + 指标口径(实测/派生/估算三态)
  ├─ NLP          中文分词 + 情感 + 争议点识别(无第三方依赖)
  ├─ 洞察         规则化结论(带证据与可信度)
  └─ 交付         CSV / JSON / 单文件离线 HTML(dashboard.html)
```

设计原则：**零第三方依赖**（纯标准库可跑）、**离线可用**（看板无 CDN）、**合规优先**
（不伪造、不规避反爬、自动脱敏）。

---

## 二、七大分析维度与口径

每个数字都带可信度标签：`实测 observed` / `派生 derived` / `估算·低置信度 estimated`。

| # | 维度 | 关键指标 | 口径要点 |
| --- | --- | --- | --- |
| ① | 行业分类与榜单 | 平均粉丝、涨粉榜、掉粉预警、互动率榜 | 按垂类+粉丝量级分层；中位数+P5/P95 缩尾 |
| ② | 作品分析 | 发布时段热力、爆款特征、互动率 | 仅统计播放量>0 的作品；最佳时段需样本≥3 |
| ③ | 粉丝增长 | 增长曲线、30日净增、增速拐点 | 需≥2个粉丝快照；缺快照标"不可用"不补均值 |
| ④ | 评论挖掘 | 情感分布、高频词、争议点、回复率 | 自研词典法；空串安全；不依赖 SnowNLP |
| ⑤ | 橱窗电商 | 商品数、品类、估算 GMV、带货评级 | GMV 为**估算·低置信度**；销量基于销量标识 |
| ⑥ | 会员体系 | 入会转化、内容更新频率 | 仅抖音；无数据则空态 |
| ⑦ | 变现能力 | 广告+带货+会员加权分、对标排名 | 多维加权；无数据维度记 0 分 |

---

## 三、数据来源与合规矩阵

| 来源 | 命令 | 合规姿态 | 能拿到的数据 |
| --- | --- | --- | --- |
| 合成演示 | `run.py` | 零风险(固定种子) | 全维度(用于验证口径) |
| CSV/JSON 台账导入 | `template`→`validate`→`import` | 你授权导出的自有数据 | 全维度(最稳) |
| 合规公开页采集 | `crawl` | 仅页面公开可见指标；限速；白名单 | 粉丝/获赞/作品数/公开作品列表+1快照 |
| 官方 OAuth(抖音) | `oauth`→`sync` | 平台授权；仅授权账号自身 | 作品/粉丝(1快照点) |
| 第三方 DAAS(蝉妈妈/千瓜/新榜) | 有 key 写适配器，否则导出 CSV→`import` | 企业合作；内部分析 | 电商/竞品深度 |

**明确拿不到 / 不伪造**：完播率(无对外接口)、非公开评论、私信、手机号等 PII。
`import` 路径会自动脱敏手机号/证件/邮箱/微信号并留审计。

---

## 四、导入字段字典（import_template/）

5 张表，主键/外键如下；时间字段用 ISO（`2026-11-09` 或 `2026-11-09T20:30`）。

- **bloggers.csv**（必需）：`id, platform, nickname, industry, followers[, followers_7d, followers_30d, total_likes, source]`
  - `platform` ∈ {抖音, 小红书, 快手, B站}；`followers_7d/30d` 缺失时由快照回填，再缺失标"不可用"。
- **works.csv**：`id, blogger_id, title, publish_at, duration_s, views, likes, comments, shares, tag`
  - `blogger_id` 必须存在于 bloggers；`views<=0` 计入但剔除出互动率。
- **comments.csv**：`id, work_id, blogger_id, content, likes, replied`
- **products.csv**：`id, blogger_id, name, category, price, sold_display, rating`
- **fan_snapshots.csv**：`blogger_id, date, followers`（≥2 条才有增长分析）

> 只有 `bloggers.csv` 必需；其余缺失自动降级为空态，不报错。

---

## 五、采集白名单格式（crawl_whitelist.example.json）

```json
{
  "own_accounts": [
    {"platform": "抖音", "handle": "账号标识", "industry": "美妆"}
  ],
  "acknowledged_risk_accounts": [
    {"platform": "小红书", "handle": "竞品号", "industry": "美妆",
     "ack_by": "操作人", "ack_at": "2026-11-09"}
  ]
}
```

- `own_accounts`：默认采集。
- `acknowledged_risk_accounts`：竞品/行业账号，**必须显式填 `ack_by`/`ack_at`**，否则直接拒绝并提示风险自担。
- `crawl_whitelist.json` 含账号信息，请勿提交版本库。

---

## 六、已知边界与修订后路线图

**已移除/推迟（相对原方案的修正）**：
- 删除"IP 代理池 / UA 轮换 / 降低被识别概率"等规避反爬手段（合规冲突）。
- 删除 `pyvibe.enable()` / `vb.Style` 全局主题等不实 API（已核实包真实行为）。
- 完播率"预估模型"改为明确列入不可得清单。
- SQLite→PostgreSQL"无缝升级"改为按需替换仓储层。

**未实现（需至少两个真实采集周期）**：竞品自动对标、ARIMA/Prophet 趋势预测、LLM 报告润色——接口已预留。

**发布/部署**：本 skill 即部署单元；`python3 vsa/run.py package` 不存在，打包用 skill-creator
的 `package_skill.py`。仪表盘为离线单文件，可直接发给同事双击打开，但**演示数据严禁外发为业务结论**。
