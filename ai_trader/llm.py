"""LLM brain for the independent AI paper trader (optional, fail-open-to-rules).

The trader can run purely on rules (researcher.py). When a local
Anthropic-compatible endpoint is configured, this module lets the trader
*think*: it proposes which names to buy and why, in the trader's persona.

Hard contract:
- This module NEVER decides fills, costs, limits, or ledger math. It only
  returns candidate picks + reasons; overnight.build_plan still applies every
  lot/cash/turnover/limit/next-open constraint and the ledger stays truthful.
- ANY failure (no config, network error, timeout, bad JSON) returns None so the
  nightly run silently falls back to the rule engine. It must never raise or
  hang the cycle.
- The token is read from env or the gitignored state/ai_trader/llm.env. It is
  never logged, never written to journals/results.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ai_trader.paths import STATE_DIR
from ai_trader.prompt_pack import load_prompt_pack

DEFAULT_MODEL = "claude-opus-5"
# Wire-protocol identifiers. ``anthropic`` = /v1/messages dialect (local
# relay); ``openai`` = chat/completions dialect (generic OpenAI-compatible
# relays such as an explicitly-opted external host).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
# Endpoints bound only to these hosts are one's own machine: no extra gate.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The explicit user opt-in required before ANY non-loopback endpoint may be
# called. Lives in the gitignored llm.env (or the running process env); never
# in source or tests as a real value.
EXTERNAL_ALLOW_ENV = "AI_TRADER_EXTERNAL_LLM_ALLOWED"
DEFAULT_TIMEOUT_S = 300
# Extended thinking is billed against max_tokens, and on a full 40-candidate
# prompt it alone measured 6.7k. At an 8k ceiling the answer fits only sometimes:
# when thinking runs long the response stops at max_tokens with a thinking block
# and no text block, which looks exactly like "endpoint down" and silently drops
# the trader back onto the rule engine. Headroom is far cheaper than that.
DEFAULT_MAX_TOKENS = 24000
ENV_FILE = STATE_DIR / "llm.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style ``export KEY="value"`` file without executing it."""

    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                out[key] = value
    except OSError:
        return {}
    return out


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _endpoint_host(base_url: str) -> str:
    """Hostname only — never credentials, query string or port leakage."""

    try:
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname
    except Exception:
        host = None
    return str(host or "").strip().lower()


def load_llm_config(
    explicit_optin: bool = False,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Resolve endpoint + token + protocol, but ONLY when explicitly enabled.

    The LLM brain is opt-in: it activates only when ``AI_TRADER_LLM_ENABLED`` is
    truthy in the environment (set by the scheduled-run launcher). Merely having
    credentials on disk is not enough — this keeps unit tests and any incidental
    run fully offline on the rule engine, honouring "no default LLM/network".
    Returns None when disabled or unconfigured, the signal to run on rules only.
    ``diagnostics`` receives the concrete reason so a blocked external endpoint
    is never mistaken for a silent fallback.

    ``explicit_optin`` covers the interactive case: a person typing a question and
    pressing send IS the opt-in, so the env flag (which exists to stop *unattended*
    runs from reaching the network on their own) should not also be required. It
    only bypasses the flag — credentials must still be present, so this can never
    turn an unconfigured install into a network caller.

    External-endpoint gate: an endpoint whose host is not loopback must carry
    an explicit ``AI_TRADER_EXTERNAL_LLM_ALLOWED`` truthy value (env or file)
    before any call may be made. Loopback endpoints keep the original logic and
    need no extra flag. A blocked external endpoint fails closed to the rule
    engine with reason ``external_llm_not_explicitly_allowed``.
    """

    def _reason(name: str) -> None:
        if diagnostics is not None and not diagnostics.get("reason"):
            diagnostics["reason"] = name

    parsed = _parse_env_file(ENV_FILE)
    # The opt-in gate deliberately comes from the live process only.  The file
    # may store credentials (and may contain a historical enable line), but its
    # mere presence must never turn an incidental run or a test into a network
    # caller.  The scheduled launcher sets this variable explicitly.
    enabled = os.environ.get("AI_TRADER_LLM_ENABLED", "").strip()
    if not explicit_optin and not _truthy(enabled):
        _reason("not_enabled_or_unconfigured")
        return None
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    model = os.environ.get("AI_TRADER_LLM_MODEL", "").strip()
    provider = os.environ.get("AI_TRADER_LLM_PROVIDER", "").strip()
    if not base or not token:
        base = base or parsed.get("ANTHROPIC_BASE_URL", "").strip()
        token = token or parsed.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        model = model or parsed.get("AI_TRADER_LLM_MODEL", "").strip()
        provider = provider or parsed.get("AI_TRADER_LLM_PROVIDER", "").strip()
    if not base or not token:
        _reason("not_configured_missing_credentials")
        return None
    host = _endpoint_host(base)
    is_external = bool(host and host not in LOOPBACK_HOSTS)
    if is_external:
        allowed = os.environ.get(EXTERNAL_ALLOW_ENV, "").strip()
        if not allowed:
            allowed = parsed.get(EXTERNAL_ALLOW_ENV, "").strip()
        if not _truthy(allowed):
            _reason("external_llm_not_explicitly_allowed")
            return None
    resolved = (provider or PROVIDER_ANTHROPIC).strip().lower()
    if resolved not in {PROVIDER_ANTHROPIC, PROVIDER_OPENAI}:
        resolved = PROVIDER_ANTHROPIC
    if resolved == PROVIDER_OPENAI and not model:
        _reason("openai_provider_model_missing")
        return None
    return {
        "base_url": base.rstrip("/"),
        "token": token,
        "model": model or DEFAULT_MODEL,
        "provider": resolved,
    }


def llm_runtime_descriptor() -> dict[str, Any]:
    """Secret-free description of the configured LLM runtime.

    Read-only, no gate and no network: describes what the system WOULD call.
    The token is never included (``token`` is always null, no Authorization
    header, no ``llm.env`` original text). ``endpoint_host`` carries the
    hostname only.
    """

    parsed = _parse_env_file(ENV_FILE)
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or parsed.get(
        "ANTHROPIC_BASE_URL", ""
    ).strip()
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() or parsed.get(
        "ANTHROPIC_AUTH_TOKEN", ""
    ).strip()
    model = os.environ.get("AI_TRADER_LLM_MODEL", "").strip() or parsed.get(
        "AI_TRADER_LLM_MODEL", ""
    ).strip()
    provider = os.environ.get("AI_TRADER_LLM_PROVIDER", "").strip() or parsed.get(
        "AI_TRADER_LLM_PROVIDER", ""
    ).strip()
    allowed = os.environ.get(EXTERNAL_ALLOW_ENV, "").strip() or parsed.get(
        EXTERNAL_ALLOW_ENV, ""
    ).strip()
    host = _endpoint_host(base)
    resolved = (provider or PROVIDER_ANTHROPIC).strip().lower()
    if resolved not in {PROVIDER_ANTHROPIC, PROVIDER_OPENAI}:
        resolved = PROVIDER_ANTHROPIC
    external = bool(host) and host not in LOOPBACK_HOSTS
    return {
        "configured": bool(base and token),
        "provider": resolved,
        "model": model or ("" if resolved == PROVIDER_OPENAI else DEFAULT_MODEL),
        "endpoint_host": host,
        "is_external": external,
        "token_present": bool(token),
        "token": None,
        "external_allowed": _truthy(allowed),
        "context_disclosure": [
            "paper account summary",
            "paper positions",
            "candidate symbols and recent changes",
            "trader stance and lessons",
            "recent journal",
            "bounded chat history",
            "current user question",
        ],
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a text block (tolerates code fences)."""

    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Shared transport: one POST, parsed JSON or None with a real reason."""

    def _note(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_S) as response:
            if diagnostics is not None:
                diagnostics["http_status"] = int(getattr(response, "status", 200))
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if diagnostics is not None:
            diagnostics["http_status"] = int(exc.code)
        _note(f"transport:{type(exc).__name__}")
        return None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as exc:
        _note(f"transport:{type(exc).__name__}")
        return None


def _call_openai_compatible(
    system: str,
    user: str,
    cfg: dict[str, str],
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """OpenAI chat-completions dialect.

    Reasoning relays put their thinking in ``message.reasoning_content`` and the
    answer in ``message.content``; only the latter is an answer, so that is all
    this parser accepts.
    """

    def _note(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason

    base = cfg["base_url"].rstrip("/")
    url = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    payload = _post_json(
        url,
        {
            "model": cfg["model"],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        {
            "authorization": f"Bearer {cfg['token']}",
            "content-type": "application/json",
            "accept": "application/json",
        },
        diagnostics,
    )
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    text = ""
    finish = ""
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = str(message.get("content") or "").strip()
        finish = str(first.get("finish_reason") or "")
    if not text:
        usage = payload.get("usage") or {}
        _note(
            f"no_text_block:finish={finish or 'unknown'}"
            f":output_tokens={usage.get('completion_tokens')}"
            f":max_tokens={DEFAULT_MAX_TOKENS}"
        )
        return None
    if diagnostics is not None:
        usage = payload.get("usage") or {}
        diagnostics["output_tokens"] = usage.get("completion_tokens")
    return text


def _call_anthropic_compatible(
    system: str,
    user: str,
    cfg: dict[str, str],
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """Anthropic /v1/messages dialect (the original local relay path)."""

    def _note(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason

    payload = _post_json(
        f"{cfg['base_url']}/v1/messages",
        {
            "model": cfg["model"],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {
            "authorization": f"Bearer {cfg['token']}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        diagnostics,
    )
    if not isinstance(payload, dict):
        return None
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        _note("no_content_blocks")
        return None
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = "\n".join(part for part in texts if part).strip()
    if not joined:
        stop = str(payload.get("stop_reason") or "unknown")
        usage = payload.get("usage") or {}
        _note(
            f"no_text_block:stop={stop}:output_tokens={usage.get('output_tokens')}"
            f":max_tokens={DEFAULT_MAX_TOKENS}"
        )
        return None
    if diagnostics is not None:
        usage = payload.get("usage") or {}
        diagnostics["output_tokens"] = usage.get("output_tokens")
    return joined


def _call(
    system: str,
    user: str,
    cfg: dict[str, str],
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """POST one message to the configured endpoint; text or None.

    ``diagnostics`` is filled with why a call failed. Without it a token-exhausted
    response and an unreachable endpoint are indistinguishable to the caller, and
    the trader degrades to rules with no trace of having tried.
    """

    if cfg.get("provider") == PROVIDER_OPENAI:
        return _call_openai_compatible(system, user, cfg, diagnostics)
    return _call_anthropic_compatible(system, user, cfg, diagnostics)


def _build_prompt(context: dict[str, Any]) -> tuple[str, str]:
    persona = str(context.get("persona_zh") or "一个自己拿主意的 A 股散户")
    pack = load_prompt_pack()
    system = pack.render_system(persona)
    lines = [
        f"今天(决策日)：{context.get('as_of')}，为次日 {context.get('trade_date')} 开盘布置。",
        f"账户：权益 {context.get('equity')}，现金 {context.get('cash')}，"
        f"当前持仓 {context.get('holdings_count')} 只。账户硬约束：{context.get('mandate_note')}",
    ]
    stance = context.get("stance") or {}
    if stance.get("summary"):
        lines.append(
            f"你上次(自己写的)投资主张：{stance['summary']}"
            f"（偏好：{'、'.join(stance.get('preferred_styles') or []) or '未定'}）。"
            "还这么想就沿用，想改就改并说明理由。"
        )
    else:
        lines.append("你还没写下自己的投资主张——今天可以第一次定下来。")
    scoreboard = context.get("style_scoreboard") or []
    if scoreboard:
        lines.append(
            "你自己各种做法到目前的真实战绩(这是你的账本,不是别人的观点)："
            + "；".join(scoreboard)
        )
    holdings = context.get("holdings") or []
    if holdings:
        lines.append("持仓明细(含你当初打算的做法和持有天数)：" + "；".join(holdings))
        lines.append(
            "对持仓你可以：继续拿着(不写进 sells)、或者写进 sells 说明为什么卖。"
            "计划持有天数到了不等于必须卖——理由还在就继续拿。"
        )
    recs = context.get("recommendations") or []
    if recs:
        lines.append("研究台今日推荐(可选可不选)：" + "；".join(recs))
    methods = context.get("method_results") or []
    if methods:
        lines.append("你自己几个老套路的成本后样本外表现(负=不灵)：" + "；".join(methods))
    evidence = context.get("evidence_notes") or []
    if evidence:
        lines.append("本地历史检验已知的事实(供你参考,不是命令)：" + "；".join(evidence))
    cands = context.get("candidates") or []
    lines.append(f"可选候选(只能从这里选，共{len(cands)}只，附近期涨跌)：")
    lines.append("，".join(cands) if cands else "(今天没有可用候选)")
    lines.append(
        "请先想清楚你要做哪一类投资者，再挑你真正看得上的票(0-2 只)，"
        "说人话理由，标明做法(style)和打算拿多少天。没把握就少买或不买。"
    )
    return system, "\n".join(lines)


def propose_picks(
    context: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ask the configured LLM for its plan. None => caller uses rules.

    Pass ``diagnostics`` to learn *why* None came back; silent degradation to the
    rule engine is otherwise invisible, and the user cannot tell whether they are
    watching the AI think or a hardcoded ranking.
    """

    cfg = load_llm_config(diagnostics=diagnostics)
    if cfg is None:
        if diagnostics is not None and not diagnostics.get("reason"):
            diagnostics["reason"] = "not_enabled_or_unconfigured"
        return None
    system, user = _build_prompt(context)
    text = _call(system, user, cfg, diagnostics)
    if not text:
        return None
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        if diagnostics is not None:
            diagnostics["reason"] = "unparseable_json"
        return None
    buys_raw = parsed.get("buys")
    if not isinstance(buys_raw, list):
        buys_raw = []
    buys: list[dict[str, Any]] = []
    for row in buys_raw:
        if not isinstance(row, dict):
            continue
        symbol = re.sub(r"[^0-9]", "", str(row.get("symbol") or ""))
        if len(symbol) != 6:
            continue
        try:
            hold_days = int(row.get("hold_days") or 0)
        except (TypeError, ValueError):
            hold_days = 0
        # A declared horizon is honoured as-is, including a year. Clamped only to
        # a sane range so a garbled value cannot become a permanent position.
        hold_days = max(0, min(hold_days, 750))
        buys.append(
            {
                "symbol": symbol,
                "reason": str(row.get("reason") or "").strip(),
                "style": str(row.get("style") or "").strip(),
                "hold_days": hold_days,
                "conviction": str(row.get("conviction") or "").strip(),
            }
        )
    sells: list[dict[str, Any]] = []
    for row in parsed.get("sells") or []:
        if not isinstance(row, dict):
            continue
        symbol = re.sub(r"[^0-9]", "", str(row.get("symbol") or ""))
        if len(symbol) != 6:
            continue
        sells.append({"symbol": symbol, "reason": str(row.get("reason") or "").strip()})
    stance_raw = parsed.get("stance")
    stance: dict[str, Any] = {}
    if isinstance(stance_raw, dict):
        styles = stance_raw.get("preferred_styles")
        stance = {
            "summary": str(stance_raw.get("summary") or "").strip(),
            "preferred_styles": [
                str(s).strip() for s in styles if str(s).strip()
            ] if isinstance(styles, list) else [],
            "reasoning": str(stance_raw.get("reasoning") or "").strip(),
        }
    pack = load_prompt_pack()
    return {
        "model": cfg["model"],
        "buys": buys,
        "sells": sells,
        "stance": stance,
        "lesson": str(parsed.get("lesson") or "").strip(),
        "avoid": [str(x) for x in (parsed.get("avoid") or []) if x],
        "market_note": str(parsed.get("market_note") or "").strip(),
        "prompt_version": pack.prompt_version,
        "menu_version": pack.menu_version,
    }


__all__ = [
    "DEFAULT_MODEL",
    "EXTERNAL_ALLOW_ENV",
    "LOOPBACK_HOSTS",
    "load_llm_config",
    "llm_runtime_descriptor",
    "propose_picks",
]
