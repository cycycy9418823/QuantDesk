# Reference: Ogannesson/ashare-llm-analyst

v2.8.8 treats this project as a report-workflow reference. QuantDesk does not copy its code or its LLM prompt/report logic.

## Fetch Status

- Local clone target: `external_refs/ashare-llm-analyst`
- Current machine status: `git` is not available in PATH, so the clone command could not run in this Codex session.
- Reference used for this summary: GitHub README page.
- License observed from repository page: MIT. Any future code reuse still needs file-level verification and attribution.

## Useful Ideas

The project combines:

- Ashare historical data retrieval.
- MyTT-style technical indicators such as MA, MACD, KDJ, RSI, and BOLL.
- Matplotlib charts.
- An HTML report output.
- Optional LLM-generated analysis text.

## Fit For QuantDesk

Suitable design lessons:

- Keep the data acquisition, indicator calculation, and report rendering stages separate.
- HTML reports are useful for local review and browser printing.
- Optional LLM or external API credentials must never become required for the main local workflow.

Reference-only for now:

- LLM-generated “investment advice” wording is not compatible with QuantDesk's boundary.
- API-key storage and prompt execution are outside this version.

## Risks

- Requires external LLM credentials for its AI component.
- Report language can drift into advice, which QuantDesk explicitly avoids.
- It uses Ashare as a dependency, so daily/minute availability inherits Ashare's provider risks.
