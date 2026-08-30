# v4.2.4 Data Update Policy

Default `network=false` never calls providers. Intraday sessions write only snapshots. Formal daily writes are only allowed post-close/after-hours after staging and validation. This workflow is research-only and not a trading system.
