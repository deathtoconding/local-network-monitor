# Dashboard design specification

The dashboard is the only part of this project a non-programmer sees. It is
therefore held to the same standard as the collectors: every claim must be
provable from data the API actually returns, and every state must be visible.

This document is the reference for *why* the page looks the way it does. The
rules here are enforced by `tests/test_dashboard.py` (18 tests) and verified
against real API payloads in a DOM during development.

Files: `src/network_monitor/web/index.html`, `css/app.css`, `js/app.js` — three
static files, no build step, no CDN, no external fonts.

---

## 1. The story

The page is read top to bottom as a narrative: *what is happening → what is
wrong → what has been happening → who is doing it → where it goes → can I trust
this page*. Each section is a real heading, so the order is structure rather than
a visual accident.

| # | Section | Question it answers | Content | When there is nothing to show |
|---|---|---|---|---|
| 1 | **Right now** | Is anything happening, and is it fine? | Plain-language verdict, live download/upload rates with a 15-minute sparkline, and five numbers: open connections, processes online, events in 24 h, uptime, readiness | "Quiet" with the timestamp of the last cycle |
| 2 | **Needs attention** | What broke, and why? | Warnings and criticals, newest first, each with the reasoning the rule recorded; click for structured evidence. Informational events are folded into a disclosure | An explicit, calm "Nothing needs your attention" card — not an empty box |
| 3 | **Traffic trend** | What has been happening? | 15-minute area chart, hover crosshair with per-sample rates, caption stating samples, window and peak | "collecting the first samples…" |
| 4 | **Who** | Which process owns those connections? | Processes ranked by open connections with remote-endpoint and state counts, then the full connection table behind a disclosure with filters | "No process currently owns a TCP connection." |
| 5 | **Where** | Which interface carries it? | Per-interface state, rates, cumulative bytes, errors and drops; non-zero error counts highlighted | "no interfaces detected" |
| 6 | **The monitor itself** | Can I trust this page? | Collector runs/failures/durations, readiness checks, stored rows, host facts, and links to the raw JSON endpoints | "no collector data yet" |

### The verdict line

The headline is one sentence, derived only from data already on the page — never
speculation:

| Condition (checked in order) | Headline | Tone |
|---|---|---|
| critical event in the window | "Something needs you now" | critical |
| warning event in the window | "Worth a look" | warning |
| API unreachable | "Waiting for the monitor" | warning |
| `ready: false` | "Collecting, but not fully ready" | warning |
| no measurable traffic | "Quiet" | calm |
| otherwise | "Traffic is flowing" | active |

---

## 2. Layout

```
┌───────────────────────────────────────────────────────────────────────┐
│ masthead: pulse · title · database/cycle     freshness chip · controls │
├───────────────────────────────────────────────────────────────────────┤
│ [partial-failure banner — only when a section could not be refreshed]  │
├───────────────────────────────────────────────────────────────────────┤
│ 1 · RIGHT NOW                                                          │
│   verdict headline                                                     │
│   one-sentence explanation                                             │
│   ┌──────────┬──────────┬──────────────────────────────┐               │
│   │ download │ upload   │ 15-minute sparkline          │               │
│   └──────────┴──────────┴──────────────────────────────┘               │
│   [ connections | processes | events 24h | uptime | readiness ]        │
├───────────────────────────────────────────────────────────────────────┤
│ 2 · NEEDS ATTENTION   warnings and criticals + folded info events      │
│ 3 · TRAFFIC TREND     area chart + tooltip + caption                   │
│ 4 · WHO               process ranking + ▸ all connections (table)      │
│ 5 · WHERE             interface table                                 │
│ 6 · MONITOR           collector table + storage / readiness / host     │
├───────────────────────────────────────────────────────────────────────┤
│ footer: version · last cycle · bind/scope reminders                    │
└───────────────────────────────────────────────────────────────────────┘
```

The layout is a single fluid column capped at 1200 px: one grid for the hero,
`auto-fit` grids for the stat strip and the three health columns, and a
horizontally scrollable table wrapper. Breakpoints at 900 px (sparkline drops to
its own row, process host list folds away) and 640 px (single-column hero, event
rows restack) keep it usable on a laptop and a phone.

---

## 3. Design tokens

One token set drives both themes; `app.css` is the source of truth.

| Group | Tokens | Purpose |
|---|---|---|
| Surfaces | `--surface-0…3` | page, panel, raised, hover |
| Lines | `--line`, `--line-soft` | borders and table rules |
| Text | `--text`, `--text-muted`, `--text-dim` | primary, supporting, tertiary |
| Roles | `--down`, `--up`, `--ok`, `--warn`, `--crit`, `--info` (+ `-soft` tints) | meaning, not decoration |
| Space | `--s1…--s8` (4 → 44 px) | every margin and padding |
| Type | `--t-xs` 11.5 → `--t-num` 34 px, `--sans`, `--mono` | hierarchy; numbers always monospace |
| Shape | `--r-sm/md/lg/pill`, `--shadow-1/2` | consistent rounding and elevation |
| Focus | `--focus` | visible keyboard focus on any surface |

Both themes are first class: dark by default, light via `prefers-color-scheme`
and an explicit toggle (auto → light → dark) remembered in `localStorage`.
Contrast targets AA for text at the smallest sizes used (`--t-xs`), which is why
`--text-dim` is a deliberate step lighter than a pure grey.

---

## 4. Interaction patterns

| Pattern | How it appears | Why |
|---|---|---|
| **Freshness first** | "live · updated 2 s ago" → "lagging · …" (amber, >3 intervals) → "stale · …" (red, >6) → "paused · updated …" | A monitoring page that presents stale data as current is the worst failure mode it has |
| **Partial failure is visible, never fatal** | Endpoints fetched in parallel with a 6 s timeout; failed sections keep their last values and the banner names them | Blanking a panel or showing "0" during an outage would be a lie |
| **Progressive disclosure** | headline → list → row → dialog → raw JSON; connection table and collector detail behind `<details>` | The glance stays readable without hiding anything permanently |
| **Severity is never colour alone** | Every state pairs a colour with a word and a glyph (`▲ critical`, `● warning`, `○ info`) | Colour-blind operators, greyscale printing, screen readers |
| **Explicit empty states** | Each list has a written sentence for "nothing here", and the empty state is reassuring where that is honest | "Empty" and "broken" must not look the same |
| **Skeletons, then data** | `body[data-state="loading"]` shows shimmer rows until the first successful refresh | No layout jump, no flash of "0" |
| **Evidence in words** | The dialog maps rule evidence keys to labels (`threshold_mbps` → "Configured threshold (Mb/s)") and keeps the raw JSON one click away | The reader should not have to decode key names to understand an alert |
| **Filters are local** | Process / remote / state filters re-render from the already-fetched snapshot; no extra requests | Filtering stays instant and cannot hammer the API |
| **Honest scope** | A standing note states that traffic is measured per interface and per-process byte totals are not claimed | The specification forbids implying more precision than exists |

---

## 5. Accessibility commitments

- One `h1`, real `<header>` / `<main>` / `<footer>` landmarks, and a skip link.
- The verdict is an `aria-live="polite"` region; freshness is a live status; the
  partial-failure banner is `role="alert"`.
- `<main>` carries `aria-busy` while a refresh is in flight.
- Every table has a `<caption>` and `<th scope>`; canvases have text alternatives
  and the chart's caption repeats the essential numbers in text.
- Keyboard: everything interactive is a real `<button>`/`<select>`/`<input>`,
  focus is always visible (`:focus-visible`), and the evidence dialog closes with
  Escape (native `<dialog>`, with an attribute fallback for older engines).
- `prefers-reduced-motion: reduce` disables every animation and transition.
- No `innerHTML` anywhere in the script: all text is written with `textContent`,
  so a process name containing markup can never become markup.

---

## 6. Changing the dashboard

1. Read the story order — if a change reorders or renames a section, update
   `STORY_SECTIONS` in `tests/test_dashboard.py` deliberately rather than editing
   the test to silence it.
2. Keep element ids and the script in step: the contract test fails if the script
   queries an element the markup does not contain (this caught the refresh button
   that one redesign dropped).
3. Do not introduce a remote resource — the offline invariant is asserted for
   HTML, CSS and JS.
4. New semantic colours or spacings go in the token block, not inline.
5. Verify behaviour against real payloads: run the monitor, then use the
   fixtures approach described in `CONTRIBUTING.md` (jsdom) if you touch the
   rendering paths.
6. Update this document and `CHANGELOG.md` in the same pull request.
