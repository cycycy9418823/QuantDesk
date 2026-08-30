# Git Deletion Audit

v3.7.0.4 adds a read-only audit for dirty worktrees with tracked deletions. v3.7.0.5 refines the classification for root-level modules that were migrated to subpackages.

```powershell
python scripts/qd.py maintenance git-deletion-audit --json true --summary true
```

The audit reads `git status --short`, classifies tracked deletions and untracked files, and searches common references to deleted Python modules.

Root-level modules migrated to subpackages are deprecated legacy paths. When `quantdesk.__init__` compatibility aliases cover both `import quantdesk.old_name` and `from quantdesk import old_name`, the deleted root file is classified as `resolved_by_compat_shim`, not restore-required high risk. Documentation-only historical references are not runtime import risk.

Root-level modules deleted without a compatibility alias are classified as
`retired_legacy_deleted` when the scan finds no runtime import references. This
keeps old v5-era cleanup from being mistaken for a broken alias while still
flagging unmapped modules if live code imports them.

It never runs `git restore`, `git clean`, or automatic recovery/deletion. Recommendations are advisory only.
## v3.7.0.6 Note

The v3.7.0.5 deletion reconciliation remains valid: shim-covered deleted root paths are not restore candidates. v3.7.0.6 adds strategy report regression checks without restoring old root modules.
