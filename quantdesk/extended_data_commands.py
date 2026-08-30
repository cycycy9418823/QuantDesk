"""Shared operator commands for the extended-data workflow."""

from __future__ import annotations

FULL_AUDIT_COMMAND = "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0"
FULL_APPLY_COMMAND = "python scripts/qd.py import-extended-data --apply true --summary true --max-files-per-rule 0"
