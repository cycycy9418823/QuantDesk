from __future__ import annotations

__all__ = ["validate_agent_allowed_output"]


def __getattr__(name: str):
    if name == "validate_agent_allowed_output":
        from quantdesk.agent.agent_policy_guard import validate_agent_allowed_output

        return validate_agent_allowed_output
    raise AttributeError(name)
