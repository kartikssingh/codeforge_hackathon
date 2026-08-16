# ARIA frontend

Electron shell and dashboard for the ARIA backend.

```bash
npm install
npm start            # starts the backend too, if one is not already running
npm run ui-only      # attach to a backend someone else started
npm run dev          # with the inspector attached
```

---

## Layout

```
electron/
  main.js      process lifecycle: spawn the backend, poll /health, own the
               window, shut the backend down cleanly, show a readable error
               dialog instead of a blank screen when boot fails
  preload.js   the ONLY code that touches the network — window.aria.* over
               Node's http module, plus an SSE subscription that reconnects

index.html     three-column shell and the three dialogs
styles.css     design system: tokens, three themes, every component
js/
  util.js            esc(), time formatting, DOM helpers
  toast.js           transient notifications
  api.js             wraps window.aria; every failure becomes a toast
  store.js           single source of truth + subscribe/notify
  view-inventory.js  stock panel
  view-board.js      metric strip, filters, incident cards, timer tick
  view-volunteers.js volunteer activity board
  view-analysis.js   review · incident detail · agent log
  modals.js          return · override · confirm (focus-trapped)
  actions.js         every state-changing operation
  intake.js          audio and text submission
  controls.js        the small forms
  app.js             bootstrap, SSE wiring, render loop, shortcuts
```

Scripts are classic, not ES modules: module scripts do not load over `file://`.
Each file is an IIFE that attaches one object to `window.ARIA`, and load order
in `index.html` is the dependency order.

---

## How it works

**One state object.** `store.js` holds everything; views read `store.state` and
redraw when notified. No view fetches, and no view calls another. Every
mutating endpoint returns the whole board, so the three panels always describe
the same instant.

**Live updates.** `preload.js` holds one SSE connection and reconnects with
exponential backoff. An event says *that* something changed; `app.js` responds
by fetching `/board` once (coalesced). Polling remains as a slow safety net —
10 s when the stream is down, 30 s when it is healthy.

**Timers are surgical.** A one-second interval updates only elements carrying
`data-countdown` / `data-elapsed`. Cards are not re-rendered, so a stepper you
are editing does not lose focus.

**Everything is escaped.** Transcripts and situation labels come from a model
processing a stranger's voice. Every interpolated value goes through
`util.esc()`.

**Nothing renders from the network directly.** The renderer has no network
access at all: its CSP is `default-src 'none'; script-src 'self'`, with no
`connect-src`. All traffic goes through the preload bridge.

---

## Keyboard

| Key | Action |
|---|---|
| `A` / `T` | Audio / typed intake |
| `R` / `D` / `L` | Review · incident detail · agent log |
| `G` | Refresh the board |
| `/` | Search stock |
| `Ctrl+Enter` | Submit a typed report |
| `Esc` | Close a dialog |

---

## Themes

Three, switched in the top bar and remembered in `localStorage`: **dark**
(default), **light**, and **hc** — a high-contrast phosphor-on-black scheme for
operating in the dark without ruining night vision.

All colours come from CSS custom properties defined once per theme in
`styles.css`. Severity is never carried by colour alone; every card and badge
also spells the level out.

---

## Adding a panel

1. Add markup with a stable `id` in `index.html`.
2. Create `js/view-thing.js` exporting `render(state)` and `mount()`.
3. Add it to the `VIEWS` array and the `mount()` sequence in `app.js`, and to
   the script list in `index.html`.

Keep `render` a pure function of `state` — that is what makes a redraw on every
change cheap and predictable.
