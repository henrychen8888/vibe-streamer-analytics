"""Vibe Streamer Analytics · 多平台主播数据分析引擎（零第三方依赖）。

模块地图：
  schema      数据契约 + 可信度三态
  adapters    合规采集适配器（官方 OAuth / 本地授权导出 / 演示数据）
  seed        可复现合成数据
  store       SQLite 仓储 + 审计日志 + 水位
  nlp         中文分词 / 情感 / 热词 / 争议识别
  analytics   七大维度 + 统一指标口径
  insights    带证据数字的规则化洞察
  report      CSV / JSON / 自包含 HTML 输出与一致性核对
"""
__version__ = "2.0.0"
__all__ = ["schema", "adapters", "seed", "store", "nlp", "analytics", "insights", "report"]
