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
