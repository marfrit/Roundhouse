# MVP12 — the page shows what this host runs, and the detail pane opens where you look

**A UI-only package.** No route, no snapshot key, no MCP tool, no actuation path
changes. `mvp1/static/index.html` is the only shipped file that moves; the tests
grow a static guard class. Requested by Markus 2026-10-14 after a design review of
the live page on boltzmann (1440 px desktop, 390 px phone, light + dark).

## Part 0 — question and substrate

**Question:** does an operator opening `https://boltzmann.fritz.box` see the
local units first, reach a unit's detail without scrolling, and use the page on a
phone without sideways panning?

**Rig:** `curie` (boltzmann Incus, headless Chromium via `/opt/degater`) runs
`rhmeasure.js` (scratch, not shipped): four contexts (desktop 1440×900 dark/light,
phone 390×844 @2x light/dark, `isMobile`), waits for the first ACTIVE row, clicks
it, records geometry. With the flag `actuate` it answers `/api/events` with the live
`/api/units` snapshot re-tagged `mode: actuate`, so the actuate-only header (token
field) is rendered without arming a server. Candidates are served by a second,
**read-only** Roundhouse on boltzmann (`--no-db --port 8098`, same unit dir, same
fleet peers) from a scratch copy of the working tree; the production instance on
:8099 is not touched until the package ships.

## Part 1 — baseline (live :8099, 0.11.0-1, 2026-10-14)

Raw, `rhmeasure.js https://boltzmann.fritz.box/` (read-only data, identical for
dark and light):

| metric | desktop 1440×900 | phone 390×844 |
|---|---|---|
| layout viewport `innerWidth` | 1440 | **543** (page wider than the device: it zooms out) |
| document height | 2235 | 2395 |
| ACTIVE section top / height | 358 / 134 | 478 / 396 |
| fleet section height | **846** | 846 |
| port board height | **540** | 68 (collapsed) |
| detail pane top after clicking the first ACTIVE row | **2235** (row was at 390; viewport 900 → pane not visible) | 0 (full screen) |
| close control hittable (Playwright trial click) | yes | **no** — `#detail-content`'s title row covers the floated button |
| elements painted `--amber` | `#mode-badge.actuate`, `.unit-row.conflict-armed` + every child incl. the boot toggle, `.badge.conflict`, `.port-cell.armed` | same minus the collapsed board |
| console errors | 0 | 0 |

Readings:

1. Desktop: clicking a row appears to do nothing. The pane is in-flow below fleet
   and port board (MVP3 F6 kept ≥ 1200 px byte-identical, so it never moved).
2. Phone: the header line (title + token field + gauge) does not wrap, the layout
   viewport grows to 543 px, everything is zoomed out, and the full-screen pane's
   close button sits under the title row.
3. Weight: the six local units take 268 px; the read-only fleet listing (half of
   it OFF/RETIRED rows) and the port board (one row per port, free ports as loud as
   the conflict) take 1386 px. Peers are listed twice (strip + fleet), the listener
   strip is a full-width box for one line.
4. Colour: amber means *armed port conflict* and also *normal operating mode*
   (`[ACTUATE]`). The conflict row paints its whole text amber on top of its badge.
   The loudest element on the page is the green `8099 roundhouse (self)` chip — a
   fact, not a state.
5. Rows say their state three times (section heading, glyph, `READY`/`STANDBY`
   word) and their alias twice (`nomic-embed-text :8082 alias nomic-embed-text`);
   the unit file name (`llama-embed.service`) appears only in the pane, so list and
   pane disagree on what the thing is called.

## Part 2 — acceptance (same rig, same data, candidate on :8098)

- **A1** desktop: after the click the pane's top is inside the viewport, the
  clicked row stays visible (not covered), the close control is hittable.
- **A2** phone: `innerWidth == 390` and `document.documentElement.scrollWidth == 390`
  (body's `overflow-x: hidden` hides overflow without removing it) in both themes, also
  in actuate mode (token field present); pane close hittable; pane content right
  edge ≤ 390.
- **A3** desktop: ACTIVE starts in the first 250 px; with this data the document is
  ≤ 1300 px tall (fleet + port board ≤ 500 px together).
- **A4** amber means *attention* — a conflict, a notice, a degraded source — and
  never a mode, a fact, or a frame. In the measured states it appears only on
  conflict elements (`.badge.conflict`, `.port-cell.armed`, the conflict row's edge
  marker); `#mode-badge` carries no alarm colour in either mode. Statically: a
  rule-by-rule `var(--amber)` allowlist like the red one (degraded banner, conflict
  badge/row/chip, toast, gate notice, unknown/suggested tags, preflight notice).
  Red/peer/fleet colour contracts (MVP2 §5, MVP7 §5.4, MVP8 §6.4) unchanged and
  still enforced by their tests.
- **A5** zero page/console errors; full test suite green.

## Part 3 — plan

What changes (CSS + rendering in `index.html` only):

1. **Detail pane as a side panel (> 700 px):** `position: fixed` right column,
   `width: clamp(320px, 42vw, 560px)`, own scroll; the page gets a matching right
   padding so the list stays visible beside it. (First drafted as "room only from
   1000 px, overlay below"; the code review showed the overlay covers every boot
   toggle at 768 px, so the room now starts at 701 px.) The phone full-screen rule (≤ 700 px) stays. The close button moves
   into a sticky bar at the top of the pane instead of floating under the title
   row; Escape closes the pane.
2. **Header wraps:** `flex-wrap` on the header line; on phones the token field
   takes its own line. Listener and peer strips become one compact text line each
   (no box). Mode badge: outlined, neutral, in both modes (text `[ACTUATE]` /
   `[READ-ONLY]` unchanged).
3. **Section order:** ACTIVE, FAILED, STANDBY, OFF, RETIRED, port board, fleet —
   what this host can act on first, what others run last. OFF/RETIRED toggles
   become `<button>` elements (same ids, same 44 px selector).
4. **Rows:** name = alias, else the unit name without `.service`; the unit name is
   shown muted only when it differs from the alias (so list and pane can be
   matched). The rung word is shown only where the section does not imply it
   (`LOADING`, `STARTING`, `BUSY` in ACTIVE). The `alias X` repetition goes. A
   conflict row keeps its badge and gets an amber left edge instead of amber text.
5. **Fleet (read-only, still non-interactive):** one block per peer, laid out as a
   grid; it lists only units that are not OFF/RETIRED and ends with a count line
   (`9 off · 4 retired`). No toggles — MVP8 §6.4 keeps the section free of targets.
6. **Port board:** chips in a wrapping row instead of one line per port; free ports
   muted, latent/armed/active conflicts as before; the self chip outlined neutral
   instead of green. An expanded conflict chip spans the full row.
7. **Type and surfaces:** a `--mono` stack (`ui-monospace, "JetBrains Mono",
   "DejaVu Sans Mono", Menlo, Consolas, monospace`) replaces `'Courier New'`; a
   `--surface` token replaces `--border` used as a background.

Not touched: routes, snapshot/SSE shapes, the frozen 44 px selector block, the
textContent-only rule, no storage APIs, the switch/edit/rollout flows and modals
(beyond the pane container they live in).

Tests: a `TestMvp12LayoutStatic` class pins the regressions statically (pane is
fixed above 700 px; no floated close; header wraps; no `'Courier New'`; mode badge
free of alarm colours; FAILED before OFF and port board before fleet in the
markup; fleet renderer drops OFF/RETIRED rows and counts them). Computed layout is
proven by the rig, not by those tests — they prove the rules exist.

## Part 4 — plan review (Fable, 2026-10-14) and what changed because of it

1. **`overlay-open` is shared by the pane and both modals** — closing a modal
   stripped the pane's room (and, on phones, the scroll lock) while the pane stayed
   open. The room is now keyed on `body:has(#detail-pane.active)`; the
   three close functions go through `releaseOverlay()`, which drops the lock only
   when neither the pane nor a modal is still open.
2. **A4 as first written could not pass** — amber also marks notices and the
   degraded banner, and the old pane frame was amber. A4 now states the semantics,
   the pane frame is neutral, and an allowlist test pins it.
3. **Edit form inside a 560 px pane** — forced to one column inside the pane.
4. **A persistent pane next to a live list goes stale** — `rung` and `enablement`
   events for the open unit re-fetch its detail, except while its edit form is open;
   the open unit's row is marked `.selected` (neutral tokens).
5. **Untested breakpoints and moving data** — the rig gained a 1024×768 context and
   now replays one captured snapshot + fleet roster into both pages (EventSource is
   replaced before page scripts run), so baseline and candidate see identical data.
6. **Superseded contracts**, stated here so the next reader of the specs is not
   misled: MVP3-SPEC F6 ("≥ 1200 px byte-wise unchanged"), SPEC.md §5 item 8 (pane
   "below the list") and item 2 (row format `RUNG alias :port alias …`), MVP8-SPEC
   §6.4 (one fleet row per unit — OFF/RETIRED are now counted, not listed). The
   frozen 44 px block is untouched byte-for-byte.
7. **Escape precedence** — an open modal wins; Escape in an input or with the edit
   form open does nothing.
8. Port board / toggles: `#port-board-cells` wraps, `[show]` uses `.link-button`,
   `off-section-toggle` sits on the button, not the heading.
9. **Mode badge** — inverse (ink on surface) for ACTUATE, outline for READ-ONLY:
   distinct without an alarm colour.

## Part 5 — result (candidate on :8098, frozen data captured from :8099)

Same replayed snapshot (26 units, mode actuate) and fleet roster (4 peers, 52
units) for both pages; `rhmeasure2.js`:

| metric | baseline 1440 | MVP12 1440 | baseline 1024 | MVP12 1024 | baseline phone | MVP12 phone |
|---|---|---|---|---|---|---|
| layout viewport width | 1440 | 1440 | 1024 | 1024 | **543** | **390** |
| document height | 2235 | **900** (= viewport) | 2283 | **768** | 2395 | 1295 |
| ACTIVE top | 358 | 115 | 382 | 144 | 478 | 266 |
| fleet + port board height | 1386 | 250 | 1386 | 250 | 914 | 319 |
| pane top inside viewport after click | **no** (y 2235) | **yes** (y 16) | no | yes | yes | yes |
| clicked row + its boot toggle visible beside the pane | — | yes | — | yes | n/a (full screen) | n/a |
| close control hittable | yes | yes | yes | yes | **no** | **yes** |
| pane content right edge ≤ viewport | — | yes | — | yes | 527 > 390 | 375 |
| amber elements | mode badge, whole conflict row incl. toggle, badge, chip | conflict row edge, badge, chip | same as 1440 | same as 1440 | mode badge, row, badge | row edge, badge |
| page errors | 0 | 0 | 0 | 0 | 0 | 0 |

Light and dark are identical in geometry; read-only mode (no token field) measured
too — same verdicts. A1–A5 met. `tests/test_server.py` 201 OK, the new
`TestMvp12LayoutStatic` (12 tests) fails 12/12 against the 0.11.0 page — it can go red.

## Part 6 — code review (Fable, b5fbe82) and the follow-up

Nothing blocking; frozen 44 px block md5-identical; 12/12 new tests red against
0.11.0 confirmed independently. Folded in:

- **M1** a serving unit flips BUSY↔READY per request, so an open pane would
  re-fetch twice per inference and late answers could paint out of order —
  refreshes are coalesced (250 ms) and sequenced (`paneSeq`); an answer paints only
  if it is the newest, the pane still shows that unit and no edit form opened
  meanwhile.
- **L1** clicking row B before row A's detail arrived let A paint under B's
  highlight — `fetchUnitDetail` uses the same sequence.
- **L2** 701–999 px: the overlay covered every boot toggle — the room now starts
  at 701 px with one clamped `--pane-w` token for pane and padding. Measured at
  768×1024 (below): row and toggle visible beside the pane.
- **L3** the amber test now also scans inline `'var(--amber)'` in the script and
  allows it only at the switch preview's attention lines (fit verdict, hint,
  notices); the overlay-lock test no longer slices at the first `}`.

| 768×1024 light | baseline | MVP12 |
|---|---|---|
| document height | 2379 | 1024 (= viewport) |
| pane top after click | 2379 (off screen) | 16 |
| clicked row + boot toggle visible beside pane | — | yes |
| fleet + port board | 1386 | 278 |

Full suite: `mvp1/tests` 949 OK, root `tests` 16 OK.
