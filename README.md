# Vibe Streamer Analytics

多平台创作者 / 主播数据分析引擎 —— **零第三方依赖**，纯 Python 标准库，产物是**单文件离线 HTML 看板**（无 CDN，双击即开）。

覆盖抖音 / 小红书 / 快手 / B站 的创作者分析：行业排名、内容分析、粉丝增长、评论挖掘、电商与变现评估。

![python](https://img.shields.io/badge/python-3.8%2B-blue) ![deps](https://img.shields.io/badge/依赖-零第三方-green) ![output](https://img.shields.io/badge/产物-离线单页HTML-orange)

---

## 三步上手

```bash
# 1. 把 skill 的 scripts/ 目录复制到你的工作区，命名为 vsa/
cp -R scripts vsa

# 2. 跑通全链路（合成演示数据 → 分析 → 导出 → 看板）
python3 vsa/run.py

# 3. 打开 vsa/dashboard.html（单文件，离线可看）
```

自检：`python3 vsa/run.py selftest`（33 个用例）

## 数据来源（三条路径）

| 路径 | 命令 | 说明 |
|------|------|------|
| 合成演示 | `run.py` / `run.py demo` | 无需任何数据，快速看效果 |
| 自有台账导入 | `run.py template` → `validate` → `import` | 真实分析首选；自动脱敏 PII、体检拦截脏数据 |
| 公开页采集 | `run.py crawl` | 仅白名单账号，单 UA + 限速 + 审计留痕 |
| 官方 OAuth（抖音） | `run.py oauth` → `sync` | 仅拉取授权账号自身数据 |

## 命令一览

| 命令 | 作用 |
|------|------|
| `run.py` | 演示全链路 |
| `run.py demo --seed 42 --per-industry 8` | 可复现演示数据 |
| `run.py analyze` | 重新分析库内数据 |
| `run.py template [DIR]` | 生成 5 张 CSV 导入模板 + 字段字典 |
| `run.py validate DIR` | 导入前体检（字段/类型/外键/隐私/新鲜度） |
| `run.py import DIR` | 体检 → 脱敏回填 → 分析 → 重建看板 |
| `run.py crawl` | 白名单账号公开页采集 |
| `run.py oauth [--code CODE]` | 官方授权换取 token |
| `run.py sync` | 拉取授权账号自身数据 |
| `run.py scopes` | 打印官方 scope 目录与授权边界 |
| `run.py verify` | 交付前一致性核对（CSV ↔ SQLite + HTML 自包含） |
| `run.py selftest` | 单元测试 |

## 看板能力

10 个模块：KPI、行业、粉丝增长、作品、评论、电商、会员、变现、洞察、数据源配置；
浅色/深色主题、CSV 导出、`Ctrl+K` 搜索、指标口径抽屉。

## 合规边界（设计即约束）

- 真实数据须经**本人授权**（OAuth）或**授权导出台账**入表；缺失字段渲染为空态，绝不伪造。
- **不做反爬对抗**：无代理池、无 UA 轮换、无指纹伪造、无签名破解（X-Bogus / x-s / x-t）、无验证码识别。
- **不采集个人信息**：私信、手机号、非公开评论不在范围内（PIPL）；`import` 路径自动脱敏。
- 第三方数据仅供内部研判，不得转售。

详细架构、指标口径、合规渠道矩阵见 [`references/guide.md`](references/guide.md)。

## 目录结构

```
SKILL.md                          # 技能说明（触发条件、命令、资源清单）
references/guide.md               # 详细参考：架构 / 指标口径 / 合规矩阵 / 字段字典
scripts/run.py                    # CLI 入口
scripts/engine/                   # schema / adapters / seed / store / nlp / analytics /
                                  # insights / report / validate / reconcile / crawler
scripts/web/index.template.html   # 看板模板（注入点 /*__PAYLOAD__*/）
scripts/import_template/          # 5 张 CSV 模板（含 EXAMPLE- 行，导入前删除）
scripts/config.example.json       # 抖音 OAuth 凭据模板
scripts/crawl_whitelist.example.json  # 采集白名单模板
scripts/tests/test_engine.py      # 单元测试
```
