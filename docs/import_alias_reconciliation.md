# Import Alias Reconciliation

QuantDesk v3.5.x and later moved several root-level modules into subpackages. The old root files are deprecated legacy paths. They are intentionally not restored.

Compatibility is provided by `quantdesk.__init__` through `_MOVED_MODULES`, a meta-path alias finder, `sys.modules` registration, and `__getattr__` for `from quantdesk import old_name` compatibility.

Some deleted root modules are intentionally retired rather than moved. When a
deleted module has no `_MOVED_MODULES` target and no runtime import references,
the audit reports it as `retired_deleted`, not as a broken alias. A broken alias
is limited to a mapped legacy path whose destination import or compatibility
alias fails, or an unmapped legacy path that still has runtime imports.

New code should import from the destination packages directly:

- `quantdesk.paper.*`
- `quantdesk.intraday.*`
- `quantdesk.agent.*`
- `quantdesk.research.*`

Use:

```bash
python scripts/qd.py maintenance import-alias-reconciliation --json true --summary true
python scripts/qd.py maintenance import-alias-reconciliation --scan-only true --json true --summary true
```

The reconciliation command is read-only by default. It does not run `git restore`, does not run `git clean`, and does not recreate deleted root modules.
## v3.7.0.6 Note

Import alias reconciliation is preserved. Strategy regression checks may inspect the import rewrite manifest, but they do not restore deprecated root modules or undo the compatibility shim.
