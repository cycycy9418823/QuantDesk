from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from importlib import metadata


@dataclass(frozen=True)
class IntegrationInfo:
    name: str
    package: str
    installed: bool
    version: str
    purpose: str
    status: str
    license_note: str
    warning: str
    recommended_priority: str
    integration_type: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def check_optional_import(package_name: str) -> dict[str, object]:
    """Detect an optional package without importing heavy runtime modules."""

    spec = importlib.util.find_spec(package_name)
    installed = spec is not None
    version = ""
    if installed:
        try:
            version = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            version = "unknown"
    return {"package": package_name, "installed": installed, "version": version}


def _integration(
    *,
    name: str,
    package: str,
    purpose: str,
    priority: str,
    integration_type: str,
    license_note: str,
    warning: str,
) -> IntegrationInfo:
    detected = check_optional_import(package)
    installed = bool(detected["installed"])
    return IntegrationInfo(
        name=name,
        package=package,
        installed=installed,
        version=str(detected.get("version") or ""),
        purpose=purpose,
        status="available" if installed else "optional_not_installed",
        license_note=license_note,
        warning=warning,
        recommended_priority=priority,
        integration_type=integration_type,
    )


def list_integrations() -> list[IntegrationInfo]:
    """Return optional integration status for local packages only.

    The registry never installs packages, never downloads data, and never runs
    external strategy/training code. Missing tools are expected and safe.
    """

    return [
        _integration(
            name="AKShare",
            package="akshare",
            purpose="财经数据接口，适合继续完善 provider、fallback、data health、news health。",
            priority="high",
            integration_type="data_provider",
            license_note="Optional dependency; check upstream license and data source terms before redistribution.",
            warning="数据源稳定性、接口变动、网络失败都需要结合 data health / news health 检查。",
        ),
        _integration(
            name="vectorbt",
            package="vectorbt",
            purpose="向量化研究和参数扫描候选工具。",
            priority="medium",
            integration_type="research",
            license_note="Commons Clause caution; optional research dependency only.",
            warning="Optional research only; parameter scans can overfit and are not daily decision output.",
        ),
        _integration(
            name="Backtrader",
            package="backtrader",
            purpose="事件驱动回测架构参考或独立插件实验。",
            priority="low",
            integration_type="research",
            license_note="GPL-3.0 license caution; do not copy source into main project.",
            warning="Keep outside core QuantDesk; do not mix GPL source into the main project.",
        ),
        _integration(
            name="Microsoft Qlib",
            package="qlib",
            purpose="AI / 因子研究插件候选。",
            priority="low",
            integration_type="research",
            license_note="Optional heavy dependency; check license and data/model obligations before use.",
            warning="Heavy dependency; research plugin only, no model training in this RC.",
        ),
        _integration(
            name="FinRL / FinRL-X",
            package="finrl",
            purpose="强化学习研究实验候选。",
            priority="low",
            integration_type="research",
            license_note="Optional research dependency; check project and dependency licenses before experiments.",
            warning="Research only; not for daily trading decisions or beginner-facing signals.",
        ),
    ]
