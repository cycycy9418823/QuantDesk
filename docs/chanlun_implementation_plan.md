# Chanlun Implementation Plan

QuantDesk treats Chanlun content as local learning material and a source for
technical-analysis rule research. It does not connect to brokers, does not
place real orders, does not save real account information, and does not provide
investment advice.

The v3.0.1 stage builds an index, a concept table, and a roadmap. The v3.0.2
stage adds only the first programmable layer: fractal structure observation. In
QuantDesk, any later "buy/sell point" wording must be rendered as an
observation signal, structure observation, paper simulation signal, or learning
signal.

## Stage 1: Knowledge Base And Rule Index

Version: `v3.0.1-chanlun-knowledge-base-and-rule-roadmap`

Goals:

- Register local PDF learning material.
- Build a 108-lesson metadata index.
- Build the core concept table.
- Build a programmable-rule roadmap.
- Do not save full text and do not generate trading signals.

Outputs:

- `data/knowledge/chanlun_lessons_index.json`
- `data/knowledge/chanlun_concepts.json`
- `data/knowledge/chanlun_rule_roadmap.json`
- `reports/chanlun_knowledge_summary_*.md`

## Stage 2: Fractal Recognition Engine

Version: `v3.0.2-chanlun-fractal-engine`

Status: implemented

Goals:

- Input OHLC bars.
- Identify top fractals and bottom fractals.
- Handle inclusion relationships.
- Output structure points.
- Do not make trading decisions.

Implemented outputs:

- `results/chanlun_fractals_latest.json`
- `data/chanlun/fractals/{symbol}_{timeframe}_fractals.json`
- `reports/chanlun_fractal_report_*.md`

The runtime outputs are excluded from the clean zip. This stage does not build
bi, segments, zhongshu, divergence, or trading actions.

## Stage 3: Bi Recognition Engine

Version: `v3.0.3-chanlun-bi-engine`

Status: implemented.

Goals:

- Build strokes from confirmed fractals.
- Output start point, end point, direction, and strength proxies.
- Support minimum interval rules.
- Mark uncertain areas.

Implemented scope:

- Read v3.0.2 top/bottom fractals or generate them from local OHLC when no fractal file is supplied.
- Merge consecutive same-type fractals by keeping the more extreme top or bottom.
- Build up/down bi observations only from alternating bottom/top or top/bottom fractals.
- Reject candidate bi when the configurable minimum K-line separation is not met.
- Output diagnostics, rejected candidates, JSON artifacts, CLI, API, UI, and learning report.
- Bi observations are structure-learning outputs only. They are not buy/sell points, are not connected to the automatic paper portfolio, and do not constitute investment advice.

## Stage 4: Segment Recognition Engine

Version: `v3.0.4-chanlun-segment-engine`

Status: implemented.

Goals:

- Build segments from strokes.
- Output segment direction, start/end points, and break status.
- Keep the result as structural research, not a direct trade action.

Implemented scope:

- Read v3.0.3 bi results or generate them from local OHLC/fractals when no bi file is supplied.
- Filter effective bi, keep confirmed bi by default, and optionally include provisional bi.
- Merge consecutive same-direction bi by keeping the more extreme endpoint.
- Build up/down segment observations with configurable minimum bi count.
- Record break observation fields such as `break_status`, `break_type`, `break_date`, and `break_price`.
- Output diagnostics, rejected segment candidates, JSON artifacts, CLI, API, UI, and learning report.
- Segment observations are structure-learning outputs only. They are not buy/sell points, are not connected to the automatic paper portfolio, and do not constitute investment advice.

## Stage 5: Zhongshu Recognition Engine

Version: `v3.0.5-chanlun-zhongshu-engine`

Goals:

- Identify zhongshu ranges from strokes or segments.
- Output upper edge, lower edge, and middle axis.
- Mark extension, expansion, departure, and pullback.

## Stage 6: Divergence Observation

Version: `v3.0.6-chanlun-divergence-observation`

Goals:

- Combine structure and MACD-style auxiliary fields.
- Output divergence observations.
- Do not present observations as trading advice.

## Stage 7: Historical Replay Validation

Version: `v3.0.7-chanlun-replay-validation`

Goals:

- Put observation signals into walk-forward historical replay.
- Count subsequent structural behavior and paper-simulation outcomes.
- Do not promise returns and do not place real orders.

## Safety Notes

- 本模块仅用于学习和规则研究，不构成投资建议。
- 买卖点在 QuantDesk 中仅作为观察信号，不代表买卖建议。
- Do not store the PDF full text in repository artifacts.
- Do not put original PDF files into the clean zip.
- Any future implementation must avoid future-data leakage.
