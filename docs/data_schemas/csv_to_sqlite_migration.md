# CSV To SQLite Migration

Migration is additive and reversible:

- CSV files are scanned dynamically.
- Forward, back, and raw adjusted daily bars migrate to separate tables.
- `partial_daily` migrates only to `intraday_partial_daily` with `is_complete=0`.
- Missing or malformed files produce warnings, not crashes.
- Dry-run mode writes no rows.
- Compare mode reports row count, date set, close/volume diff counts, hash ratio, and sample mismatches.

The row hash uses normalized fields in a fixed order and MD5 first 16 hex characters.
