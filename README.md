# cs-brief

Rank a Customer Success book of business and explain, in plain English, why each account needs
attention this week. It is reachable three ways, all backed by the same core code:

- **Web dashboard:** `cs-brief ui` opens a local page at http://127.0.0.1:8000 with:
  - **My book:** the ranked list
  - **Account pages:** the score breakdown, signal history and next step
  - **Weekly brief:** a one-page summary with **Print or save as PDF** (fits on 2 A4 pages)
  - **Onboarding prep:** pick an account to get a call brief. It covers where the account is on
    the standard onboarding plan, an agenda, questions to ask and the signals to watch. You can
    print it (1 page) or copy it as text.
  - **Settings:**
    - Appearance: theme, accent colour, density, and which screen it opens on. These are saved
      to `config/settings.yaml`.
    - Scoring: sliders for every weight and threshold, with a live preview of how your top 10
      would reorder. These are saved to `config/scoring.yaml`.
- **Claude (MCP):** `cs-brief mcp` serves the book to Claude over stdio
- **Terminal:** `cs-brief score` prints the top of the book

It runs on bundled demo data (40 invented accounts) or reads your HubSpot portal. The HubSpot
connection is read-only; see [docs/hubspot.md](docs/hubspot.md) for setup.

## Quickstart (Windows, PowerShell)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
. .\scripts\dev.ps1        # keeps the virtualenv outside OneDrive
uv sync
uv run cs-brief score      # ranked top 10 in the terminal
uv run cs-brief ui         # dashboard at http://127.0.0.1:8000
```

### Use it from Claude Code

```powershell
claude mcp add cs-brief -- uv --directory "<path to this folder>" run cs-brief mcp
```

The server exposes five tools:

| Tool | What it returns |
|---|---|
| `list_book` | accounts in priority order; optional `motion`, `search`, `limit` |
| `get_account` | everything about one account |
| `explain_score` | why the account ranks where it does, with a short summary |
| `weekly_brief` | totals, counts by health and motion, call-outs, and the top accounts |
| `onboarding_brief` | a prep brief for an onboarding call: plan status, agenda, questions |

## How scoring works

Eight signals add risk points: usage trend, days since login, open severity-1 tickets, NPS,
champion left, seat utilisation, overdue billing and overdue onboarding milestones.

- **Risk:** each signal has a `start` value, where its risk begins, and a `full` value, where it
  adds its whole weight. Risk is the total of all signals, capped at 100.
- **Health:** risk of 45 or more is **At risk**, 20 or more is **Watch**, and anything lower is
  **Healthy**.
- **Priority:** risk scaled by ARR (log-scaled, 30% influence by default). This is what the book
  is sorted by.
- **Motion:** each account gets one motion, checked in this order:
  1. **Onboarding:** under 90 days old with milestones open.
  2. **Renewal:** renewing within 120 days.
  3. **Churn:** everything else.

All weights, thresholds and windows live in [`config/scoring.yaml`](config/scoring.yaml).

## Privacy and security

- The web UI binds to `127.0.0.1` only and answers only to `127.0.0.1` and `localhost`.
- Nothing is loaded from a CDN. htmx and the Atkinson Hyperlegible Next font are vendored; see
  [`VENDORED.md`](src/cs_brief/web/static/VENDORED.md).
- A strict Content-Security-Policy blocks inline scripts and third-party requests.
- Settings can only be changed from cs-brief's own pages. Writes that come from another website
  are refused.
- With demo data, nothing leaves your machine. With HubSpot, the only requests are read-only
  calls to `api.hubapi.com`. Your access token is stored in your user profile
  (`%LOCALAPPDATA%\cs-brief`), never in this folder, and the UI only ever shows its last 4
  characters.
- Web research, a later and optional phase, will be off by default.

## Development

```powershell
. .\scripts\dev.ps1
uv run pytest -q
uv run ruff check
uv run python scripts/gen_demo.py   # regenerate demo data (deterministic)
```

Design direction for the UI lives in [`docs/design.md`](docs/design.md).
