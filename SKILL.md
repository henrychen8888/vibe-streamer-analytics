---
name: vibe-streamer-analytics
description: "Vibe Streamer Analytics — a zero-third-party-dependency, multi-platform creator and streamer data analysis engine. This skill should be used when the user wants to analyze Douyin, Xiaohongshu, Kuaishou, or Bilibili creators: building data dashboards, importing real export data such as CSV or JSON ledgers, compliantly crawling public profile data, connecting official OAuth, or generating creator analytics reports covering industry ranking, content analysis, fan growth, comment mining, e-commerce, and monetization evaluation."
agent_created: true
---

# Vibe Streamer Analytics

## Overview

A self-contained creator/streamer analytics engine that turns multi-platform account data
into a high-fidelity offline dashboard. The engine is pure Python standard library
(no pandas/streamlit/requests required), ships an offline single-file HTML dashboard
(no CDN, double-click to open), and enforces strict data-compliance boundaries.

The bundled `scripts/` directory contains the full engine (`run.py` + `engine/`), the
dashboard template (`web/`), CSV import templates (`import_template/`), and example
credential/whitelist files. Everything resolves relative to `run.py`'s own directory, so
the skill runs unchanged once deployed locally.

## When To Use

Trigger this skill when the user asks to:

- Analyze a Douyin / Xiaohongshu / Kuaishou / Bilibili creator or a batch of creators.
- Build a streamer "数据看板 / 仪表盘" with industry ranking, content, fan, comment,
  e-commerce, or monetization views.
- Import their own exported creator data (Excel/CSV/JSON ledgers) for analysis.
- Compliantly collect public profile data of their own accounts or acknowledged-risk
  competitor accounts.
- Connect official platform OAuth (Douyin) to pull an authorized account's own data.
- Generate a creator diagnostic / 变现能力 / 竞品对标 report.

Do NOT use this skill for real-time scraping that bypasses login walls, proxy pools,
UA rotation, signature cracking, or any anti-bot evasion — those are explicitly excluded
by design (see Compliance Boundaries).

## Quick Start

The bundled scripts must run from a local working directory (so outputs land in the
user's project, not inside the skill). To deploy and smoke-test:

1. Copy the skill's `scripts/` directory into the user's workspace as `vsa/`.
   Keep `run.py`, `engine/`, `web/`, `import_template/`, and the two `*.example.json`
   files together — `run.py` resolves all paths relative to itself.
2. Generate demo data and build the dashboard:
   `python3 vsa/run.py`  (runs the full chain: demo → analyze → export → dashboard)
3. Open `vsa/dashboard.html` in a browser. It is a single offline file.
4. Verify the engine with `python3 vsa/run.py selftest` (expect all tests pass).

## Capabilities & Commands

All commands are invoked as `python3 vsa/run.py <command> [args]`. Outputs go to
`vsa/out/` (SQLite + JSON + 5 CSVs) and `vsa/dashboard.html`.

### 1. Demo / Smoke Test
- `python3 vsa/run.py` — synthetic demo data → analysis → exports → dashboard.
- `python3 vsa/run.py demo --seed 42 --per-industry 8` — reproducible seed / per-industry count.
- `python3 vsa/run.py analyze` — re-analyze data already in the database.

### 2. Import Real Data (preferred for real accounts)
The dashboard supports three data sources; real data is loaded via CSV/JSON ledgers.
- `python3 vsa/run.py template [DIR]` — generate 5 CSV templates + field dictionary.
- `python3 vsa/run.py validate DIR` — pre-import health check (blocks dirty data:
  undeleted EXAMPLE rows, missing required columns, duplicate primary keys, broken
  foreign keys, unparseable numbers/dates, privacy column names; warns on low coverage,
  stale data >30 days, anomalous interaction).
- `python3 vsa/run.py import DIR` — validate → desensitize (phone/ID/email/WeChat) →
  deterministic backfill → analyze → rebuild dashboard. The dashboard's data-source
  badge switches from "合成演示数据" to "授权数据".

### 3. Compliant Public-Page Crawling
- `python3 vsa/run.py crawl` — collect public profile data for whitelisted accounts.
- Whitelist file: `crawl_whitelist.json` (copy from `crawl_whitelist.example.json`).
  `own_accounts` run by default; `acknowledged_risk_accounts` require explicit
  `ack_by` / `ack_at` fields or the crawler refuses them.
- The crawler uses a single identifiable User-Agent, a shared rate limiter (default 4s/
  request), parses only page-public metrics, and records an audit trail. It never uses
  proxy pools, UA rotation, fingerprint spoofing, signature cracking, or CAPTCHA solving.

### 4. Official OAuth (Douyin)
- `python3 vsa/run.py oauth` — print the official authorization link.
- `python3 vsa/run.py oauth --code CODE` — exchange code for token, write `config.json`.
- `python3 vsa/run.py sync` — pull the authorized account's OWN data via OAuth.
- `python3 vsa/run.py scopes` — print official scope catalog and authorization boundaries.
- Note: OAuth returns only the authorized account's own data (1 follower-snapshot point);
  comments / products / membership details still require the `import` ledger path.

### 5. Deliverables & Self-Check
- `python3 vsa/run.py verify` — CSV ↔ SQLite row consistency + HTML offline-self-contained check.
- `python3 vsa/run.py selftest` — unit tests (currently 33 cases).
- `vsa/dashboard.html` — offline dashboard: 10 sections (KPI, industry, fan growth,
  works, comments, e-commerce, membership, monetization, insights, data-source config),
  light/dark themes, CSV export, Ctrl+K search, metric-definition drawer.

## Compliance Boundaries (enforced by design)

- Real platform data requires the user's own authorization (OAuth) or authorized export.
  The engine never fabricates data it cannot obtain — missing fields render as graceful
  empty states, never a fake "50/100".
- No anti-bot evasion: no proxy pools, UA rotation, fingerprint spoofing, signature
  cracking (X-Bogus / x-s / x-t), or CAPTCHA solving.
- No personal-information collection: private messages, phone numbers, non-public
  comments are out of scope (PIPL). The `import` path auto-desensitizes PII.
- Third-party data is for internal analysis only; do not resell (precedent cases exist).

## Resources

- `scripts/run.py` — CLI entry point (all commands above).
- `scripts/engine/` — schema, adapters (OAuth + local export + crawler), seed, store
  (SQLite/audit), nlp (Chinese sentiment), analytics (7 dimensions), insights, report,
  validate, reconcile, crawler.
- `scripts/web/index.template.html` — dashboard template (injection point `/*__PAYLOAD__*/`).
- `scripts/import_template/` — 5 CSV templates with `EXAMPLE-` rows (delete before import).
- `scripts/config.example.json` — Douyin OAuth credential template.
- `scripts/crawl_whitelist.example.json` — crawler whitelist template.
- `scripts/tests/test_engine.py` — unit test suite.

For the full audit, metric definitions, compliance channel matrix, and the revised
roadmap, load `references/guide.md`.
