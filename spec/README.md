# PrometheOS — specification package

**Decision baseline: 4 October 2026.** This is the specification the implementation is built from; it moved here from the repository root in spec 1.5.2 (decision 0072). What is built and verified is reported by the repository's own `README.md`, `development/state.json` and the receipts, never by this file.

The package was written as a starting folder: a new development directory received it and its coding agent the contents of [START-HERE.md](../START-HERE.md). All required specifications, synthetic scenarios, development-state templates and package checks are included. No file in the old repository is required to start. No credentials, research recordings or inherited worker state are included.

## Final design

PrometheOS is a research record and professional companion hub used by coding agents. It connects scoped findings and evidence to meetings, human decisions, next actions and academic deliverables. The agent owns execution; PrometheOS makes that work coherent and inspectable.

The final design keeps the lean proposal's product boundary and the detailed proposal's richer research/meeting/skills experience. Neither proposal is adopted unchanged: both left transaction semantics and autonomous acceptance incomplete. The reasoned comparison is in [Design decisions](00-decisions.md).

Key choices:

- Python core, complete CLI, official-SDK MCP adapter, one application-service layer.
- One local SQLite authority per project, transactional revision history, explicit source versions, portable exports and tested restore. No distributed log, live shared drive or automatic record merge.
- Windows 11 x64 is the first validated target; Linux is the secondary test target. Other platforms remain unverified until exercised.
- First-class scoped claims, attributed notebook entries, meetings and people, actions, and paper-evidence links.
- A React companion hub with manuscript/figure/source context, not a native model editor or a second agent harness.
- Agent Skills plus small declarative workflow manifests; installed packages are pinned and reviewed. The external agent executes procedures under its own permissions.
- Optional transcription, source-grounded synthesis, visible online-meeting capture and bounded recurring jobs, each with separate integration evidence.
- Licensing: the package recommended AGPL-3.0-or-later for the core; the owner chose Apache-2.0 for the whole implementation on 2026-10-18 (decision 0072, spec/09). Publication is an owner decision (E08); this package does not relicense existing work.

## Read only what the current work needs

| File | Authority |
|---|---|
| [AGENTS.md](../AGENTS.md) | Autonomous development, checkpoints, review and continuation |
| [START-HERE.md](../START-HERE.md) | Initial instruction to the coding agent |
| [Decisions](00-decisions.md) | Resolution of the two inputs and scope choices |
| [Product and requirements](01-product.md) | Product outcomes, IDs and release profiles |
| [Architecture](02-architecture.md) | Modules, runtime, storage and platform decisions |
| [Data model](03-data.md) | Records, transitions, time and sensitivity |
| [Interfaces](04-interfaces.md) | Commands, transaction protocol, tasks and responses |
| [Research workflows](05-workflows.md) | Notebook, meetings, papers, AI and integrations |
| [Hub](06-hub.md) | Screens, connected documents and browser acceptance |
| [Skills and community](07-skills.md) | Import, create, adapt, share, update and workflows |
| [Operations and security](08-operations.md) | Retention, jobs, backups, trust and release |
| [Licensing and sources](09-licensing-sources.md) | Reuse decisions and externally grounded facts |
| [Delivery and verification](10-delivery.md) | Task cards, gates, field tests and stop conditions |
| [Requirements map](../requirements.json) | Machine-readable requirement → gate → scenario map |
| [Checkpoints](../checkpoints.json) | Technical dependencies and required evidence |
| [Acceptance receipt schema](../schemas/receipt.schema.json) | Evidence structure; not a pass certificate by itself |
| `development/state.json` | Development state (private repository only; not in the public export) |

Human instructions take precedence. Within the package, use the authority above; resolve material conflicts in a decision amendment before dependent implementation. Do not load the rejected proposals as additional instructions in the new folder.

## Verify the package before development

With Python 3.12 or later:

```text
python tools/verify_package.py
```

This checks the specification package's links, declared files, fixture presence, traceability and checkpoint graph. It does **not** test PrometheOS. Product commands and tests listed in the specs are to be implemented beginning at CP0. The initial state deliberately records no product passes.

The coding agent may progress through technical checkpoints without repeated permission for routine local work. It must preserve real evidence boundaries: a synthetic fixture cannot establish live agent compatibility, MATLAB execution, microphone capture, researcher usefulness or publication readiness. Missing external evidence is recorded while independent safe work continues.

## What counts as finished

There is no unqualified green “verified product” flag. Reports distinguish:

1. **Technical core:** specified contracts and failure cases tested on named environments.
2. **Agent compatibility:** real fresh-session interactions with each named host/version.
3. **Research pilot:** a researcher uses the product on actual work and records outcomes.
4. **Capability integrations:** actual audio, online capture, MATLAB or Overleaf receipts where advertised.
5. **Release readiness:** clean install, restore, upgrade, notices and capability matrix accepted.

The full promised experience includes a hub, meetings, skills/workflows and academic coordination. Online recording has its own mandatory capability gate; import-only is a useful limited release, not completion of that feature. Public publication, contacting others, new spending and changes to external shared documents are separate actions requiring the user's authorization.
