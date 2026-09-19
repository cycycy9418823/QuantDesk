# Paper State Backup And Recovery

This tool supports Charter sections 2 and 7 and handbook H8. It copies paper
research evidence to an explicitly chosen directory outside the source checkout.
It does not install a task, choose a production backup location, notify anyone,
or apply a restore. No actual owner backup has been performed by implementation
tests.

Required artifacts are the strategy ledger, AI trader ledger, AI decision log
and owner learning log. Existing decision/journal companions are also copied.
Missing required files produce a failed result with a manifest of what was
available; the tool never creates a replacement ledger or learning log.

```powershell
python scripts/backup_paper_state.py --destination D:/QuantDeskBackup --dry-run
python scripts/backup_paper_state.py --destination D:/QuantDeskBackup --learning-log docs/learning_log.md
python scripts/backup_paper_state.py verify --manifest D:/QuantDeskBackup/backup_TIMESTAMP/manifest.json
python scripts/backup_paper_state.py restore-plan --manifest D:/QuantDeskBackup/backup_TIMESTAMP/manifest.json --target-root D:/QuantDeskRestoreReview
```

Paths above are examples, not a selected production destination. A backup
command writes only to its explicit destination. Dry-run, verification and
restore-plan do not write either the source or restore target. Every backup has
an exclusive timestamp directory; an existing backup is never overwritten.

The manifest records source paths, byte sizes and SHA256 values. All source
hashes are measured before copying and checked again afterward, and every copy
must match. A source change or copy failure marks the result failed. These are
byte-integrity checks, not proof of valid ledger calculations or a transactionally
consistent application checkpoint. Perform the owner-authorized production
backup in a confirmed quiet writer window.

Verification resolves file paths relative to the manifest, so a backup can be
moved to another disk. Empty manifests, missing required identities, duplicate
entries, path traversal and changed bytes fail verification. A failed backup
cannot produce a ready restore plan.

The owner must inspect the plan and perform a recovery rehearsal in a separate
empty directory before a real restore. The API only returns target paths and
hashes; it never restores files itself. The production backup location,
scheduled invocation and owner recovery rehearsal remain pending. Health-row
publication is not wired by this source-only task.
