"""Recovery contracts for an interrupted AI evening cycle.

An interruption is not an invitation to re-decide.  If the frozen input exists
but the original model reply / tool result was never reliably persisted, the
cycle must stay *interrupted / missing output*: this module never calls a model,
never re-reads a new price, and never fabricates a judgment that "was predicted
back then".  Only a persisted raw response with a verifiable source and
timestamp may be reused, and even then it is reported as a reconstruction, not
as an originally published record.
"""
from __future__ import annotations

from typing import Any, Callable

MISSING_ORIGINAL_MODEL_OUTPUT = "missing_original_model_output"
REUSABLE_ORIGINAL_OUTPUT = "reusable_original_output"
NO_INPUT = "no_input"


def _verifiable_model_output(output: dict[str, Any] | None) -> bool:
    """Whether a persisted response has a verifiable source *and* timestamp."""

    if not isinstance(output, dict) or not output:
        return False
    source = str(output.get("source") or output.get("model") or "").strip()
    created = str(output.get("created_at") or output.get("persisted_at") or "").strip()
    return bool(source and created)


def plan_cycle_recovery(
    *,
    input_snapshot: dict[str, Any] | None,
    persisted_model_output: dict[str, Any] | None = None,
    model_caller: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Classify how an interrupted cycle may be recovered; never calls the model.

    ``model_caller`` is accepted only so callers/tests can prove it is *not*
    invoked.  The returned ``model_called`` is always ``False``.

    - no frozen input            -> ``no_input``
    - input but no verifiable persisted response -> ``missing_original_model_output``
    - verifiable persisted response -> ``reusable_original_output`` (reconstruction)
    """

    if not input_snapshot:
        return {"status": NO_INPUT, "model_called": False}
    if not _verifiable_model_output(persisted_model_output):
        # Keep the cycle interrupted.  Do NOT call model_caller, do not use a new
        # price, do not invent an "original" judgment.
        return {
            "status": MISSING_ORIGINAL_MODEL_OUTPUT,
            "model_called": False,
            "reason_code": "original_model_output_not_persisted",
        }
    return {
        "status": REUSABLE_ORIGINAL_OUTPUT,
        "model_called": False,
        "reconstruction": True,
        "output": dict(persisted_model_output or {}),
    }
