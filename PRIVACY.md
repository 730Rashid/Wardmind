# Privacy

cs-brief is a local tool. It has no server of its own, no account, no analytics and no
telemetry. This page lists everything it stores, and every time anything leaves your computer.

## What leaves your computer

Only these, and only when you set them up:

| When | Where it goes | What is sent |
|---|---|---|
| Demo data | Nowhere | Nothing. The 40 demo accounts are invented and bundled with the app. |
| A HubSpot connection | `api.hubapi.com` | Read-only API calls with your access token, to read companies and tickets. Nothing is ever written to HubSpot. |
| Web research, **off by default** | The customer's own website, and nowhere else | An ordinary visit to that website, when you press **Look up their website** on its account page or **Look up all websites**. Nothing about the account is sent. |
| AI summaries, **off by default**, with your own OpenRouter key | `openrouter.ai`, and the company running the model you chose | Only the text web research read from the customer's public website. Nothing from your book. |
| Data feeds you add (**Settings > Connections > Add a connection**) | Only the address or product you set up: your own file or SQLite database (nothing leaves), a link or API you typed, Google Sheets, Stripe, Chargebee, Zendesk, Freshdesk, PostHog or Airtable | Read requests with the key you gave that feed, on the schedule you chose. Nothing from your book is sent, and nothing is ever written to the source. |
| Claude, if you add the MCP server | Anthropic, as part of your Claude conversation | Whatever the tools return when Claude calls them: account names, ARR, owners, signals and briefs. This is covered by your own Claude account's terms. |

Spreadsheets you import and accounts you add by hand never leave your computer.

The web dashboard loads nothing from the internet. Its script (htmx) and font are bundled
(see [`VENDORED.md`](src/cs_brief/web/static/VENDORED.md)), and a strict Content-Security-Policy
stops the page from loading anything from anywhere else.

## Web research in detail

Web research is off until you turn it on in **Settings > Data & privacy**. When it is on:

- **What it does.** On an account page, **Look up their website** visits that account's own
  website (its Website field), the way your browser would. It reads three things: the home
  page title, its description, and the news or blog feed the site publishes. Headlines about
  funding, acquisitions, leadership changes or restructuring are tagged.
- **What is sent.** Only the visit itself. The site sees a request from your internet address,
  as it would from any visitor. No cookies are kept. Nothing about the account (ARR, owner,
  health, signals, notes) is sent, and nothing about your other accounts.
- **Who sees it.** Only that customer's website. No search engine or other company is involved.
- **When.** Only when you press the button, for that one account. Never in the background,
  and never for the whole book.
- **What is kept.** The result, on this computer, for 24 hours. **Clear local cache** removes
  it, and so does turning web research off.
- **Demo data.** Demo accounts are invented, so they show sample news and nothing is sent.
- **Safety.** A website field comes from imported data, so cs-brief treats it as untrusted.
  It visits only public `https` websites. It refuses IP addresses, `localhost` and
  private-network names. It checks the address every connection resolves to, redirects
  included, and refuses this computer and private networks. Proxy settings from the
  environment are ignored, so that check can't be bypassed. If you're on a network that
  requires a proxy, web research won't work there.

## AI summaries in detail

AI summaries are off until you turn them on in **Settings > Data & privacy** and add your own
OpenRouter API key. cs-brief has no AI account or key of its own.

- **What it does.** It turns what web research read from a customer's website into a short
  About and up to three highlights: acquisitions, funding, leadership changes or restructuring.
- **What is sent.** Only the website text: its address, title, description and recent
  headlines. Nothing from your book: not your name for the account, ARR, owner, health,
  signals, notes or other accounts.
- **Who sees it.** OpenRouter, and the company that runs the model you chose (by default
  `anthropic/claude-haiku-5.5`). Every request asks OpenRouter to use only providers that don't
  store or train on the data.
- **When.** Only when you press **Summarise with AI** on an account, or tick **Also summarise
  with AI** when looking up all websites. Never in the background.
- **Cost.** You pay OpenRouter directly. A summary is a few thousand tokens; each one is reused
  until the website text or the model changes, and the cost of every run is shown.
- **What is kept.** The summaries, on this computer. **Clear local cache** removes them, and
  so does turning AI summaries off.
- **Demo data.** Demo accounts get invented sample summaries, and nothing is sent.
- **Safety.** Website text is treated as untrusted. The model is told it is data, not
  instructions, and it has no tools. Its answer must match a fixed format, and it is shown as
  plain text labelled as written by AI.

## Data feeds in detail

A data feed keeps accounts or signals up to date from a source you choose. Nothing is set up
until you add one.

- **What is sent.** Read requests only (GET, plus PostHog's read-only query), to the address or
  product you set up, with that feed's key. Nothing from your book is sent.
- **Your key goes only where you sent it.** Keys travel in request headers, never in a web
  address, and are never shown again after you save them. If a server redirects to another
  site, or a next-page link points at one, the key is not sent there. Change a feed's address
  and you must type its key again.
- **Addresses.** `https` only. Plain `http` is allowed only for `localhost`, for a test server
  of your own. A feed may point at your company's private network on purpose (an internal
  export link), because you typed that address yourself.
- **Files and databases** are read where they are. A SQLite database is opened read-only, and
  only `SELECT` queries can run.
- **When.** On the schedule you picked for that feed (from every 15 minutes to once a day, when
  a file changes, or only when you press **Sync now**), while the dashboard is running.
- **Limits.** A response over 25 MB or a source with more than 50,000 rows is refused rather
  than cut short. Freshdesk's search returns at most 300 urgent tickets and Zendesk's at most
  1,000, so a very large ticket count can read low.
- **Removing a feed** deletes its saved readings and its key.

## What is stored, and where

| What | Where |
|---|---|
| Appearance and other UI settings | `config/settings.yaml` in the folder you run cs-brief from |
| Scoring weights and thresholds | `config/scoring.yaml` in the same folder |
| HubSpot access token | `%LOCALAPPDATA%\cs-brief\hubspot-token`, or the `CS_BRIEF_HUBSPOT_TOKEN` environment variable. The dashboard shows only its last 4 characters. |
| Your connections, imported sheets, hand-added accounts, edits, readings, links, hidden accounts and notices | `%LOCALAPPDATA%\cs-brief\data` |
| Imports you started but didn't finish | `%LOCALAPPDATA%\cs-brief\data\drafts`, deleted after 24 hours or by **Clear local cache** |
| Web research results | `%LOCALAPPDATA%\cs-brief\data\research`, kept for 24 hours |
| AI summaries | `%LOCALAPPDATA%\cs-brief\data\summaries` |
| Data feed keys | `%LOCALAPPDATA%\cs-brief\feeds`, one file per feed. Only the last 4 characters are ever shown. |
| Data feeds' latest data | `%LOCALAPPDATA%\cs-brief\data\sheets\feed-<n>.json` |
| Feed setups you started but didn't finish | `%LOCALAPPDATA%\cs-brief\data\feed-drafts`, deleted after 24 hours |
| OpenRouter API key | `%LOCALAPPDATA%\cs-brief\openrouter-key`, or the `CS_BRIEF_OPENROUTER_KEY` environment variable. Only its last 4 characters are ever shown. |

On macOS and Linux, `%LOCALAPPDATA%\cs-brief` is `~/.local/state/cs-brief` (or
`$XDG_STATE_HOME/cs-brief`).

HubSpot data is held in memory while the app runs and isn't written to disk.

## The dashboard's own safety

- It listens on `127.0.0.1` only, so other computers on your network can't reach it. It
  answers only to `127.0.0.1` and `localhost`.
- Changes can only come from cs-brief's own pages. A form posted from another website is
  refused.

## Removing everything

1. In **Settings > Connections**, delete the HubSpot token and remove any connections, and in
   **Settings > Data & privacy** remove the OpenRouter key. Or simply delete the folder
   `%LOCALAPPDATA%\cs-brief`.
2. Delete `config/settings.yaml` to go back to the default appearance.
3. If you added the MCP server to Claude, remove it with `claude mcp remove cs-brief`.
