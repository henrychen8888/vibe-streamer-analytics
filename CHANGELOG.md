# Changelog

本项目的所有重要变更都会记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

（暂无）

## [1.0.0] - 2026-11-09

### 新增
- 多平台创作者分析引擎：抖音 / 小红书 / 快手 / B站，零第三方依赖（纯 Python 标准库）。
- 数据接入四条路径：
  - 合成演示数据（`run.py` / `demo`，支持 `--seed`、`--per-industry`）；
  - 自有台账导入（`template` → `validate` → `import`），导入前体检拦截脏数据，导入时自动脱敏手机号/身份证/邮箱/微信；
  - 公开页合规采集（`crawl`），仅白名单账号，单 UA + 默认 4s/请求限速 + 审计留痕；
  - 抖音官方 OAuth（`oauth` → `sync` → `scopes`），仅拉取授权账号自身数据。
- 分析维度（7 类）：行业排名、内容、粉丝增长、评论情感（中文 NLP）、电商、会员、变现评估。
- 离线看板：单文件 HTML（无 CDN），10 个模块（KPI / 行业 / 粉丝 / 作品 / 评论 / 电商 / 会员 / 变现 / 洞察 / 数据源），浅色与深色主题、CSV 导出、`Ctrl+K` 搜索、指标口径抽屉。
- 交付自检：`verify`（CSV ↔ SQLite 一致性 + HTML 自包含校验）、`selftest`（33 个单元测试）。
- 合规边界内建：不做反爬对抗（无代理池 / UA 轮换 / 指纹伪造 / 签名破解 / 验证码识别），不采集个人信息。

[Unreleased]: https://github.com/henrychen8888/vibe-streamer-analytics/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/henrychen8888/vibe-streamer-analytics/releases/tag/v1.0.0
