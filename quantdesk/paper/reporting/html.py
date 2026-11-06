"""Render paper-account HTML from supplied query results, without I/O."""
from __future__ import annotations

import html
import json
from typing import Any


def render_swing_execution_html(latest: dict[str, Any]) -> str:
    title = "长线波段计划与盘中触发回放"
    if latest.get("exists"):
        body = html.escape(json.dumps(latest, ensure_ascii=False, indent=2, default=str))
        meta = f"生成时间：{html.escape(str(latest.get('generated_at') or '-'))}"
    else:
        body = html.escape(str(latest.get("message") or "尚未生成长线波段计划。"))
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:960px;margin:2em auto;padding:0 1em;line-height:1.6}}pre{{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:1em;border-radius:8px}}button{{padding:0.5em 1.2em;cursor:pointer}}.disclaimer{{color:#666;font-size:0.9em;margin:1em 0}}</style>
</head>
<body>
  <main>
    <button type="button" onclick="window.print()">打印 / 浏览器另存为 PDF</button>
    <h1>{html.escape(title)}</h1>
    <p>{meta}</p>
    <p class="disclaimer">本报告仅用于本地策略学习与模拟验证，不构成投资建议。盘中回放按分钟线顺序执行，避免未来函数；缺少分钟线时不会用日线假装成交。</p>
    <pre>{body}</pre>
  </main>
</body>
</html>"""



def render_auto_paper_portfolio_html(
    latest: dict[str, Any], *, disclaimer: str, broker_boundary: str,
) -> str:
    title = "自动虚拟组合报告"
    if latest.get("exists"):
        body = html.escape(json.dumps(latest, ensure_ascii=False, indent=2, default=str))
        meta = f"生成时间：{html.escape(str(latest.get('generated_at') or '-'))}"
    else:
        body = html.escape(str(latest.get("message") or "尚未初始化自动虚拟组合。"))
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:960px;margin:2em auto;padding:0 1em;line-height:1.6}}pre{{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:1em;border-radius:8px}}button{{padding:0.5em 1.2em;cursor:pointer}}.disclaimer{{color:#666;font-size:0.9em;margin:1em 0}}</style>
</head>
<body>
  <main>
    <button type="button" onclick="window.print()">打印 / 另存为 PDF</button>
    <h1>{html.escape(title)}</h1>
    <p>{meta}</p>
    <p class="disclaimer">{html.escape(disclaimer)} {html.escape(broker_boundary)}</p>
    <pre>{body}</pre>
  </main>
</body>
</html>"""

