# Security policy

## How PrometheOS handles your data

- **Local only.** Each project store is a SQLite database on your computer. PrometheOS has no cloud service and no account.
- **No telemetry.** PrometheOS sends no usage data, crash reports or analytics anywhere.
- **Loopback hub.** The hub listens on `127.0.0.1` only. A browser pairs with a code shown in your terminal or in the desktop app; a coding agent uses its own bearer token at agent scope.
- **Network only when you ask.** Optional features that reach the network (installing Tectonic, a transcription model, an Overleaf Git push, a local Zotero query) start only from an explicit action of yours, and the default tests make no network requests.
- **Agents are not trusted with owner decisions.** Protected changes from an agent wait in the hub's approval queue until you grant or deny them.

## Supported versions

PrometheOS is pre-release. Security fixes go into the latest release and the `main` branch only.

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's **Report a vulnerability** button on this repository's **Security** tab (private vulnerability reporting). Do not open a public issue.

Include what you found, the version or commit, and how to reproduce it. Use synthetic data in any proof of concept. You can expect an acknowledgement within one week. This is a one-person project, so a fix may take longer; you will be told the plan and credited in the release notes if you wish.

## Scope

In scope: the core, the CLI, the MCP server, the hub and its local API, the desktop app and its installer, and the agent packages this repository generates.

Out of scope: vulnerabilities in coding agents, MCP hosts, Zotero, Tectonic or other software PrometheOS connects to (please report those upstream), and attacks that need an attacker already running code as your user.
