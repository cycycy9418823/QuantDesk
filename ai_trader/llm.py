"""LLM brain for the independent AI paper trader (failure means hold only).

Rule research remains separate. When an authorised compatible endpoint is configured, this module lets the trader
*think*: it proposes which names to buy and why, in the trader's persona.

Hard contract:
- This module NEVER decides fills, costs, limits, or ledger math. It only
  returns candidate picks + reasons; overnight.build_plan still applies every
  lot/cash/turnover/limit/next-open constraint and the ledger stays truthful.
- ANY failure (no config, network error, timeout, bad JSON) returns None so the
  nightly run records the failure and creates no new trade intents. Previously
  declared plans can still settle and existing positions can still be valued.
- The token is read from env or the gitignored state/ai_trader/llm.env. It is
  never logged, never written to journals/results.
"""

from __future__ import annotations

import json
import http.client
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from ai_trader.paths import STATE_DIR, active_profile, profile_env_file
from ai_trader.prompt_pack import PromptPack, load_prompt_pack
from ai_trader.evaluation_contract import declared_verification
from ai_trader import llm_transport

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


def _env_file() -> Path:
    """Credentials file for the active book.

    The default book keeps using the module-level ``ENV_FILE`` so existing
    callers and tests can keep swapping that attribute; a named profile reads
    its own ``state/ai_trader_<profile>/llm.env``.
    """

    profile = active_profile()
    if profile and profile != "default":
        return profile_env_file()
    return ENV_FILE


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

    parsed = _parse_env_file(_env_file())
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

    parsed = _parse_env_file(_env_file())
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
    # 根类型必须在**任何**支持的包装下都成立：代码围栏不能把数组根变成内部对象。
    fenced_body = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced_body is not None and fenced_body.group(1).lstrip().startswith("["):
        return None
    stripped = text.strip()
    if stripped.startswith("["):
        # 根是数组：既不是我们的对象契约，也不能"抽里面那个对象"当成成功（T20 根结构）。
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


_TRANSPORT_STAGES = frozenset({"request", "response", "decode"})
_TRANSPORT_CATEGORIES = frozenset({
    "http_error",
    "timeout",
    "connection_error",
    "decode_error",
    "unknown",
})
_URLERROR_CAUSES = frozenset({
    "timeout",
    "connection_refused",
    "connection_reset",
    "dns",
    "ssl",
    "network_unreachable",
    "unknown",
})
_ERRNO_CAUSES = {
    60: "timeout",
    110: "timeout",
    10060: "timeout",
    111: "connection_refused",
    10061: "connection_refused",
    104: "connection_reset",
    10054: "connection_reset",
    101: "network_unreachable",
    10051: "network_unreachable",
    113: "network_unreachable",
    10065: "network_unreachable",
}


def _classify_transport_leaf(exc: BaseException) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return "connection_reset"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "ssl"
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and not isinstance(errno, bool):
        return _ERRNO_CAUSES.get(errno, "unknown")
    return "unknown"


def _classify_urlerror_cause(exc: BaseException) -> str:
    current: BaseException | None = exc
    for _ in range(4):
        if not isinstance(current, BaseException):
            return "unknown"
        if isinstance(current, urllib.error.HTTPError):
            return "unknown"
        if isinstance(current, urllib.error.URLError):
            reason = getattr(current, "reason", None)
            if isinstance(reason, BaseException) and reason is not current:
                current = reason
                continue
            return "unknown"
        return _classify_transport_leaf(current)
    return "unknown"


def _record_transport(
    diagnostics: dict[str, Any] | None,
    *,
    stage: str,
    started: float,
    error_category: str | None = None,
    http_status: int | None = None,
    urlerror_cause: str | None = None,
) -> None:
    if diagnostics is None:
        return
    row: dict[str, Any] = {
        "stage": stage,
        "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }
    if error_category is not None:
        row["error_category"] = error_category
    if http_status is not None:
        row["http_status"] = int(http_status)
    if urlerror_cause is not None:
        row["urlerror_cause"] = urlerror_cause
    diagnostics["transport"] = row


def persistable_transport_diagnostics(diagnostics: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whitelist copy safe to store in an evening result. No exception text."""

    if not isinstance(diagnostics, Mapping):
        return {}
    raw = diagnostics.get("transport")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    stage = raw.get("stage")
    if stage in _TRANSPORT_STAGES:
        out["stage"] = stage
    elapsed = raw.get("elapsed_ms")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
        out["elapsed_ms"] = elapsed
    category = raw.get("error_category")
    if category in _TRANSPORT_CATEGORIES:
        out["error_category"] = category
    status = raw.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        out["http_status"] = status
    cause = raw.get("urlerror_cause")
    if cause in _URLERROR_CAUSES:
        out["urlerror_cause"] = cause
    return out


def _record_model_response(
    diagnostics: dict[str, Any] | None,
    *,
    raw_text: str | None,
    content_type: str,
    response_model: Any = None,
    finish_reason: Any = None,
    stop_reason: Any = None,
    output_tokens: Any = None,
) -> None:
    """Capture answer text before stripping/parsing, never reasoning or headers."""

    if diagnostics is None:
        return
    evidence = diagnostics.get("response_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        diagnostics["response_evidence"] = evidence
    evidence.setdefault("source", "model_response")
    evidence.setdefault("call_state", "attempted")
    evidence.update(
        raw_text=raw_text,
        content_type=content_type,
        response_model=response_model if isinstance(response_model, str) else None,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        output_tokens=(
            output_tokens
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0
            else None
        ),
        observed_at=datetime.now().astimezone().isoformat(),
    )


def persistable_response_evidence(diagnostics: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy this invocation's evidence without changing successful replay input.

    An unavailable response is not an empty answer. Provider truncation is
    recorded only from an explicit finish/stop reason, never from broken JSON.
    """

    if not isinstance(diagnostics, Mapping):
        return {}
    raw = diagnostics.get("response_evidence")
    if not isinstance(raw, Mapping):
        return {}
    answer = raw.get("raw_text")
    answer = answer if isinstance(answer, str) else None
    out = {
        key: raw.get(key) if isinstance(raw.get(key), str) else None
        for key in (
            "source", "call_state", "observed_at", "requested_model", "response_model",
            "content_type", "finish_reason", "stop_reason", "parse_stage",
        )
    }
    tokens = raw.get("output_tokens")
    finish, stop = out["finish_reason"], out["stop_reason"]
    truncated = (
        True if finish == "length" or stop == "max_tokens"
        else False if finish == "stop" or stop in {"end_turn", "stop_sequence"}
        else None
    )
    out.update(
        raw_text=answer,
        text_char_count=len(answer) if answer is not None else None,
        text_status=("unavailable" if answer is None else "received" if answer.strip() else "empty"),
        output_tokens=tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None,
        provider_reported_truncation=truncated,
        error=diagnostics.get("reason") if isinstance(diagnostics.get("reason"), str) else None,
        transport=persistable_transport_diagnostics(diagnostics),
    )
    detail = diagnostics.get("transport_detail")
    if isinstance(detail, Mapping):
        # This is generated by our per-request tracer. The response body is
        # stored once in its request file, not copied into every plan/journal.
        out["transport_detail"] = dict(detail)
        capture_path = detail.get("capture_path")
        if isinstance(capture_path, str) and detail.get("capture_status") == "written":
            from hashlib import sha256
            try:
                path = Path(capture_path)
                if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("transport_capture_file_invalid")
                raw = path.read_bytes()
                out["transport_detail"].update(capture_sha256=sha256(raw).hexdigest(),
                    capture_binding_status="frozen_file_bytes")
            except (OSError, ValueError) as exc:
                out["transport_detail"].update(capture_binding_status="unverified",
                    capture_binding_reason=type(exc).__name__)
    # JSON may legally deliver escaped lone surrogate code points. Keep them
    # reversibly without letting new evidence break existing UTF-8 writers.
    for key, value in list(out.items()):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                out[key] = None
                out[key + "_json"] = json.dumps(value, ensure_ascii=True)
    out["text_storage"] = "json_escaped" if "raw_text_json" in out else "utf8" if answer is not None else None
    return out


def _cfg_int(cfg: Mapping[str, Any], key: str, default: int) -> int:
    """Read an int-valued tuning knob from a caller's cfg, else the default.

    Callers may pass ``max_tokens``/``timeout`` (the daytime trader caps a slot at
    2000 tokens / 120 s). Anything unparsable falls back to the module default so a
    bad knob degrades to the old behaviour instead of raising mid-cycle.
    """

    raw = cfg.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    diagnostics: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any] | None:
    """Shared transport: one POST, parsed JSON or None with a real reason.

    ``timeout`` lets a caller tighten the socket budget (the daytime trader uses
    120 s); omitting it keeps the historical :data:`DEFAULT_TIMEOUT_S`.
    """

    effective_timeout = DEFAULT_TIMEOUT_S if timeout is None else timeout

    def _note(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    started = time.perf_counter()
    status: int | None = None
    exchange = llm_transport.Exchange(url, body, headers, effective_timeout)
    if diagnostics is not None:
        diagnostics["transport_detail"] = exchange.detail
        evidence = diagnostics.setdefault("response_evidence", {})
        evidence.setdefault("source", "model_response")
        evidence.setdefault("call_state", "attempted")
        evidence.setdefault("requested_model", str(body.get("model") or ""))
        evidence.setdefault("raw_text", None)
    try:
        with llm_transport.open_response(
            request, effective_timeout, exchange, open_func=urllib.request.urlopen,
        ) as response:
            status = int(getattr(response, "status", 200))
            exchange.headers_received(response)
            if diagnostics is not None:
                diagnostics["http_status"] = status
            try:
                raw_body = exchange.read_body(response)
                with exchange.phase("utf8_decode"):
                    decoded = raw_body.decode("utf-8")
                with exchange.phase("outer_json"):
                    payload = json.loads(decoded)
            except (UnicodeError, ValueError) as exc:
                _note(f"transport:{type(exc).__name__}")
                _record_transport(
                    diagnostics,
                    stage="decode",
                    started=started,
                    error_category="decode_error",
                    http_status=status,
                )
                return None
            if not isinstance(payload, dict):
                _note("invalid_response_root:" + type(payload).__name__)
                exchange.detail["outer_json_root_type"] = type(payload).__name__
                _record_transport(diagnostics, stage="decode", started=started,
                                  error_category="decode_error", http_status=status)
                return None
            _record_transport(
                diagnostics,
                stage="response",
                started=started,
                http_status=status,
            )
            return payload
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exchange.headers_received(exc)
        if getattr(exc, "fp", None) is not None:
            try:
                exchange.read_body(exc)
            except (OSError, ValueError, http.client.HTTPException) as body_error:
                exchange.detail["error_body_read_error"] = type(body_error).__name__
            finally:
                try:
                    exc.close()
                except (OSError, ValueError, http.client.HTTPException) as close_error:
                    exchange.detail["error_body_close_error"] = type(close_error).__name__
        if diagnostics is not None:
            diagnostics["http_status"] = status
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics,
            stage="response",
            started=started,
            error_category="http_error",
            http_status=status,
        )
        return None
    except http.client.HTTPException as exc:
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics, stage="response" if status is not None else "request",
            started=started, error_category="connection_error", http_status=status,
        )
        return None
    except urllib.error.URLError as exc:
        cause = _classify_urlerror_cause(exc)
        category = "timeout" if cause == "timeout" else "connection_error"
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics,
            stage="response" if status is not None else "request",
            started=started,
            error_category=category,
            http_status=status,
            urlerror_cause=cause,
        )
        return None
    except (socket.timeout, TimeoutError) as exc:
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics,
            stage="response" if status is not None else "request",
            started=started,
            error_category="timeout",
            http_status=status,
            urlerror_cause="timeout",
        )
        return None
    except ValueError as exc:
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics,
            stage="decode",
            started=started,
            error_category="unknown",
        )
        return None
    except OSError as exc:
        cause = _classify_transport_leaf(exc)
        category = "timeout" if cause == "timeout" else (
            "connection_error" if cause != "unknown" else "unknown"
        )
        _note(f"transport:{type(exc).__name__}")
        _record_transport(
            diagnostics,
            stage="response" if status is not None else "request",
            started=started,
            error_category=category,
            http_status=status,
            urlerror_cause=cause if cause != "unknown" else None,
        )
        return None
    finally:
        exchange.finish()


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
    max_tokens = _cfg_int(cfg, "max_tokens", DEFAULT_MAX_TOKENS)
    timeout = _cfg_int(cfg, "timeout", DEFAULT_TIMEOUT_S)
    payload = _post_json(
        url,
        {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        {
            "authorization": f"Bearer {cfg['token']}",
            "content-type": "application/json",
            "accept": "application/json",
            # Some OpenAI-compatible relays sit behind Cloudflare, which rejects
            # the default Python-urllib user agent with a 403 page before the
            # request reaches the model.
            "user-agent": "QuantDesk-AI-Trader/1.0",
        },
        diagnostics,
        timeout,
    )
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    text = ""
    finish = ""
    raw_content: Any = None
    content_type = "missing"
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        raw_content = message.get("content")
        content_type = type(raw_content).__name__ if "content" in message else "missing"
        text = str(raw_content or "").strip()
        finish = str(first.get("finish_reason") or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    _record_model_response(
        diagnostics,
        raw_text=raw_content if isinstance(raw_content, str) else "" if raw_content is None else None,
        content_type=content_type,
        response_model=payload.get("model"),
        finish_reason=finish or None,
        output_tokens=usage.get("completion_tokens"),
    )
    if not text:
        _note(
            f"no_text_block:finish={finish or 'unknown'}"
            f":output_tokens={usage.get('completion_tokens')}"
            f":max_tokens={max_tokens}"
        )
        return None
    if diagnostics is not None:
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

    max_tokens = _cfg_int(cfg, "max_tokens", DEFAULT_MAX_TOKENS)
    payload = _post_json(
        f"{cfg['base_url']}/v1/messages",
        {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {
            "authorization": f"Bearer {cfg['token']}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": "QuantDesk-AI-Trader/1.0",
        },
        diagnostics,
        _cfg_int(cfg, "timeout", DEFAULT_TIMEOUT_S),
    )
    if not isinstance(payload, dict):
        return None
    blocks = payload.get("content")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not isinstance(blocks, list):
        _record_model_response(
            diagnostics, raw_text=None, content_type=type(blocks).__name__,
            response_model=payload.get("model"), stop_reason=payload.get("stop_reason"),
            output_tokens=usage.get("output_tokens"),
        )
        _note("no_content_blocks")
        return None
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    answer = "\n".join(part for part in texts if part)
    joined = answer.strip()
    _record_model_response(
        diagnostics, raw_text=answer, content_type="text_blocks",
        response_model=payload.get("model"), stop_reason=payload.get("stop_reason"),
        output_tokens=usage.get("output_tokens"),
    )
    if not joined:
        stop = str(payload.get("stop_reason") or "unknown")
        _note(
            f"no_text_block:stop={stop}:output_tokens={usage.get('output_tokens')}"
            f":max_tokens={max_tokens}"
        )
        return None
    if diagnostics is not None:
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


def _build_prompt(context: dict[str, Any], *, pack: PromptPack | None = None) -> tuple[str, str]:
    persona = str(context.get("persona_zh") or "一个自己拿主意的 A 股散户")
    pack = pack or load_prompt_pack()
    allowed = {slot.get("id") for slot in pack.menu.get("slots", []) if isinstance(slot, dict)}
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
    if recs and "recommendations" in allowed:
        lines.append("研究台今日推荐(可选可不选)：" + "；".join(recs))
    methods = context.get("method_results") or []
    if methods and "method_results" in allowed:
        lines.append("你自己几个老套路的成本后样本外表现(负=不灵)：" + "；".join(methods))
    evidence = context.get("evidence_notes") or []
    if evidence and "evidence_notes" in allowed:
        lines.append("本地历史检验已知的事实(供你参考,不是命令)：" + "；".join(evidence))
    cands = context.get("candidates") or []
    lines.append(f"可选候选(只能从这里选，共{len(cands)}只，附近期涨跌)：")
    lines.append("，".join(cands) if cands else "(今天没有可用候选)")
    facts = context.get("candidate_facts") or []
    if facts:
        lines.append("上述候选的周期与来源证据（研究代理或不完整区间仅作诊断，不可改述为今日原始行情）：")
        lines.append(json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False))
    lines.append(
        "请先想清楚你要做哪一类投资者，再挑你真正看得上的票(0-2 只)，"
        "说人话理由，标明做法(style)和打算拿多少天。没把握就少买或不买。"
    )
    return system, "\n".join(lines)


def propose_picks(
    context: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
    *,
    reuse_text: str | None = None,
    reuse_model: str = "",
    prompt_candidate: str | None = None,
    reuse_prompt_stamp: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ask the configured LLM for its plan. None => caller holds existing assets.

    Pass ``diagnostics`` to learn *why* None came back; silent degradation to the
    safe hold decision remains distinguishable from the model choosing to hold.

    ``reuse_text`` replays a *persisted original* response during recovery
    (A2): the model is never called again and the saved reply is re-parsed as it
    was.  No network, no new price, no re-decision.
    """

    if diagnostics is None:
        diagnostics = {}
    # A caller may reuse its diagnostics dict; old replies/status must not leak
    # into a new failed call. Only fields owned by this invocation are reset.
    for key in ("response_evidence", "prompt_stamp", "reason", "transport", "transport_detail", "http_status", "output_tokens", "reused_persisted_response"):
        diagnostics.pop(key, None)
    evidence: dict[str, Any] = {
        "source": "persisted_replay" if reuse_text is not None else "model_response",
        "call_state": "not_called",
        "parse_stage": "not_started",
    }
    diagnostics["response_evidence"] = evidence
    pack = load_prompt_pack(candidate=prompt_candidate)
    stamp = pack.version_stamp()
    if reuse_text is not None:
        text = reuse_text
        model = reuse_model
        _record_model_response(diagnostics, raw_text=text, content_type="str")
        evidence["requested_model"] = reuse_model
        # Recovery may not pretend that today's prompt files produced an old
        # persisted response. Unknown original stamp fields remain unknown.
        stamp = {key: str((reuse_prompt_stamp or {}).get(key) or "") for key in stamp}
        stamp["effective_prompt_sha256"] = str((reuse_prompt_stamp or {}).get("effective_prompt_sha256") or "")
        if diagnostics is not None:
            diagnostics["reused_persisted_response"] = True
    else:
        cfg = load_llm_config(diagnostics=diagnostics)
        if cfg is None:
            if diagnostics is not None and not diagnostics.get("reason"):
                diagnostics["reason"] = "not_enabled_or_unconfigured"
            evidence["parse_stage"] = "configuration"
            return None
        system, user = _build_prompt(context, pack=pack)
        stamp["effective_prompt_sha256"] = sha256(json.dumps(
            {"system": system, "user": user}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        diagnostics["prompt_stamp"] = dict(stamp)
        evidence["requested_model"] = str(cfg.get("model") or "")
        evidence["call_state"] = "attempted"
        text = _call(system, user, cfg, diagnostics)
        if "observed_at" not in evidence:
            # Transport failure has no answer. This also keeps injected/offline
            # adapters inspectable without fabricating provider finish metadata.
            _record_model_response(
                diagnostics, raw_text=text if isinstance(text, str) else None,
                content_type="str" if isinstance(text, str) else "unavailable",
            )
        model = str(cfg.get("model") or "")
    if not text:
        evidence["parse_stage"] = "transport" if str(diagnostics.get("reason") or "").startswith("transport:") else "text"
        return None
    evidence["parse_stage"] = "json"
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        if diagnostics is not None:
            diagnostics["reason"] = "unparseable_json"
        return None
    # T20：容器级类型错必须明确失败——把 `buys` 是对象、`sells` 是布尔、`avoid` 是数字这类
    # 回复悄悄变成"空计划"，等于替模型伪装成"主动空仓"，也掩盖了模型真的坏了这件事。
    # 单行垃圾（非对象行、非 6 位代码）仍按既有"逐行净化"处理，不在此列。
    evidence["parse_stage"] = "shape"
    for key, kind in (("buys", "array"), ("sells", "array"), ("avoid", "array")):
        value = parsed.get(key)
        if value is not None and not isinstance(value, list):
            if diagnostics is not None:
                diagnostics["reason"] = f"malformed_{key}_not_{kind}"
            return None
    if parsed.get("stance") is not None and not isinstance(parsed.get("stance"), dict):
        if diagnostics is not None:
            diagnostics["reason"] = "malformed_stance_not_object"
        return None
    buys_raw = parsed.get("buys") or []
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
                "verification": declared_verification(row.get("verification")),
            }
        )
    sells: list[dict[str, Any]] = []
    for row in parsed.get("sells") or []:
        if not isinstance(row, dict):
            continue
        symbol = re.sub(r"[^0-9]", "", str(row.get("symbol") or ""))
        if len(symbol) != 6:
            continue
        sells.append({"symbol": symbol, "reason": str(row.get("reason") or "").strip(),
                      "style": str(row.get("style") or "").strip(),
                      "verification": declared_verification(row.get("verification"))})
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
    evidence["parse_stage"] = "accepted"
    return {
        "model": model,
        "buys": buys,
        "sells": sells,
        "stance": stance,
        "lesson": str(parsed.get("lesson") or "").strip(),
        "avoid": [str(x) for x in (parsed.get("avoid") or []) if x],
        "market_note": str(parsed.get("market_note") or "").strip(),
        **stamp,
        # The unparsed model reply is persisted so an interrupted cycle can be
        # honestly reconstructed (or reported missing) instead of re-decided.
        "raw_response": text,
        "reused_persisted_response": reuse_text is not None,
    }


__all__ = [
    "DEFAULT_MODEL",
    "EXTERNAL_ALLOW_ENV",
    "LOOPBACK_HOSTS",
    "load_llm_config",
    "llm_runtime_descriptor",
    "persistable_transport_diagnostics",
    "persistable_response_evidence",
    "propose_picks",
]
