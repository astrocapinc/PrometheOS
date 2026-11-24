# PrometheOS researcher hub (web UI, CP5)

A TypeScript + React + Vite single-page app served by the PrometheOS hub
(`prometheos.hub.app`) under its strict CSP. It is an orientation and review surface;
execution stays in the coding agent.

- Destinations: **Now**, **Notebook**, **Knowledge**, **Papers**. Contextual pages:
  meetings, corrections, actions, skills and workflow runs, Capture.
- A side **context panel** shows retained source text with the cited lines highlighted.
- Owner decisions (adopting a meeting item, applying a correction) always go
  preview → explicit Approve → apply-with-digest.

## Requirements

- Node 24 and npm 11 (tested: Node v24.15.0, npm 11.12.1).
- This repository's Python environment (`uv sync` at the repository root), for the browser tests.
- Playwright's own Chromium headless shell, already installed in the Playwright browser cache
  (`PLAYWRIGHT_BROWSERS_PATH`, or the per-user default) at the revision `@playwright/test`
  expects (r1243, Chrome Headless Shell 153). The tools never download a browser, and no
  installed Edge or Chrome is used: see the browser boundary below.

## Commands

Run from this folder.

```sh
# install exactly the locked dependencies, without downloading Playwright browsers
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci

npm run typecheck          # tsc over src/
npm run build              # tsc + vite build -> dist/
npm run test:e2e           # Playwright against the real hub (see below)
npm run licenses           # regenerate LICENSES.md from node_modules
```

PowerShell equivalent of the install line:
`$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"; npm ci`.

Keep the full test log: `npx playwright test > test-output.txt 2>&1`.

### Browser tests against the real backend

Build and ship the assets first (from the repository root):

```sh
uv run python tools/build_hub.py      # npm ci + build, copies dist/ into the package
uv run python tools/check_hub_assets.py
```

`e2e/global-setup.ts` then starts, from the repository root,

```sh
uv run python tools/hub_fixture.py --info hub/e2e/.state/info.json --tokens 150
```

which builds a disposable synthetic project (scoped claims, a captured meeting with
agent-proposed items, a pending correction, retained sources, an imported synthetic skill
package and a notebook entry citing exact source lines) and serves the real hub on
127.0.0.1 at a free port with the assets shipped in the package. `HUB_STATIC=dist` serves
this folder's `dist/` instead. Teardown kills the process tree.

- The repository defaults to `..`; override with `PROMETHEOS_REPO=<path>`.
- Each test pairs with its own one-use token through `POST /api/v1/pair`; state is
  asserted by re-reading records through a separately paired API client.
- Tests share one fixture project and run in one worker in file order
  (`02-journeys` is serial: adopt → return → correct). No retries.
- Screenshots (supplementary evidence) are written to the run's own folder (`E2E_SHOTS`, set by
  `tools/run_hub_e2e.py --evidence DIR`; `test-results/screenshots` when run directly). The
  committed `screenshots/` images are not rewritten by a run.

### Browser boundary

Every Playwright suite (`playwright.config.ts` and the C10-WALK's `playwright.walk.config.ts`)
spreads the options in `e2e/boundary.ts`: the Chromium headless shell in a fresh temporary
profile (no `channel`, no persistent context), with every non-loopback request sent to a closed
local port, no DNS for non-local names, and WebRTC and QUIC off the direct path. The first spec
of each suite (`e2e/00-boundary.spec.ts`, `walk/00-boundary.spec.ts`) proves it against reserved
non-routable targets only, as the `boundary` Playwright project: a dependency of the journeys, so
it runs before them under any spec or `--grep` filter and the journeys do not run if a control
fails. `tools/run_hub_e2e.py` also requires the boundary evidence and refuses `--no-deps`,
`--retries` and `--repeat-each`; each journey worker re-checks its own browser. It
constrains the browser's own traffic, not the Node processes of the harness. See
`development/decisions/0028-cp10-browser-test-boundary.md`.

### Local manual preview

```sh
uv run python tools/hub_fixture.py --info %TEMP%/hub-info.json
```

Open the printed URL and type one of the `pairing_tokens` from the info file into the
pairing form. Synthetic data only; never point this at real research.

## Security notes

- Session: HttpOnly SameSite=Strict cookie set by the hub. The CSRF token is held in
  memory only; nothing credential-like is written to localStorage/sessionStorage/URLs.
  The localStorage keys are `prometheos.hub.lastSeenSeq.<project_id>` (a number) and, only once
  the owner picks a theme, `prometheos.hub.theme` (`dark`, `light` or `system`).
- The pairing code is typed into a `method="post"` form and sent in a POST body.
- Built assets contain no inline script or style, no external URLs, no fonts or CDNs.
- Record text is rendered as React text nodes; no `dangerouslySetInnerHTML`, no
  Markdown/HTML rendering of record content.

## Layout

```
src/api.ts            fetch wrapper, CSRF header, error envelope -> ApiError kinds
src/hub.tsx           session, project snapshot, change-feed long-poll, useQuery, announcer
src/router.tsx        tiny History-API router (no dependency)
src/components/       shared states, ContextPanel, DecisionReview (preview/approve/apply)
src/pages/            Now, Notebook, Knowledge, Papers, Capture, Meeting, Correction,
                      Action, Skills (+ workflow run), Pairing, NotFound
e2e/                  Playwright specs, fixtures, global setup
tools/licenses.mjs    LICENSES.md generator
```
