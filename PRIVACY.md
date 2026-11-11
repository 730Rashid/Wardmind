# Privacy

Wardmind is a local tool. It has no server of its own, no account, no analytics and no
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
| Your mailbox, **off until you connect it** (**Settings > Mailbox**) | Only your own mail provider's IMAP server (Gmail, Outlook, iCloud, Yahoo, Fastmail or the server you typed), over TLS | Your username and app password, to read Inbox and Sent. Wardmind never sends, moves, deletes or marks mail as read, and nothing from your book is sent. |
| Logos, with web research on | The customer's own website | An ordinary visit to its home page and the icon files it names, when you press **Find on website** or during **Look up all websites**. Nothing about the account is sent. |
| Claude, if you add the MCP server | Anthropic, as part of your Claude conversation | Whatever the tools return when Claude calls them: account names, ARR, owners, signals, briefs, and from your journal the last-contact dates, follow-ups, snoozes and outcomes (never the text of your notes). This is covered by your own Claude account's terms. |

Spreadsheets you import and accounts you add by hand never leave your computer.

The web dashboard loads nothing from the internet. Its script (htmx) and font are bundled
(see [`VENDORED.md`](src/wardmind/web/static/VENDORED.md)), and a strict Content-Security-Policy
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
- **Who sees it.** That customer's website. If its home page redirects to another public
  website (a new domain, for example), that site is visited instead. No search engine or other
  company is involved.
- **When.** When you press **Look up their website** for one account or **Look up all
  websites** for the whole book. If you added the MCP server, Claude can also look up an
  account's website when you ask it about that customer. Never on a schedule.
- **What is kept.** The result, on this computer, until you press **Clear local cache** or
  turn web research off. A result older than 24 hours is fetched again the next time you look
  it up. My book uses saved results for its news tags.
- **Demo data.** Demo accounts are invented, so they show sample news and nothing is sent.
- **Safety.** A website field comes from imported data, so Wardmind treats it as untrusted.
  It visits only public `https` websites. It refuses IP addresses, `localhost` and
  private-network names. It checks the address every connection resolves to, redirects
  included, and refuses this computer and private networks. Proxy settings from the
  environment are ignored, so that check can't be bypassed. If you're on a network that
  requires a proxy, web research won't work there. Each lookup has time and size limits, so a
  slow or hostile website can't stall Wardmind. Website text given to Claude is labelled as
  untrusted data that may contain instructions Claude must not follow.

## AI summaries in detail

AI summaries are off until you turn them on in **Settings > Data & privacy** and add your own
OpenRouter API key. Wardmind has no AI account or key of its own.

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
  export link), because you typed that address yourself. A public feed can't use a redirect to
  send Wardmind into your private network or this computer: a redirect to another site is only
  followed to a public `https` address.
- **Files and databases** are read where they are. A SQLite database is opened read-only, only
  `SELECT` queries can run, and a query is stopped after 30 seconds.
- **When.** On the schedule you picked for that feed (from every 15 minutes to once a day, when
  a file changes, or only when you press **Sync now**), while the dashboard is running.
- **Limits.** A response over 25 MB, more than 100 MB across all pages, a source with more
  than 50,000 rows or 500 columns, or a request still unfinished after 2 minutes is refused
  rather than cut short. Freshdesk's search returns at most 300 urgent tickets and Zendesk's at most
  1,000, so a very large ticket count can read low.
- **Removing a feed** deletes its saved readings and its key.

## Contacts and client updates

**Contacts** are the people at each customer that you add, or accept from a client update. They
are personal data, so:

- They stay on this computer, in `%LOCALAPPDATA%\wardmind\data\contacts.json`.
- Claude, through the MCP server, sees contact names, job titles, main-contact flags and who has
  left, but never email addresses.
- Removing a contact deletes it. Removing a connection or a hand-added account deletes its
  accounts' contacts.

**Client updates** are off for every client until you turn them on for that client, after
agreeing it with them. Wardmind records who agreed and when.

- **How it works.** You make one form in your own form tool (Microsoft Forms, Airtable or Google
  Forms). Each client who agreed gets their own link to it, with a client code filled in.
  Wardmind reads the form's responses through a data feed. The client never connects to
  Wardmind or to your CRM.
- **Nothing changes until you accept it.** Every response waits on the Client updates page
  until you accept it (after correcting it if needed) or reject it.
- **What is kept.** A response with no code, a code that matches no client who agreed, or an
  account no longer in your book is ignored, and nothing from it is kept. Once you accept or
  reject an update, the client's text is deleted, and only a record that it was handled stays.
- **Turning updates off** for a client deletes their agreement and any updates still waiting.
  **New link** makes the old link stop working.
- **Google Forms.** Its responses sheet has to be shared as "Anyone with the link" for
  Wardmind to read it, so treat that link like a password. Microsoft Forms, with responses in
  an Excel workbook in your OneDrive, or an Airtable form avoids this.

## Notes, follow-ups and score history

**Your journal** holds the notes, calls, meetings and emails you log, your follow-ups, snoozes and
renewal outcomes. It is your own record of your customers, so:

- It stays on this computer, in `%LOCALAPPDATA%\wardmind\data\journal.json`. Notes you type
  while trying the demo data go in a separate file, `demo-journal.json`, and never mix with
  your own accounts.
- Claude, through the MCP server, sees when you last contacted an account, how many notes you
  keep, your open follow-ups, any snooze and recorded outcomes, but never the text of a note.
- A renewal outcome also keeps what Wardmind thought of the account when you recorded it (its
  risk, health and signal values), so the scoring can later learn from real outcomes. Nothing
  learns from it yet, and it never leaves your computer.
- Removing a note, follow-up or outcome deletes it. Removing a connection or deleting a
  hand-added account deletes its accounts' journal.

**Emails from your mailbox** (your data only), once you connect it in **Settings > Mailbox**:

- Every 15 minutes Wardmind reads the newest mail in Inbox and Sent, read-only. The first check
  goes back 30 days.
- Only emails to or from one of an account's contacts, or an address at its website's domain,
  are kept. Each one is kept as its subject, date, the addresses involved and a short excerpt
  (quoted replies cut off) in that account's Activity history. Everything else is read and
  forgotten. Free mail domains such as gmail.com never match a whole account.
- The app password is saved like a feed key and only ever shown as dots. **Disconnect** deletes
  it; emails already logged stay until you remove them.

**Logos** are images you upload, or that **Find on website** fetched from the customer's own
site. They are kept until you remove them, or until **Delete all local data**; clearing the cache
or turning web research off doesn't remove them. Removing a connection or deleting an account
removes its logo.

**Score history** (your data only): one snapshot a day of every account's name, rank, risk,
health, ARR and signal values, kept for two years in `%LOCALAPPDATA%\wardmind\data\history`,
one file per day. These are the same facts My book shows, kept so Wardmind can say what changed
since last week and show each account's trend. Removing a connection or deleting an account
removes it from every day's file. With demo data, nothing is recorded: the demo's history is
worked out from its sample readings.

## What is stored, and where

| What | Where |
|---|---|
| Appearance and other UI settings | `config/settings.yaml` in the folder you run Wardmind from. The desktop app (Wardmind.exe) uses `%LOCALAPPDATA%\wardmind\config\settings.yaml` instead. |
| Scoring weights and thresholds | `config/scoring.yaml` in the same folder (desktop app: `%LOCALAPPDATA%\wardmind\config\scoring.yaml`) |
| HubSpot access token | `%LOCALAPPDATA%\wardmind\hubspot-token`, or the `WARDMIND_HUBSPOT_TOKEN` environment variable. The dashboard shows only its last 4 characters. |
| Your connections, imported sheets, hand-added accounts, edits, readings, links, hidden accounts and notices | `%LOCALAPPDATA%\wardmind\data` |
| Imports you started but didn't finish | `%LOCALAPPDATA%\wardmind\data\drafts`, deleted after 24 hours or by **Clear local cache** |
| Web research results | `%LOCALAPPDATA%\wardmind\data\research`, until **Clear local cache** or turning web research off |
| AI summaries | `%LOCALAPPDATA%\wardmind\data\summaries` |
| Contacts | `%LOCALAPPDATA%\wardmind\data\contacts.json` |
| Notes, activity (calls, meetings, logged emails), pins, account teams, follow-ups, snoozes and renewal outcomes | `%LOCALAPPDATA%\wardmind\data\journal.json` (demo data: `demo-journal.json`) |
| Mailbox settings: provider, server, username, your other addresses, last check | `%LOCALAPPDATA%\wardmind\data\mailbox.json` |
| Mailbox app password | `%LOCALAPPDATA%\wardmind\feeds\mailbox-password` |
| Logos | `%LOCALAPPDATA%\wardmind\data\logos` |
| Score history, one snapshot a day for two years | `%LOCALAPPDATA%\wardmind\data\history` |
| Client update agreements, codes, your form link and updates waiting for review | `%LOCALAPPDATA%\wardmind\data\client_updates.json` |
| Data feed keys | `%LOCALAPPDATA%\wardmind\feeds`, one file per feed. Only the last 4 characters are ever shown. |
| Data feeds' latest data | `%LOCALAPPDATA%\wardmind\data\sheets\feed-<n>.json` |
| Feed setups you started but didn't finish, with any key you typed for them | `%LOCALAPPDATA%\wardmind\data\feed-drafts`, deleted after 24 hours or by **Clear local cache** |
| OpenRouter API key | `%LOCALAPPDATA%\wardmind\openrouter-key`, or the `WARDMIND_OPENROUTER_KEY` environment variable. Only its last 4 characters are ever shown. |

On macOS and Linux, `%LOCALAPPDATA%\wardmind` is `~/.local/state/wardmind` (or
`$XDG_STATE_HOME/wardmind`).

HubSpot data is held in memory while the app runs and isn't saved as a copy. Your own edits to
HubSpot accounts are saved, together with the value HubSpot had when you made each edit, and a
notice may quote a value HubSpot changed.

On macOS and Linux, the data files are readable only by you. On Windows they sit in your own
user profile, which other users of the computer can't read.

Some links work like passwords: a Google Sheet shared as "Anyone with the link", a signed
export link, or your client update form's link. They are saved as connection settings in the
same folder, not with the keys, so treat that folder as private.

## The dashboard's own safety

- It listens on `127.0.0.1` only, so other computers on your network can't reach it. It
  answers only to `127.0.0.1` and `localhost`, which stops websites from reaching it by
  pointing their own names at your computer.
- Changes can only come from Wardmind's own pages. A form posted from another website is
  refused.
- Other websites can't load its pages, images or scripts, or put it in a frame, so they can't
  read your book or tell that Wardmind is running.
- Pages aren't kept in your browser's cache. Logos are the one exception: the browser may keep
  them for an hour, so lists don't fetch each one again.
- The desktop app (Wardmind.exe) runs the same dashboard on `127.0.0.1`, on a port chosen
  fresh each time it starts, with all of the protections above. Its window's browser runs in
  private mode, so it keeps no cookies, cache or website storage after you close it. Links to
  other websites open in your normal browser, not in the Wardmind window.

## Removing everything

1. **Settings > Data & privacy > Delete all local data** deletes everything Wardmind keeps on
   this computer: connections, accounts, edits, contacts, client updates, notes, follow-ups,
   outcomes, score history, saved lookups and
   summaries, unfinished imports, and every saved token and key. If you set
   `WARDMIND_HUBSPOT_TOKEN` or `WARDMIND_OPENROUTER_KEY` as environment variables, remove those
   yourself. Or simply delete the folder `%LOCALAPPDATA%\wardmind`.
2. Delete `config/settings.yaml` to go back to the default appearance (the desktop app's is
   in `%LOCALAPPDATA%\wardmind\config`). To remove the desktop app itself, delete its
   `Wardmind` folder.
3. If you added the MCP server to Claude, remove it with `claude mcp remove wardmind`.
