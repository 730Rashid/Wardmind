# Privacy

cs-brief is a local tool. It has no server of its own, no account, no analytics and no
telemetry. This page lists everything it stores, and every time anything leaves your computer.

## What leaves your computer

Only these, and only when you set them up:

| When | Where it goes | What is sent |
|---|---|---|
| Demo data | Nowhere | Nothing. The 40 demo accounts are invented and bundled with the app. |
| A HubSpot connection | `api.hubapi.com` | Read-only API calls with your access token, to read companies and tickets. Nothing is ever written to HubSpot. |
| Web research, **off by default** | The customer's own website, and nowhere else | An ordinary visit to that one website, when you press **Look up their website** on its account page. Nothing about the account is sent. |
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

## What is stored, and where

| What | Where |
|---|---|
| Appearance and other UI settings | `config/settings.yaml` in the folder you run cs-brief from |
| Scoring weights and thresholds | `config/scoring.yaml` in the same folder |
| HubSpot access token | `%LOCALAPPDATA%\cs-brief\hubspot-token`, or the `CS_BRIEF_HUBSPOT_TOKEN` environment variable. The dashboard shows only its last 4 characters. |
| Your connections, imported sheets, hand-added accounts, edits, readings, links, hidden accounts and notices | `%LOCALAPPDATA%\cs-brief\data` |
| Imports you started but didn't finish | `%LOCALAPPDATA%\cs-brief\data\drafts`, deleted after 24 hours or by **Clear local cache** |
| Web research results | `%LOCALAPPDATA%\cs-brief\data\research`, kept for 24 hours |

On macOS and Linux, `%LOCALAPPDATA%\cs-brief` is `~/.local/state/cs-brief` (or
`$XDG_STATE_HOME/cs-brief`).

HubSpot data is held in memory while the app runs and isn't written to disk.

## The dashboard's own safety

- It listens on `127.0.0.1` only, so other computers on your network can't reach it. It
  answers only to `127.0.0.1` and `localhost`.
- Changes can only come from cs-brief's own pages. A form posted from another website is
  refused.

## Removing everything

1. In **Settings > Connections**, delete the HubSpot token and remove any connections. Or
   simply delete the folder `%LOCALAPPDATA%\cs-brief`.
2. Delete `config/settings.yaml` to go back to the default appearance.
3. If you added the MCP server to Claude, remove it with `claude mcp remove cs-brief`.
