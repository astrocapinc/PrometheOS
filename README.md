# PrometheOS

A local research ledger for researchers who work with coding agents.

PrometheOS keeps the durable record of a research project: sources and their exact versions, scoped claims and their corrections, notebook entries, meetings, decisions, work items and the evidence behind a paper. Your coding agent (Claude Code, OpenCode or any MCP host) does the work; PrometheOS gives it bounded context and records what happened, and you approve what matters. Everything stays on your computer.

<!-- Screenshot slot: the hub UI/UX pass supplies docs/images/hub.png. -->
> Screenshot pending.

## What it is, and is not

- **A ledger beneath any agent.** One SQLite store per project, with transactional revision history, verified backups and staged restore. Agents read and write through one application layer, over the CLI or MCP, and protected changes wait for your approval.
- **A companion hub.** A local web UI on `127.0.0.1` (also packaged as a Windows desktop app) to read the record, review approvals, follow work items and read papers next to the claims that cite them.
- **Not an agent.** PrometheOS never runs a model of its own by default, never starts or supervises agent runs, and has no cloud service. There is no telemetry.

## Status

Pre-release, version 0.1.0.dev0. Its author started using it on their own research, on Windows 11, in October 2026. The core, the hub, the desktop shell and the agent package have automated tests and recorded acceptance evidence, but the project has had no outside users yet. Expect rough edges and changes to the store format before 1.0, though every format change ships with a tested migration.

| Area | State |
|---|---|
| Ledger, CLI, MCP server, backups and restore | Built and tested |
| Hub (sources, claims, approvals, work items, library, LaTeX workspace) | Built and tested |
| Agent packages for Claude Code and OpenCode | Built and tested on fixtures; live sessions with real hosts not yet recorded |
| Windows desktop app (Electron, per-user installer) | Built and tested; the installer is not code-signed |
| Folder adoption (scan an existing project; MATLAB parsing optional) | Built and tested |
| Literature library and local Zotero import | Built and tested |
| Meetings (transcript import; optional local transcription) | Built; live microphone capture is not validated |
| Local AI, literature discovery and watches, meeting-to-deliverable recipes, calendar connectors | Not built yet |

## Platform support

- **Windows 11 x64**: the tested platform, for the core, the hub and the desktop app.
- **Linux**: the core and the hub are meant to work, but Linux has not been exercised yet. The CI workflow is set up to run the core test subset on Ubuntu; until that job has passed once, it reports without blocking.
- **macOS**: not tested. There is no desktop build for it.

## Install

### From the Windows installer

When a release is published, download `PrometheOS Setup <version>.exe` from the Releases page and run it. It installs for your user only, with no administrator rights, under `%LOCALAPPDATA%\Programs\prometheos-desktop`. The installer is not code-signed yet, so Windows SmartScreen warns before it runs. Your data lives in `%LOCALAPPDATA%\PrometheOS-v1` (or `PROMETHEOS_HOME`), and uninstalling never touches it.

### From source

You need [uv](https://docs.astral.sh/uv/) and Git. Node.js 24 is needed only to rebuild the hub's web assets or the desktop app; the built assets are in the repository.

```bash
git clone <repository url> prometheos
cd prometheos
uv sync --locked
uv run prometheos --version
```

Optional extras: `uv sync --locked --extra adoption-parsers` adds MATLAB parsing for folder adoption, and `--extra audio` adds local transcription (see [the notices](THIRD_PARTY_NOTICES.md) before redistributing it).

## Quick start

```bash
# Create a project. The objective may say it is still undecided.
uv run prometheos project init --title "Thesis" --objective "undecided" --timezone Europe/Berlin

# See projects, then what is due, changed or waiting for you.
uv run prometheos project list
uv run prometheos now --project <project id>

# Open the hub in your browser (loopback only; the pairing code is shown in this terminal).
uv run prometheos hub serve --project <project id> --open
```

`uv run prometheos --help` lists every command. Each one has `--help`, and most have `--json` for scripts.

## Connect a coding agent

PrometheOS speaks MCP through the official Python SDK. Every agent gets the same tools at agent scope; none gets an operation of its own.

- **Claude Code**: `prometheos host setup --host claude-code-plugin --target <folder> --project <id>` shows a local plugin (MCP server, bundled skills and a read-only session-start hook); add `--apply` to write it, then run `/plugin marketplace add <folder>` and `/plugin install prometheos@prometheos-local` in Claude Code.
- **OpenCode**: `prometheos host setup --host opencode --target <coding folder> --project <id> --apply`.
- **Any other MCP host**: run `prometheos mcp serve --project <id>` as a stdio server.

Setup always shows its plan first, writes only into the folder you name, and never changes an agent's global settings. Details are in [docs/agent-package.md](docs/agent-package.md).

## Honest limits

- One researcher per project store. There is no sync, no shared live store and no automatic merge.
- PrometheOS records what agents report; it cannot prove that an agent did what it said.
- Real agent hosts change their formats. The Claude Code and OpenCode setups follow each host's documented configuration and are tested on fixtures; a live session with each real host has not been recorded yet.
- Optional integrations (Zotero's local API, Tectonic for LaTeX, transcription models) are installed by you and keep their own licences; PrometheOS never bundles them.

## Documentation

- [docs/agent-package.md](docs/agent-package.md): connecting coding agents.
- [desktop/app/README.md](desktop/app/README.md): building and testing the desktop app.
- [docs/development.md](docs/development.md): how the project is developed, tested and accepted.
- [spec/README.md](spec/README.md): the specification the implementation follows.
- [docs/release/inventory.md](docs/release/inventory.md): what ships, with which licences.

## Contributing, security and licence

Contributions are welcome under the [Developer Certificate of Origin](CONTRIBUTING.md). Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). This project follows a [code of conduct](CODE_OF_CONDUCT.md).

PrometheOS is licensed under the [Apache License 2.0](LICENSE). Third-party components keep their own licences; see [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). A software licence covers the code only: it grants nothing over the papers, recordings or data you keep in PrometheOS.
