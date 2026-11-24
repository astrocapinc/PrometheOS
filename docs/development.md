# How PrometheOS is developed

PrometheOS is built from a written specification ([spec/README.md](../spec/README.md)) in checkpoints, each with required checks, recorded evidence and an independent review. This page is for contributors who want to work the same way. [AGENTS.md](../AGENTS.md) holds the full rules that coding agents follow in this repository.

## Layout

| Path | What it holds |
|---|---|
| `src/prometheos/` | The Python core: store, application services, CLI, MCP adapter, hub server and the built hub assets (`hub/static/`) |
| `hub/` | The hub's React 19 + Vite sources and its browser tests |
| `desktop/app/` | The Electron desktop app, its core build and its installer test |
| `contracts/`, `schemas/` | Public JSON Schemas with valid and invalid examples |
| `spec/`, `checkpoints.json`, `requirements.json`, `acceptance/` | The specification, its checkpoint graph and its scenarios |
| `fixtures/` | Synthetic research material for tests; never real research |
| `tests/` | The Python test suite |
| `tools/` | Development tools: contract checks, the dependency register, walks, builds and the public export |
| `development/` | The private repository's state, receipts, decisions and evidence; not part of the public export |

## Everyday commands

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python tools/check_contracts.py
uv run python tools/verify_package.py
```

Plain `uv run pytest` runs the suite in one process. To run it in parallel, run `uv run pytest -n auto -m "not serial"` and then `uv run pytest -p no:xdist -m serial`: the serial modules kill or race real worker processes against short delays and must run alone.

For the hub (Node.js 24, from `hub/`): `npm ci`, `npm run typecheck`, `npm run test:unit`, and `npm run test:e2e` for the browser tests. After a hub change, `uv run python tools/build_hub.py` rebuilds the shipped assets and `uv run python tools/check_hub_assets.py` checks them.

## What runs where

The [CI workflow](../.github/workflows/ci.yml) runs on Windows and Ubuntu: lint, format, types, the contract and package checks, the dependency register check, the fast Python tests and the hub's typecheck and unit tests. Some checks run only on a developer's Windows machine, because they need software or devices CI does not have:

- the hub's browser tests and the end-to-end walks (`tools/walk_*.py`), which drive a real browser or the desktop window;
- the desktop shell test, the installer check and the release build (`desktop/app/README.md`);
- tests marked `slow` (process-kill and concurrency probes), `real_model` (an installed speech model) and `audio_extra` (the optional audio extra);
- anything that needs Tectonic, MATLAB, a microphone, Zotero or a real coding host.

## Checkpoints, receipts and review

Each checkpoint in `checkpoints.json` lists required checks. A checkpoint is accepted only when its checks pass on a recorded candidate tree, its scope is met and a reviewer other than the implementer accepts the evidence. The receipts and their evidence live in `development/receipts/` of the private repository; `tools/verify_receipt.py` validates them. Field gates (a real researcher, a real coding host, real hardware) need real evidence and are never simulated.

Changes to sealed specification files go through a reviewed amendment that bumps the spec version (`tools/seal_package.py`, `development/spec-history/`).

## The public repository

The public repository is a fresh export of the private one, built by `tools/public_export.py`. It takes an allow-list of paths, leaves out `development/`, local data and build outputs, runs the privacy and secret scan (`tools/privacy_scan.py`) and commits the result as one new repository. Nothing is pushed by the tool.
