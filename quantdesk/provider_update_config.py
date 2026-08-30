from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def provider_update_config() -> dict:
    providers = [
        {"provider_name": "local_csv", "enabled": True, "network_required": False, "timeout_seconds": 0, "max_retries": 0, "rate_limit_seconds": 0, "max_symbols_per_run": 0, "update_scope": "read_local", "fallback_behavior": "use_local", "failure_policy": "never_block", "writes_allowed": False, "target_data_dir": "data/daily", "protected_paths": ["canonical equity", "combo metrics"]},
        {"provider_name": "tencent", "enabled": False, "network_required": True, "timeout_seconds": 30, "max_retries": 1, "rate_limit_seconds": 1, "max_symbols_per_run": 200, "update_scope": "latest_daily_only", "fallback_behavior": "fallback_local_with_warning", "failure_policy": "record_error_continue", "writes_allowed": "new_date_only_after_user_enabled", "target_data_dir": "data/daily", "protected_paths": ["canonical equity", "combo metrics"]},
        {"provider_name": "akshare", "enabled": False, "network_required": True, "timeout_seconds": 30, "max_retries": 1, "rate_limit_seconds": 1, "max_symbols_per_run": 200, "update_scope": "latest_daily_only", "fallback_behavior": "fallback_local_with_warning", "failure_policy": "record_error_continue", "writes_allowed": "new_date_only_after_user_enabled", "target_data_dir": "data/daily", "protected_paths": ["canonical equity", "combo metrics"]},
        {"provider_name": "mootdx", "enabled": False, "network_required": True, "timeout_seconds": 30, "max_retries": 1, "rate_limit_seconds": 1, "max_symbols_per_run": 200, "update_scope": "latest_daily_only", "fallback_behavior": "fallback_local_with_warning", "failure_policy": "record_error_continue", "writes_allowed": "new_date_only_after_user_enabled", "target_data_dir": "data/daily", "protected_paths": ["canonical equity", "combo metrics"]},
        {"provider_name": "wrds_csmar", "enabled": False, "network_required": True, "timeout_seconds": 30, "max_retries": 0, "rate_limit_seconds": 0, "max_symbols_per_run": 0, "update_scope": "user_provided_csv_only", "fallback_behavior": "do_not_run_without_user_input", "failure_policy": "human_review_required", "writes_allowed": False, "target_data_dir": "data/metadata/formal_inbox", "protected_paths": ["data/daily", "canonical equity"]},
    ]
    return {"task_id": "v421_provider_update_config", "status": "completed", "providers": providers, "network_false_behavior": "network_disabled_using_local", **boundary_payload()}

