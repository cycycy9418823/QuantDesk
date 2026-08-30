# QuantDesk v3.7.0.6

`v3.7.0.6-strategy-regression-guard-and-report-hygiene` is a small engineering hygiene release for strategy report integrity.

- Adds strategy regression audit diagnostics.
- Adds report provenance and `write_latest` / smoke-run separation for strategy reports.
- Adds baseline distinctness, top-level metric consistency, and performance sanity guards.
- Keeps v3.7.0.5 import alias reconciliation intact; deprecated root import paths remain supported.
- Research only: no broker connection, no real orders, no automatic trading, and no investment advice.

