# Reference: mpquant/Ashare

v2.8.8 uses Ashare as a design reference for small optional daily/minute adapters. QuantDesk does not vendor or copy `Ashare.py`.

## Fetch Status

- Local clone target: `external_refs/Ashare`
- Current machine status: `git` is not available in PATH, so the clone command could not run in this Codex session.
- Reference used for this summary: GitHub README page.

## Useful Ideas

The project presents a lightweight A-share price API centered on `get_price()`.

- It supports daily, weekly, monthly, and minute frequencies such as `1m`, `5m`, `15m`, `30m`, and `60m`.
- It describes Sina and Tencent as dual data cores with automatic fallback.
- It returns cleaned pandas DataFrame-style data.
- It accepts multiple symbol formats such as exchange-prefixed and joinquant-style codes.

## Fit For QuantDesk

Suitable adapter ideas:

- `ashare_daily`: optional provider for filling stale local daily files when the user explicitly runs a POST/CLI update.
- `ashare_intraday`: optional provider for 1-minute or short-interval cache fill before conditional-plan simulation.
- Symbol normalization can be reused as a design, but implemented independently in QuantDesk.

Reference-only for now:

- Any “live trading” framing in the upstream README is outside QuantDesk's boundary.
- QuantDesk will not use Ashare to place orders or connect brokers.

## Risks

- Ashare may not be installed as a package in this environment.
- The single-file module may require manual placement, so status should be `unavailable` instead of crashing.
- Sina/Tencent field formats can change.
- Minute data may be delayed, incomplete, or unavailable outside suitable sessions.
- If intraday data is missing, QuantDesk must keep `no_fill` and never use daily close as a fake fill.
