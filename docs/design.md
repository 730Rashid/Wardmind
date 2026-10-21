# cs-brief design direction

Implementers: this is the source of truth for every screen. If something here conflicts with a
brief, escalate rather than guess.

## Subject, audience, job
- **Subject:** a Customer Success Manager's own book of business, around 40–150 accounts.
- **Audience:** one CSM on a laptop, usually on Monday morning or before a call.
- **Primary job:** answer "who needs me this week, and why" in under ten seconds, then get to the
  account.

The interface is a **call sheet**, not a BI dashboard. It reads as a ranked working list with
plain-English reasons. The words a CSM would say on a call ("Active users down 38% in 30 days")
are the most important text on the page, so they get the most typographic care.

## Principles
1. **The reason is the hero.** In every row the top reason sits in full ink at body size. It is
   never truncated to a chip or a tooltip. Everything else in the row is quieter than it.
2. **Colour means health, and only health.** Red, amber and green appear only for at risk, watch
   and healthy, and always alongside a shape and a word. The plum accent is for interaction
   (links, focus, the selected filter). Motion tags and priority bars are neutral.
3. **Say it in a sentence.** Summaries are sentences, not stat tiles. For example: "9 accounts are
   at risk, carrying $1.4M of ARR."
4. **Honest state.** The UI always shows where the data comes from ("Showing demo data"), and
   empty or error states say what to do next.
5. **One moment of motion.** On first page load the priority bars fill once (300 ms). Nothing
   else animates unless the user caused it, and `prefers-reduced-motion` turns this off.

## Colour tokens
All colours are defined on `:root` in `web/static/css/app.css`. Contrast is verified by
`tests/test_contrast.py`: at least 4.5:1 for text and at least 3:1 for UI strokes.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#F7F8FA` | `#171A21` | page background |
| `--surface` | `#FFFFFF` | `#1F232C` | table, inputs |
| `--ink` | `#1C2430` | `#E6E9EE` | primary text, the reason sentence |
| `--ink-muted` | `#566173` | `#A0A9B6` | owner, counts, secondary text |
| `--line` | `#D6DBE3` | `#353B47` | decorative rules and bar tracks only (not a 3:1 boundary) |
| `--control` | `#7A8494` | `#7D8696` | input, button and filter-segment borders (≥ 3:1) |
| `--accent` | `#5A3E9B` | `#BBA6F2` | links, focus ring, selected filter (plum ink) |
| `--risk` | `#B42318` | `#FF8A80` | at risk |
| `--watch` | `#9A4A05` | `#F7B955` | watch |
| `--healthy` | `#067647` | `#5FD39A` | healthy |
| `--bar` | `#7D8696` | `#6F7888` | priority bar fill (neutral, ≥ 3:1 on paper and surface) |

Theme selection works like this:
- With no user choice, `prefers-color-scheme` decides.
- `<html data-theme="light|dark">` overrides it (Settings comes in phase 5).
- Dark rules therefore live under `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {…} }` and again under `:root[data-theme="dark"]`.

## Type
- **Atkinson Hyperlegible Next** (variable, 200–800, SIL OFL) is the only family. It was designed
  for low-vision legibility, which suits a tool people scan quickly. Its distinct letterforms also
  give the product its voice without a display face.
- Vendored at `web/static/fonts/`, using `font-display: swap`. The fallback is `system-ui, sans-serif`.
- Numbers use `font-variant-numeric: tabular-nums` where supported, and numeric columns are right-aligned.

The scale is a minor third (×1.2) from a 16px base:

| Step | Size / line-height | Weight | Use |
|---|---|---|---|
| `--t-sm` | 14px / 1.45 | 400 | owner name, counts, helper text (never smaller) |
| `--t-body` | 16px / 1.5 | 400 | body, table cells, the reason sentence |
| `--t-md` | 19px / 1.4 | 600 | account name in detail view, section headings |
| `--t-lg` | 23px / 1.3 | 600 | page titles |
| `--t-xl` | 28px / 1.25 | 500 | the summary sentence at the top of My Book |

Typography rules:
- Sentence case everywhere, with **no all-caps labels**.
- No single word in a sentence gets a different colour or weight.
- Prose is capped at 70ch.

## Layout
- The content column is left-aligned, `max-width: 1200px`, with 32px side padding (16px on mobile).
- **Spacing scale:** 4, 8, 12, 16, 24, 32, 48.
- **Radius follows hierarchy:**
  - 0 on table rows and the page
  - 4px on inputs, buttons and filter segments
  - no pill shapes
- **Shadows:** none, except the focus ring and popovers.
- **Focus ring:** 2px `--accent` outline, offset 2px, on every focusable element.

### Top bar (every page)
```
cs-brief   My book   Weekly brief   Onboarding prep   Settings              Showing demo data
───────────────────────────────────────────────────────────────────────────────────────────
```
- The product name is in `--t-body` weight 700, in ink (not accent).
- The current page's nav link gets a 2px accent underline and `aria-current="page"`.
- The data-source note on the right is `--t-sm`, muted.
- A skip link ("Skip to content") is the first focusable element.

### My book
```
Tuesday 21 October                                                   (t-sm, muted)

9 accounts are at risk, carrying $1.4M of ARR.                       (t-xl, ink)
14 more are worth watching this week.                                (t-xl, ink-muted)

[ All 40 | Churn 20 | Renewal 12 | Onboarding 8 ]          [ Search accounts         ]
Showing 40 accounts                                          (t-sm, aria-live="polite")

  #  Health        Priority     Account                Motion       ARR      Why it's on your list
 ─────────────────────────────────────────────────────────────────────────────────────────────────
  1  ▲ At risk     82 ▬▬▬▬▬▬▬░  Larkspur Freight       Renewal  $184,000      Active users down 38% in 30 days
                                Dana Whitfield                               and 2 other signals
 ─────────────────────────────────────────────────────────────────────────────────────────────────
  2  ◐ Watch       61 ▬▬▬▬▬░░░  Quillon Health         Churn     $96,500      Champion left the company
```
- **Filter:** a segmented control: one bordered group of 4px-radius buttons, a single row,
  with counts. The selected one gets an accent border and accent text plus `aria-pressed="true"`.
  Under the hood it is a `<form method="get">` so it works without JavaScript.
- **Search:** a labelled input. The visible label "Search accounts" acts as the placeholder-free
  label above it on mobile and visually hidden on desktop.
- **Table:** a real `<table>` with a `<caption>` (visually hidden) and `scope="col"` headers.
  Rows are separated by 1px `--line` rules. There's no zebra striping. Hovering a row gives a
  `--paper` background.
- **Account name** is the row's link (to `/accounts/{id}`) in ink weight 600. It gets an underline
  on hover or focus, and the owner sits under it in `--t-sm` muted.
- **Health cell:** an icon from the sprite plus its word, both in the health colour.
  - At risk: a filled triangle with "!"
  - Watch: a half-filled circle
  - Healthy: a circle with a check
- **Priority cell:** the number (tabular, right-aligned) plus a 56×6px bar. The track is `--line`
  and the fill is `--bar`, so it is neutral and not a health colour. The bar has
  `role="img" aria-label="Priority 82 of 100"`.
- **Motion cell:** plain text in ink with no colour or chip. The column header says what it is.
- **ARR:** right-aligned, formatted with no decimals (`$184,000`).
- **Why it's on your list:**
  - the top reason in `--t-body` ink, which makes it the widest column
  - below it, "and N other signals" in `--t-sm` muted when N > 0
- **Under 960px** (tablets and phones) each row becomes a stacked block, because seven columns
  can't leave the reason readable any narrower:
  ```
  1  ▲ At risk   Larkspur Freight
     Active users down 38% in 30 days
     Renewal   $184,000   Priority 82
  ```

### Account detail (`/accounts/{id}`)
This is call prep: the page a CSM opens just before speaking to the customer. It answers three
things in reading order: what is going on, what to do, and why the score says so. The nav keeps
"My book" active, because this page lives inside the book.

```
Back to My book                                                   (t-sm link)

Larkspur Freight                                                  (t-lg, h1)
▲ At risk     Renewal in 41 days     $184,000 ARR     Dana Whitfield     (facts row, t-sm)

At risk and up for renewal. Champion has left the company, and active users
down 38% in 30 days.                                              (t-md, weight 400, ink, ≤ 60ch)

┃ Next step                                                        (h2, t-md)
┃ Find the new decision-maker and meet them before the renewal conversation.
  (3px --accent rule on the left: the only accent-coloured block on the page)

Why it's ranked 3 of 40                                            (h2)
  Champion               Champion has left the company       ▬▬▬▬▬▬▬▬▬▬▬▬  25 of 25
  Usage trend            Active users down 38% in 30 days    ▬▬▬▬▬▬▬▬▬░░░  19 of 25
  Severity-1 tickets     1 open severity-1 ticket            ▬▬▬░░░░░░     7 of 20
  NPS                    Latest NPS response was 8 out of 10 ░░░░░░░       0 of 15   (muted row)
  …every signal is listed, so the CSM sees what is fine as well as what isn't
  Risk 82 of 100, which is At risk (45 or more). Priority 79 after weighting by ARR.

Signal history                                                     (h2)
  14 October    Usage trend     Active users down 38% in 30 days   Current
                Champion        Champion has left the company      Current
  30 September  Usage trend     Active users down 12% in 30 days
  …
```
- **Facts row:**
  - separate items spaced with `--space-6` gaps, with no middle dots
  - the health item uses the same icon and word as the My book table
  - "Renewal in 41 days" becomes "Renewal was 12 days ago" once the date has passed, and is
    left out when the motion is onboarding (that row shows "Day 34 of onboarding" instead)
- **Breakdown** is a `<table>`, with columns Signal, Reading and Points (the bar plus "19 of 25").
  - **Bars show weight and contribution together.** The track length is proportional to
    `max_points`: a 25-point signal gets the full 160px, a 10-point signal 64px. The fill shows
    `contribution`. The fill is `--bar` (neutral), because colour is reserved for health.
  - Rows are sorted by contribution, largest first. Rows with 0 contribution use `--ink-muted`
    text.
  - The risk sentence under the table is `--t-sm`.
- **Signal history** is a description list grouped by date (`<dl>` with `<dt>` dates).
  - Dates use `--t-sm` muted and tabular figures, like "14 October", with the year added only
    when it isn't this year.
  - Each reading sits on one line: label, sentence, and "Current" (`--t-sm` muted) on the reading
    the score uses.
  - Older readings that are no longer current use `--ink-muted` for the sentence.
  - A 1px `--line` rule runs down the left of the list, because this content really is a
    sequence.
- **Facts aside** (from 1100px it sits in a right-hand column, 1fr against a 2fr main; below
  that it follows the history):
  - a `<dl>` of Tier, Owner, Started (date plus "2 years ago"), Renews (date), Onboarding
    milestones ("4 of 6 done, 1 overdue"), and Data source
  - plain text, no card

### Weekly brief (`/brief`)
A one-page memo a CSM can bring to a 1:1 with their manager or save as a PDF. Its one visual
moment is the **book bar**: a single full-width bar showing how the book's ARR splits across
health. Everything else is sentences and a numbered list.

```
Weekly brief                                     [ Print or save as PDF ]   (button, --control border)
Week of 19 October 2026                                            (t-sm muted; Monday of as_of's week)

10 accounts are at risk, carrying $1.2M of ARR.                    (t-xl, same sentences as My book)
12 more are worth watching this week.

Where your ARR sits                                                (h2)
 ████████████████▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░    (24px tall, segments in health colours)
 ▲ At risk               ◐ Watch                ✓ Healthy
 $1.2M across 10 accounts   $2.1M across 12 accounts   $3.0M across 18 accounts

This week                                                          (h2)
 • 3 renewals due in the next 30 days
 • 2 champions have left
 (the `notable` call-outs, as a plain list; "Nothing unusual this week." when empty)

Accounts by motion                                                 (h2)
 Churn watch     20  ▬▬▬▬▬▬▬▬▬▬
 Renewal         12  ▬▬▬▬▬▬
 Onboarding       8  ▬▬▬▬      (neutral --bar fill, proportional to count; a small table)

Top 10 to contact                                                  (h2)
 1. Brindle Studio            ▲ At risk     Onboarding    $543,000
    3 open severity-1 tickets
    Next step: Ask support for a fix date on each open severity-1 ticket.
 2. …
 (an <ol>: the list is a ranked sequence, so the numbers carry meaning)

Made by cs-brief on 21 October 2026 from demo data.               (t-sm muted footer)
```
- **Book bar:**
  - `<svg role="img">` with `aria-label="ARR by health: $1.2M at risk, $2.1M watch, $3.0M healthy"`
  - segments are `<rect>`s whose `x` and `width` attributes come from the server, as
    percentages of `total_arr`; there are no inline styles, because of the CSP
  - a zero segment is omitted
  - the legend under the bar is a three-column grid aligned to the segments' order, each column
    showing the icon, word, ARR and count
- **Print / save as PDF:** a `<button type="button" data-print>`, which `app.js` wires to
  `window.print()`. It is hidden when JavaScript is off (`no-js` hides it with `.js`-gated CSS)
  and hidden in print.
- **Print stylesheet** (`@media print` in `app.css`):
  - `@page { size: A4; margin: 16mm; }`
  - force the light tokens
  - hide the top bar, skip link, button and nav
  - `print-color-adjust: exact` on the book bar and health icons
  - `break-inside: avoid` on each top-10 item and each section heading with its content
  - the footer always prints
  - **Target:** the whole brief fits on 2 A4 pages with the demo book.

### Onboarding prep (`/onboarding`, `/onboarding/{id}`)
This page helps a CSM prepare for an onboarding call. A brief is a working document they read
just before the call, can print, and can paste into their notes, so it's set like a document,
not a dashboard.

#### Picker (`/onboarding`)
```
Onboarding prep                                                    (h1)
8 accounts are in onboarding. 3 are behind on their plan.          (t-xl, the same sentence style as My book)

  Brindle Studio        ▲ At risk    Day 80 of 90    ●●○○○○  2 of 6 done, 1 overdue    Prepare brief
  Dunmere Legal         ▲ At risk    Day 41 of 90    ●●●○○   3 of 5 done, 1 overdue    Prepare brief
  …                                                  (onboarding accounts in book order)

Another account                                                    (h2, t-md)
  [ Choose an account ▾ ]  [ Open brief ]                          (GET form, works without JS)
```
- The list is a `<table>` (caption visually hidden) with columns Account, Health, Day, Plan and
  an action column.
- **Account** links to the brief. "Prepare brief" is a second link with an `aria-label` that names
  the account.
- **Plan dots:** one 8px circle per milestone.
  - Done: filled `--ink-muted`.
  - Overdue: a filled `--ink` square, so it reads by shape as well as tone.
  - Everything else: a hollow `--control` ring.
  - Each dot is `aria-hidden`; the words beside it carry the meaning.
- **Another account:** a `<select>` with two `<optgroup>`s, "In onboarding" and "Other accounts".
  The form is GET `/onboarding/open?account=…`, which redirects to `/onboarding/{id}`.
- **Empty book:** "No accounts are in onboarding right now." Keep the "Another account" form.

#### Brief (`/onboarding/{id}`)
```
Back to onboarding prep                                            (t-sm link)

Brindle Studio onboarding brief                                    (h1, t-lg)
Day 80 of 90     ▲ At risk     $543,000 ARR     Corin Vashti        (facts row, same as the account page)
[ Print or save as PDF ]   [ Copy as text ]                        (outlined buttons, js-only)

Behind on the plan with 11 days left. No one has logged in for 44 days.   (summary, t-md weight 400)

The plan                                                           (h2)
  1  Kickoff call              Done
  2  Admins set up             Done
  3  Data imported             Overdue
  4  Integrations connected    Next
  5  Pilot team trained        Upcoming
  6  Onboarding review         Upcoming
  Steps from the standard onboarding plan.                         (t-sm muted note)

Agenda for the call                                                (h2)
  1. Progress on the plan
     2 of 6 steps are done and Data imported is overdue. Agree a new date for it.
  2. Logins
     No one has logged in for 44 days. Find out who should be using it this week.
  …

Questions to ask                                                   (h2)
  • Who on your team should be logging in this week?
  • What is blocking Data imported?

Signals to watch                                                   (h2)
  (the account page's breakdown table, limited to the onboarding signals, with the same bars)

Next step                                                          (accent-ruled block, as on the account page)
  Book a check-in with the onboarding lead to review progress this week.
```
- **The plan** is an `<ol>` (a real sequence).
  - The status is a word with a small icon: check (Done), filled square (Overdue), arrow (Next),
    hollow circle (Upcoming).
  - **Next** is set in `--ink` weight 600 and **Overdue** in `--ink` weight 600. Done and Upcoming
    are `--ink-muted`.
  - No health colours here, because plan status isn't account health.
- **The agenda** is an `<ol>`: the topic in weight 600, with the detail on the next line.
- **The questions** are a `<ul>`.
- **Not in onboarding:** when `in_onboarding` is false, a `role="note"` line under the facts reads
  "{Name} isn't in onboarding right now. This brief shows its plan for reference." The rest of the
  brief still renders.
- **Copy as text** (`data-copy-target`, wired in `app.js`) copies a plain-text version of the
  brief: the title, the facts line, the summary, then the plan, agenda and questions as numbered
  and bulleted lines. The plain text sits in a hidden `<template>` rendered by the server, so the
  copied text matches the page. After copying, a `role="status"` line says "Copied." If the
  clipboard isn't available it says "Copy didn't work. Select the text and copy it instead."
- **Print:** fits on one A4 page for the demo accounts, and hides the buttons and the back link.

### Settings (`/settings/appearance`, `/settings/scoring`)
Settings is a quiet workshop page, with no decoration. `/settings` redirects to
`/settings/appearance`.
- **Sub-navigation:** a vertical list on the left from 960px (and a row of links above the content
  below that) with "Appearance" and "Scoring". The current one gets `aria-current="page"` and the
  same 2px accent underline as the top nav. Data & privacy and Connections arrive in later phases;
  don't show them yet.
- **Forms** are left-aligned in a single column, max 640px. Each group is a `<fieldset>` with a
  visible `<legend>` at `--t-md`.
- **Save changes** is the **only filled button** in the product: `--accent` background with
  `--on-accent` text, which is white in light mode and `#171A21` in dark mode. White text fails
  4.5:1 on the light dark-mode accents. Other buttons are outlined with `--control`. After a save the page redirects to itself
  with a `role="status"` message under the h1: "Appearance saved." or "Scoring saved. Every
  screen and Claude now use it."

#### Appearance
```
Settings
Appearance        Theme
Scoring             (•) Match my computer   ( ) Light   ( ) Dark
                  Accent colour
                    (•) ● Plum   ( ) ● Blue   ( ) ● Petrol   ( ) ● Graphite
                  Density
                    (•) Comfortable   ( ) Compact   "Compact fits more rows on screen."
                  Opens on
                    (•) My book   ( ) Weekly brief   "The screen cs-brief ui opens on."
                  [ Save changes ]
```
- **Radios** are real radio inputs with visible labels, not custom widgets. Accent options show a
  14px circle swatch in that accent (`.swatch--blue` and so on, set by class, never inline style)
  next to the name.
- **Live preview:** choosing a theme, accent or density applies it to the page immediately (`app.js`
  sets the `<html>` attributes) so people can see it before saving. Leaving without saving keeps
  the saved values.
- **Accent tokens.** All pass 4.5:1 on paper and surface in both themes:

  | Accent | Light | Dark |
  |---|---|---|
  | plum (default) | `#5A3E9B` | `#BBA6F2` |
  | blue | `#2C4FB0` | `#9DB4F7` |
  | petrol | `#1D5E78` | `#7CC6E0` |
  | graphite | `#3B4452` | `#C5CCD7` |

- **How settings reach the page:** `<html>` gets `data-theme="light|dark"` (left out for
  "Match my computer"), `data-accent`, and `data-density`.
- **Compact density** tightens table cell padding from 12px to 6px and the gaps between list items.
  Type sizes don't change.

#### Scoring
```
Scoring
Changes apply to every screen and to Claude once you save them.

Signals                                              | What would change
  Usage trend                                        | 10 accounts at risk now; 11 with these changes.
    Weight          [=========o--------]  25 points  |
    Risk starts at  [  0 ] and is maxed at [ -50 ]   |  1  Brindle Studio       No change
                    % change in active users, 30 days |  2  Thistledown Foods    ▲ Up 1
  Last login …                                        |  3  Corvane Freight      New in top 10 (was 14)
Health bands                                         |  4  Dunmere Legal        ▼ Down 2
  At risk from      [=====o====]  45                 |  …
  Watch from        [==o=======]  20                 |
How much ARR lifts priority  [===o======]  30%       |
Motion windows                                       |
  Onboarding lasts [ 90 ] days   Renewal window [ 120 ] days
[ Save changes ]   [ Reset to defaults ]
```
- **Sliders** are `<input type="range">` with a `<label>` and an `<output>` that shows the value and
  its unit. `app.js` keeps the output in sync. Weights run 0–50 in steps of 1, health bands 0–100
  in steps of 1, and ARR 0–100% in steps of 5. Start/full use `<input type="number">` with a unit
  hint under them.
- **The preview** (from 1100px a sticky right column, otherwise below the form) refreshes as
  values change. It uses htmx `hx-post` on `input changed delay:300ms` and returns a partial.
  - **Opening sentence:** "10 accounts at risk now; 11 with these changes." With no change it
    reads "No change to who is at risk."
  - **Top 10 list:** rank, name and movement. Movement is a word with an arrow icon: "Up 1",
    "Down 2", "No change", or "New in top 10 (was 14)". It uses neutral ink, because these aren't
    health colours.
- **Reset to defaults** doesn't save. It reloads the form with the built-in defaults
  (`?defaults=1`), so the preview shows the effect first. The status line then says "Showing the
  defaults. Save changes to use them."
- **Invalid combinations** (for example, Watch at or above At risk, or start equal to full) show a
  plain message next to the field ("Watch has to be below At risk.") and in the preview
  (`role="alert"`). Saving an invalid form re-renders with the messages and a 422, and writes
  nothing.

### Empty and error states (copy)
- **Search with no match:** `No accounts match "{q}".` followed by a **Clear search** button.
- **A motion with no accounts:** `No accounts are in {motion} right now.`
- **Pages not built yet:** `{Page name} is coming in a later build.` followed by a link back to
  My book. Don't use a placeholder illustration.

## Copy rules
- Use plain verbs and sentence case. Name things by what the CSM understands ("Why it's on your
  list", not "Top driver").
- Buttons say exactly what happens: "Clear search" and "Save changes" (later), never "Submit".
- Don't use middle-dot meta strings ("A · B · C"), arrows appended to links, or eyebrow labels
  above headings.
- Numbers: money as `$1.4M` in sentences and `$184,000` in the table; days as "41 days".

## Avoid (generic tells)
- stat-tile rows (a big number with a small label)
- gradient washes
- identical rounded cards with soft shadows
- coloured motion chips
- monospace data labels
- cream-and-terracotta palettes
- fade-up entrance animations on sections
- icons without words
