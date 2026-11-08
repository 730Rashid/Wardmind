# cs-brief

Rank a Customer Success book of business and explain, in plain English, why each account needs
attention this week. It is reachable three ways, all backed by the same core code:

![cs-brief: the ranked book, an account page, the weekly brief and onboarding prep](docs/demo.gif)

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
    - Connections: demo data or your data, HubSpot, spreadsheets and accounts added by hand.
    - Data & privacy: web research and AI summaries on or off, your OpenRouter key, and
      clearing the local cache.
- **Claude (MCP):** `cs-brief mcp` serves the book to Claude over stdio
- **Terminal:** `cs-brief score` prints the top of the book

It runs on bundled demo data (40 invented accounts), or on **your data**, which can mix these
connections:
- **HubSpot:** read-only; see [docs/hubspot.md](docs/hubspot.md) for setup
- **Spreadsheets:**
  - import a CSV or Excel file, or paste cells copied from Excel or Google Sheets
  - columns are matched for you, and only name, ARR and renewal date are required
  - import again whenever the sheet changes, and the signal history builds up
- **Accounts added by hand**

You can edit any account. An edit to a value that came from a connection lasts until that
connection changes the value; then the new value wins and the account page tells you. When the
same customer turns up in two connections, cs-brief suggests linking them, and you confirm.
Everything you add or change is stored in your user profile (`%LOCALAPPDATA%\cs-brief\data`),
not in this folder.

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

The server exposes six tools:

| Tool | What it returns |
|---|---|
| `list_book` | accounts in priority order; optional `motion`, `search`, `limit` |
| `get_account` | everything about one account |
| `explain_score` | why the account ranks where it does, with a short summary |
| `weekly_brief` | totals, counts by health and motion, call-outs, and the top accounts |
| `onboarding_brief` | a prep brief for an onboarding call: plan status, agenda, questions |
| `research_account` | what the customer's own website says: title, description, recent news (only when web research is on) |

## Data feeds: keep your data up to date by itself

**Settings > Connections > Add a connection** opens a catalog of sources. Each one either adds
accounts (an automatic spreadsheet import) or adds signals to accounts you already have,
matched by ID, website or name:

| Source | What it brings in |
|---|---|
| File on this computer, Link to a file, Google Sheets | Any CSV, TSV, Excel or JSON export, re-read when it changes or on a schedule |
| Any JSON API | Records from an API, following its next-page links |
| SQLite database | The result of your own read-only `SELECT` query |
| Stripe, Chargebee | Days each customer's oldest unpaid invoice is overdue |
| Zendesk, Freshdesk | Open urgent tickets per customer |
| PostHog | Usage trend and days since last login per company, or your own HogQL query |
| Airtable | Any table, as accounts or as signals |

You match the source's columns once, see what will happen before saving, and then the feed syncs
on its schedule. Settings > Connections shows when each feed last synced, any problem, and rows
that didn't match an account. Feeds only read, and each feed's key is sent only to its own
source ([PRIVACY.md](PRIVACY.md#data-feeds-in-detail)).

## Web research (optional, off by default)

Turn it on in **Settings > Data & privacy**. Each account page then gets a **Look up their
website** button. It visits that customer's own website (the account's Website field) and shows:
- the site's title and description
- the latest posts from the news or blog feed it publishes, tagged when they mention funding,
  an acquisition, a leadership change or restructuring

No search engine is involved, and nothing about the account is sent. Results are kept on your
computer for 24 hours. With demo data, you see invented sample news and nothing is sent.

**Look up all websites** does the whole book at once (8 sites at a time, with a progress bar),
and My book then shows each account's news topics next to its name.

**AI summaries** (also optional and off by default) use your own
[OpenRouter](https://openrouter.ai) key to turn each website into a short About and up to three
highlights. The default model is `anthropic/claude-haiku-5.5`, and you can pick any OpenRouter
model. Only the website text is sent, never anything from your book, and each summary is reused
until the website changes, so a book of 40 accounts typically costs a cent or two.

[PRIVACY.md](PRIVACY.md) has the details.

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
- Web research is off by default. When on, it visits only the customer's own public website,
  only when you ask, and never an address on your computer or private network.
- No analytics or telemetry. [PRIVACY.md](PRIVACY.md) lists everything that is stored and
  everything that leaves your computer.

## Development

```powershell
. .\scripts\dev.ps1
uv run pytest -q
uv run ruff check
uv run python scripts/gen_demo.py   # regenerate demo data (deterministic)
```

Design direction for the UI lives in [`docs/design.md`](docs/design.md).
