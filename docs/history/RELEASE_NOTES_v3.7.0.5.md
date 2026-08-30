# QuantDesk v3.7.0.5 Git Deletion Reconciliation

v3.7.0.5 is a pure engineering hygiene release. It reconciles tracked deletions left after root-level modules were migrated into subpackages.

- Root-level legacy modules remain deleted; they are not restored or duplicated.
- Deprecated root import paths are covered by `quantdesk.__init__` compatibility aliases.
- New code should import from subpackages such as `quantdesk.paper`, `quantdesk.intraday`, `quantdesk.agent`, and `quantdesk.research`.
- Git deletion audit now distinguishes shim-covered legacy paths, broken aliases, runtime imports, docs-only references, and shim-definition references.
- Deleted root modules with no moved target and no runtime references are treated as retired legacy deletions, not restore-required failures.
- This release does not change strategy logic, low-price research results, backtest assumptions, broker boundaries, or network defaults.

Research and safety boundaries remain unchanged: no broker connection, no real orders, no automatic trading, no investment advice, and network off by default.
