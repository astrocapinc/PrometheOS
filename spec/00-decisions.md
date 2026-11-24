# Final decisions and input assessment

## Why neither input is copied unchanged

Input A is the `prometheos-spec` proposal (21 Markdown files). Input B is the `files` proposal (16 Markdown files). The input inventory records exact hashes in `design-inputs.json`; source names are provenance, not dependencies to load in the new project.

Both correctly retain coding agents as the execution interface. A is leaner but its contracts cannot express its whole first journey and later capabilities remain skeletal. B is richer in people, meetings, evidence and community details, but adds an unnecessary distributed store, many first-milestone entities, and several inconsistent rules. Both need a concrete development-control and evidence layer.

This is a synthesis of judged strengths, not an average or a feature union. Assertions of “optimality” would require use evidence; these are the best-supported choices for this founder, platform and product boundary.

| Area | A | B | Final |
|---|---|---|---|
| Product boundary | Strong and small | Strong and detailed | Preserve shared boundary |
| Storage | JSONL + materialized files, unspecified atomic recovery | Per-writer segments, timestamp replay and multi-host conflict projection | Replace both with local transactional SQLite |
| Core scope | Small but missing write operations | Many subsystems before a useful loop | Thin complete core, then meeting/paper slice |
| Claims and evidence | Mostly entry-level | Scoped claims and immutable source versions | Keep B's distinctions, simplify record count |
| Meeting reconciliation | Weak dedup by title | Rich classification and context | Keep B's workflow; similarity proposes, never silently merges |
| Historical time | Implicit | Replays recorded timestamps as meeting-time truth | Distinguish recorded time, occurrence and knowledge coverage |
| Hub | Could be indefinitely deferred | Good contextual reading design | Explicit required hub checkpoint |
| Skills | Standard skills, simple manifest | Rich lifecycle, unverified plugin-standard dependency | Standard skills plus optional small recipe/metadata sidecar |
| Licensing | Apache core favors adoption | AGPL core with permissive interfaces | Recommend B's reciprocal core goal, without unsupported legal/personal assumptions |
| Autonomous delivery | M1-only brief | Large roadmap with external actions baked in | Technical graph + independent review + separate live/field/release gates |

## Settled decisions

**D01 — A local modular monolith.** Python core with one service boundary, CLI first, MCP adapter next, React hub. No agent loop, task-executing workflow engine, distributed service or simulator ownership. Amendment A02 extends this boundary only to bounded, declared literature pipelines: built-in operations with declared inputs, outputs, allowed operations, limits and review points. A pipeline is not a general agent loop and never runs arbitrary or model-generated code. External scientific runs are observed only: no simulation queue, HPC scheduling or automatic restart.

**D02 — Transactional authority.** SQLite holds records, revisions, source metadata, structured relationships, task snapshots, requests, jobs and content payloads. Small source content can live in SQLite; large retained files have explicit blob staging and recovery. JSON and Markdown are portable exports, not competing authorities. This revises earlier plain-file authority proposals deliberately: implementing crash-safe multi-object mutations and concurrent idempotency is database work. Readability remains through documented schema/export. SQLite does not prove the science.

**D03 — Local disk and one machine.** Multiple same-user processes may write through transactions. Default journal mode DELETE, synchronous FULL, short transactions; WAL is not necessary for the initial load. Network/cloud-sync directories and shared multi-machine authorities are unsupported. Research sources may be elsewhere and be unavailable. Moving a project uses verified backup/restore or portable export/import, not Git-merging a database. No hidden fallback to a server.

**D04 — Explicit historical identities.** A claim is independently addressable; notebook entries may exist without extracted claims. Source versions are immutable. A corrected statement gets an explicit relationship and rationale. Revisions distinguish what the system knew when from what occurred earlier.

**D05 — Attributed interpretation, no scientific adjudicator.** Agent reports can create claims, assessments and correction proposals. Deterministic checks establish schema/reference/quantity bindings only. Applied corrections and adopted commitments require the configured research decision authority; agent self-declared approval is not sufficient.

**D06 — Useful prose with structured grounding.** Models see relevant actual evidence. Precise values are rendered from quantity bindings; there is no blanket percentage tolerance or exemption for small numbers. Generated narrative remains interpretation; validators cannot prove semantic entailment.

**D07 — Windows first.** Windows 11 x64 and local NTFS are the initial actual-acceptance environment. Linux CI exercises portability. macOS and host integrations remain unverified until receipts exist. No fabricated platform support from mocks.

**D08 — Skills early, a marketplace unnecessary.** Ship original record/meeting skills at CP3. CP4 adds pinned import/create/adapt/export/update/revoke and agent-executed recipes. A Git index and local packages suffice; no custom portable-plugin standard dependency or hosted catalog required.

**D09 — Full experience, phased delivery.** CP5 delivers the hub regardless of whether terminal discomfort was measured. CP6 delivers audio/synthesis and a separate Windows live-capture capability. CP7 delivers recurring jobs and academic workflows. These remain acceptance tasks, not indefinitely deferred aspirations.

**D10 — Reuse at component level.** No wholesale fork of another agent/workbench. Prefer external scientific tools and selected licensed skills. Existing PrometheOS modules are optional read-only candidates; this new folder works without them. Preserving earlier grants/notices is mandatory when porting material.

**D11 — Reciprocal core, open interfaces.** Recommend AGPL-3.0-or-later for newly owned core/hub code and Apache-2.0 for independently authored schemas, skills and client helpers. DCO inbound/outbound by component. This permits competition and copying under terms; it does not protect the idea from reimplementation. No automatic future proprietary/source-available relicensing trigger.

**D12 — Honest verification.** Contracts, real hosts, real scientific tools, user field results and release rights have separate evidence. Autonomous engineering proceeds on ready tasks. Missing external inputs never become an invented pass or a reason to poll indefinitely.

## Specific defects resolved

- A's revision is an event count “or” entity revision: now entity integer revision and project sequence have distinct roles.
- A cannot create required sources/people/extractions; the final operation registry covers every primary record and the complete scenario.
- B's same-line batch and cross-host winner rules leave crash/causality gaps; multi-machine record merging is removed.
- Both need deletion beyond tombstones; audit metadata is separated from deletable payloads and retention propagates.
- B's restricted sources are inconsistently included in agent briefs; all agent/CLI/model/export egress uses one sensitivity policy.
- B treats arbitrary model `actor=researcher` or confirmation text as authority; confirmation uses an independently created local decision grant, with honest same-OS-user limits.
- B's read-only paper checks write events; final queries are pure except disposable cache updates, and saved reports are explicit mutations.
- B ambiguously falls back to repo licenses; ambiguity now blocks adaptation/distribution of that component, not all development.
- Both contain gates needing people, external messages or publication; those are separated from locally executable engineering.

## Amendment rule

Use an ADR stating problem, chosen change, impacted requirement/contract/check IDs, migration implications and evidence. Routine implementation details need only a brief note. No required test may be lowered retrospectively to pass a failed run; change the specification prospectively with an explicit rationale and retain the failed evidence.

## Amendment A01 — LaTeX workspace and Overleaf Git (spec 1.1.0, 2026-10-05)

Owner decision recorded in [decision 0008](../development/decisions/0008-owner-scope-amendment-latex-overleaf-audio.md). It changes the product boundary prospectively. Receipts issued under 1.0.0 remain bound to that version and are carried forward only through a reviewed amendment record.

- **A01.1** A LaTeX manuscript workspace (editor, local compile, PDF preview with SyncTeX, diagnostics, bibliography, evidence links) is in v1 as CP9. Other native editors remain excluded.
- **A01.2** Files on local disk remain the manuscript authority. Editor writes are atomic and refuse to overwrite a file changed since it was loaded. Compile inputs and outputs are recorded as immutable versions with provenance.
- **A01.3** Compile uses one pinned Tectonic release installed explicitly into the PrometheOS home. Compile runs with shell-escape disabled and bounded time and output, in a disposable staging copy. Package-bundle fetch is disclosed egress: it reveals which TeX packages, fonts and support files a document uses, never manuscript text, and an offline compile from a populated cache makes no request.
- **A01.4** Overleaf synchronisation uses Overleaf's Git integration with the owner's own credential store. Pull integrates only fast-forward or clean merges. Push is an explicit owner-approved action, never forced. Comments and tracked changes are not synchronised.
- **A01.5** E07 moves to CP9 and is required for the Overleaf claim. CP8 depends on CP9.

## Amendment A02 — Owned agentic research roadmap (spec 1.2.0, 2026-10-05)

The owner approved writing this amendment ([decision 0010](../development/decisions/0010-owner-plan-a02-orchestration-replay-fix.md)). The adopted scope, routine choices and evidence are in [decision 0011](../development/decisions/0011-a02-adoption.md). The amendment changes the product prospectively. Receipts issued under 1.1.0 stay bound to that version and are carried forward only through the reviewed amendment record. No check of CP0–CP7 or CP9 changes, so CP6 and CP9 keep their 1.1.0 criteria. CP8, which has not started, gains scope and three reworded checks (C08-REGRESSION, C08-RIGHTS, C08-REPORT).

- **A02.1 Parity rule.** The essential behaviour of every feature is reachable through MCP and the documented local HTTP API, through the same application services, validation and authority as the app. The app completes ordinary workflows without agent prompts or terminal commands. Parity means equivalent capabilities and outcomes, not one tool per button. This extends R07 without rewriting it (R20).
- **A02.2 Agents request, never self-authorise.** A protected operation returns a typed pending approval with the exact plan. The owner grants or denies it through a trusted route, and the agent then resumes. No agent route creates authority, reads a secret value or bypasses egress or recording rules (R21). This applies D05 to every new surface.
- **A02.3 One durable run contract.** App-owned long operations return a run ID at once, have truthful states and resume only on compatible inputs (R22). External scientific runs are registered and observed, never executed, queued or restarted by PrometheOS.
- **A02.4 Continuity, recovery and untrusted content.** Findings carry their objective and assumptions. Superseding a claim or an assumption is an authorised correction that flags dependents. Finished but uninterpreted results stay visible. Artifact availability and backup coverage are shown (R23). A project that cannot open is recovered into a new store without overwriting the damaged one (R24). Retrieved and generated text is data, enforced in code rather than by model detection (R25).
- **A02.5 Owned literature workflow.** A local library and a source-grounded evidence matrix are part of v1 (CP11, R26, R27). Discovery, screening and watches follow later (CP13, CP14; R31, R32). D01 is extended only to bounded, declared literature pipelines.
- **A02.6 Practical local AI.** A provider capability interface with a routing policy, a local model manager and CPU-first retrieval, classification, OCR and transcription (CP12; R28–R30). No silent remote fallback, automatic model download or hidden telemetry.
- **A02.7 Deliverables and connectors.** Meeting-to-deliverable recipes (CP15, R33) and selected connectors, local Zotero first (CP16, R34). Accounts are needed only for integrations the researcher chooses.
- **A02.8 Delivery.** CP10–CP16 are added. CP8 now depends on CP10 and CP11 and covers v1 plus CP10 and CP11. CP12–CP16 follow CP8. From CP10 on, every checkpoint has an end-to-end walk check on the actual app, repeated through MCP. New external gates E09–E14; E03 adds a literature session. Release profiles are in the product spec.
- **A02.9 Rights.** The rights inventory covers every shipped or installable package, executable, model, weight, tokenizer, language pack, template and other asset, at every checkpoint and at C08-RIGHTS. Zotero is used through its local API and never bundled.

## Amendment A05 — Integration doctrine, positioning, desktop application, adoption and work items (spec 1.5.0, 2026-10-14)

The owner decided the direction on 2026-10-12 and 2026-10-13. The adopted scope, routine choices and evidence are in [decision 0032](../development/decisions/0032-a05-adoption.md). The amendment changes the product prospectively. Receipts issued under 1.4.0 stay bound to that version and are carried forward only through the reviewed amendment record. No check of CP0–CP7, CP9 or CP10 changes.

**D13 — Integrate, don't rebuild.**
- PrometheOS integrates proven services and tools through their official API or MCP server, and builds only what integration cannot give as researcher-owned, verifiable records.
- **The three patterns:**
  - **A**, a direct connector in reviewed core code;
  - **B**, agent-mediated: the researcher's agent uses a vendor MCP server, and the outcome is captured as a versioned source with a provenance tier and a replayable call record;
  - **C**, an optional loopback sidecar such as Ollama.
- **Narrow exception.** PrometheOS acts as an MCP client only where MCP is a vendor's sole interface, reviewed per vendor.
- **Defaults.** Open-source and self-hosted by default; paid services optional; none required.

**D14 — The ledger beneath whatever AI you use.**
- PrometheOS owns the records (the nouns): sources and versions, claims and evidence, receipts, corrections, decisions, work items, deadlines and events.
- It leaves searching, reading, analysing, drafting and rendering (the verbs) to the agents and tools the researcher chooses.
- Agents propose; deterministic checks and the owner decide.
- (A07, D16) PrometheOS also brings a small, guarded AI of its own for typed proposals (classify, link, outline, word a card), which code verifies and the researcher can turn off. Apart from these typed proposals, the verbs stay with the researcher's agents and tools.
- Formats are open, and a full export re-imports into a fresh store.

- **A05.3 Desktop application and generic agent package (R37, CP19).**
  - **The app.** The hub ships as a Windows desktop application over the same loopback hub, with the core as a sidecar, a tray icon, notifications and a global capture hotkey. It adds no authority. (A07: the browser application is offered beside it; see A07.11.)
  - **The package.** The MCP server, skills and a session-start summary ship as a Claude Code plugin and as a configuration for at least one non-Claude MCP host. The MCP surface stays generic.
  - **Host configuration.** Installing into a host is an opt-in setup step.
  - **Technology.** A recorded spike chooses among Tauri 2 with the core as a sidecar, pywebview and Electron (C19-SPIKE).
- **A05.4 Work items, events and an agent-visible queue (R36, CP18).**
  - **Work items** are `action` records with an optional deadline, description and labels.
  - **Events.** Typed events from a registered catalogue are written in the same transaction as the change they report, and built-in detectors add events.
  - **The queue.** Agents read the queue when the owner opens a session or asks, and ask the owner before working on an item.
  - **No pickup by PrometheOS.** It never launches, schedules or wakes an agent. An agent never self-starts work because of a deadline or the queue.
- **A05.5 Generic project adoption (R35, CP17).**
  - **Mapping.** An existing working folder is mapped into a verified baseline without writing into it.
  - **What is fixed and what is data.** The fixed core covers inventory, evidence tiers, fusion, ranking, plan, baseline, re-adoption and export. Analyzers and adoption profiles are data.
  - **The plan.** The consolidation plan is typed work for the coding agent.
  - **Evaluation.** Synthetic corpora with golden labels and traps prove each check. E15 (the owner's real project, blind labels first) and E16 (a second real folder) are field gates.
- **A05.6 Order.**
  - **Dependencies:**
    - CP18 follows CP10;
    - CP19 follows CP18 (its package carries CP18's session-start summary; its notifications carry CP18's events);
    - CP17 follows CP18;
    - CP13 and CP15 follow CP11 instead of CP8;
    - CP8 follows CP0–CP7, CP9–CP11 and CP17–CP19.
  - **Unchanged:** CP12, CP14 and CP16 keep their dependencies.
  - **Text only:** CP11–CP16 are reworded under D13; no check criterion changes.
  - **Work order.** CP18, CP19 and CP17 are listed before CP11, so the first ready checkpoint follows the owner's milestone order (M1 before M2).
- **A05.8 CP8.**
  - C08-INSTALL covers the desktop installer and the package's opt-in setup.
  - C08-REGRESSION covers every accepted checkpoint in the candidate and a two-host smoke test.
  - New C08-EVAL runs an evaluation suite on recorded fixtures against targets recorded first; a new model is evaluated by an opt-in live run that is recorded and replayed.
  - E02 names Claude Code plus one non-Claude host through the generic package.

## Amendment A06.1 — The research map (spec 1.6.0, 2026-10-29)

The owner reviewed the draft on 2026-10-29 and chose its shape ([decision 0080](../development/decisions/0080-owner-a06-research-map-choices.md); the amendment's record is [decision 0081](../development/decisions/0081-a06-1-research-map.md), and the draft and evidence are in `development/spec-history/amendments/a06-draft-research-map.md` and the friction audit on the real project, `development/ux/friction-audit-kers.md`). A06 was reserved for the ownership scope (deliverables, meetings, the study ledger); the research map is its first part, and the study ledger and meetings build on its questions, claims and checks in a later A06 part, under checkpoint numbers after CP20. The amendment is prospective: receipts issued under 1.5.2 stay bound to that version.

**D15 — The record says what is still true and what comes next.** The ledger's records stay the truth; on top of them PrometheOS derives what holds, what is stale and what stands between a deliverable and its next step, and turns uncertainty into planned checks. The owner is asked only for value judgements, each with its context, a recommendation and what happens by default. Derived status is never scientific support: only the protected claim state (R21) is.

- **A06.1.1 Derived claim status (R38, CP20).** Every claim shows one derived status with all its causes, by precedence: withdrawn, superseded, stale, conflicting, settling, unverified, corrected, holds. `holds` and `corrected` name their basis (reviewed by the owner, a deterministic check, or reported by an agent and not yet reviewed). The status is a view flag, never a claim state; promotion and corrections keep their authority (R21).
- **A06.1.2 Record additions.** Assessment checks carry optional expected, measured and tolerance; quantity bindings an optional name and definition; claims optional bound quantities; an evidence reference can bind a whole artefact by digest; a claim has an optional one-line summary.
- **A06.1.3 Structure.** Entry category `track`; relations `answers` (claim to question), `addresses` (question to paper or to the objective entry) and `draws_on` (claim or question to track); deliverable stages derived from them and from the paper status.
- **A06.1.4 The verification loop.** Detectors propose verification actions with kind and estimated cost, ordered by cost. A standing owner budget admits **read-only checks only** (default at most 15 minutes each and 60 minutes a day). A **read-only check** re-reads, recomputes or compares from retained or referenced artefacts; it runs no simulation and no model, edits no model, script or parameter file, and writes only new result files inside the output folder the owner names in the budget (for the owner's project, `reports/`). A detector-proposed or owner-adopted read-only verification action inside the budget may be started by the coding agent without a per-item question. This is the one exception to A05.4's "agents ask before working on an item"; reruns, model edits, literature checks and every other work item are asked first. PrometheOS still never launches, schedules or wakes an agent and never runs a simulation. Owner questions are decision cards (what, why, recommendation, default). A default changes only derived views and generated drafts, never the ledger, a manuscript file or an anchor.
- **A06.1.5 Adoption that fits forking repositories.** Families get a class (variant, identical, tool output, run log, history, lineage, succession, compete); the owner decides only real competition and confirms the proposed outline once; baseline files are citable as reference-only versions. From CP20, this replaces "conflicts become work items" (spec/05) for classified families.
- **A06.1.6 Order and scope.** CP20 follows CP10, CP17 and CP18 and comes next in the work order, before CP8; it is not part of Full intended v1 and does not block CP8; its capabilities are reported individually until a release profile includes them. C17-PLAN and C18-QUEUE gain exceptions that apply only once CP20's classes and budget exist, so CP17 and CP18 evidence never needs CP20. The intelligence layer (a required AI provider, proposed separately as A07) is not part of this amendment.

## Amendment A07 — The intelligence layer and the web-first application (spec 1.7.0, 2026-10-30)

The owner asked on 2026-10-30 for a fast, guarded AI layer of PrometheOS's own and for an application that needs no reinstall per change, before the research map's remaining parts and before the real-project test (E19). The research, the design and the drafts are in `development/ai-layer/`; the owner's answers are [decision 0086](../development/decisions/0086-owner-answers-a07-direction.md) and the amendment's record is [decision 0087](../development/decisions/0087-a07-intelligence-layer-web-first.md). This amendment seals the intelligence layer (draft I1–I10) and part P (web-first) of the companion draft; that draft's map, facets and multi-folder adoption (M6–M8) remain a draft for a later amendment. The amendment is prospective: receipts issued under 1.6.0 stay bound to that version, and no accepted criterion is lowered.

**D16 — Models propose; code verifies.** PrometheOS may call small, fast models of its own for typed tasks (classify, link, outline, word a card), but a model's output is only ever a proposal. It is accepted as one only after deterministic checks (shape, offered options, verbatim quotes, digests, bound numbers, agreement), and no model output sets a status, a class, a baseline, a relation, a default, a promotion or a permission. The ledger, its authority rules and its derived statuses stay deterministic. Every feature that uses a model also works without one and says what it did instead.

- **A07.1 One intelligence layer (R39, CP22).** A module `intelligence` in the application layer, used only through service operations: a typed task registry, a gateway (the CP6.3 adapter generalised), deterministic validators, a result cache keyed by input digest with offline replay, call receipts, budgets and an evaluation harness. It is not an agent: no tools, no loop, no file access beyond the bundle it is given, and no effect on the record other than storing a validated proposal. A05.4 stands: PrometheOS never launches, schedules or wakes a coding agent, and the layer runs only inside an operation the owner or an agent started.
- **A07.2 Typed tasks with three executors.** Every task has a versioned input model, a strict output model with an `abstain` branch, a template digest, a validator and a declared effect. It runs on rules first, then on the gateway (inside the standing policy, A07.4) or on the coding agent (the CP17.4 prepare and submit path). All three pass the same validators. CP17.4's `agent_task` and `ai_task` become two executors of registered tasks, and the T4/T8 evidence rule is unchanged.
- **A07.3 Deterministic acceptance.** An output becomes a proposal only if:
  - it parses against the strict model;
  - every id, path, class and option is one the bundle offered;
  - every quote re-reads verbatim from the visible text of the exact bytes;
  - every named file still has the bundle's digest;
  - every number is a bound quantity referenced by id (a model never writes a number of its own);
  - `k` samples agree where the task needs agreement.

  Accepted proposals carry their provenance (task, version, template digest, model and provider served, receipt). The schema sent to a provider is a wire schema derived from the strict model in the subset that strict structured output accepts; the validator applies the strict model.
- **A07.4 Standing policy per task kind (amends R28).**
  - **What it names.** The owner may grant a standing policy that names: the task kinds that may use the remote route; the sensitivity ceiling (normal only); the evaluated models per task kind; the data policy; a per-run cap; and the home's monthly cap, a home setting counted in a home-level spend ledger with reservations under a home lock across all projects (a policy is refused while either cap is unset); and whether agents may start runs inside it. Inside it a task run needs no per-call approval.
  - **How it is granted.** Granting, changing or revoking it is an owner decision through the trusted owner route, bound to the policy's digest and to the provider configuration revision. Changing the endpoint, the key reference or any field voids it until it is granted again. Revocation stops new calls at once.
  - **Where it applies.** Only where the project's and the workflow's route permits remote use of that capability: from CP12, R28's routing policy; before CP12, the standing policy itself is the project's remote decision for its task kinds. It never overrides `local_only` or a project without AI.
  - **What never leaves.** A layer task whose input is restricted or private answers `policy_denied`. A file's sensitivity is the highest of its adoption root's and of any record citing it; a root reads as restricted until the owner sets it. Lowering a root's sensitivity is a protected owner decision bound to the root alias, the root's resolved folder and the value (an agent's request answers `approval_required`); pointing the alias at another folder returns it to restricted; raising it is not protected. There is no per-call path for it, and CP6.3's separate egress grant does not apply to layer tasks. Every other run outside the policy answers `approval_required` with the CP6.3 per-call preview, or `budget_exceeded`.
  - **What is recorded.** Every grant, change and call, each call with the digest of the input bundle it sent, which the owner can open.
- **A07.5 OpenRouter as the first remote route.** It is configured explicitly (no "OpenAI-compatible" claim). Every request with project content asks for zero-data-retention endpoints with data collection denied and parameters required, a strict JSON schema, the lowest reasoning effort the model allows, temperature 0 and a seed only where the model's recorded metadata lists them, and usage with cost. Fallbacks are limited to the task's evaluated (model, provider) pairs and run in the gateway, one call per model. The evaluated list of each task kind spans models from at least two companies, or the policy records why not. Router models, moving aliases and free tiers are refused for project content. The served model and provider are recorded; an output from a model outside the evaluated list is kept as a T8 hint only. The key is entered only through the owner's credential entry and held by reference (spec/08).
- **A07.6 Budgets in three layers.** The key's own limit with a monthly reset (set by the owner at the provider); a local monthly and per-run cap with reservations before each call; and a per-task ceiling. A batch reads the key's remaining limit before it starts and stops cleanly at any cap. Billed cost replaces the estimate when the response carries it.
- **A07.7 Evaluation before trust.**
  - **Fixed in advance.** Before the first measurement the evaluation record fixes, and the receipt binds by digest:
    - the golden set;
    - the metrics, precision per outcome and coverage (the share of items not abstained), with a target for each;
    - the confidence level and method: 95%, one-sided, Clopper–Pearson;
    - the minimum number of items;
    - for retrieval, recall@k with its k and a paired comparison with BM25 on the same queries.

    None of these changes after a run, and no target is ever lowered.
  - **Where trust comes from.** Trust is written only by the evaluation harness, from a measurement whose replies are bound by digest to the gateway's own call receipts. Synthetic or hand-made cassettes serve tests and replay and confer no trust. A trust change is shown to the owner.
  - **What trust changes, and nothing more.**
    - An untrusted task's outputs are hints shown with their source.
    - A trusted task's proposal may be shown as a decision card's recommended answer, and may be included in "apply all recommended" (CP20.3) only where the outcome is reversible and resolves no competition.
    - Moving a family out of `compete`, closing an owner work item, or any outcome a rule asks the owner about always needs the owner's explicit answer on that item. The owner's "apply all recommended" approval over a previewed, listed set is the owner's answer on each listed item; exits from `compete` and closures of work items are never listed.
    - A model's output may fill a card's `recommendation` and its generated wording, never `default_text` or `default_at`.
  - **Re-measurement.** Trust is per task version, template, wire schema and model. It is withdrawn when any of them, or the served provider set, changes.
  - **Replay.** Only stored replies are reproducible.
    - The call receipt retains the reply body, which inherits the highest sensitivity of its inputs.
    - On a cache hit the cache returns the stored proposal and creates no new one.
    - A new sample adds a proposal and never alters a recorded one.
- **A07.8 Local embeddings and hybrid retrieval for linking.** Retrieval for linking and proposing (claim to question, related files, grouping and reranking candidates) fuses BM25 with local CPU embeddings. It is measured against BM25 alone on labelled sets with the metrics of A07.7 fixed first, and its results are lists of record ids. D13's exclusion of a bespoke retrieval stack is narrowed to a general search engine or question-answering product, which stays excluded: grounded answers stay with the researcher's agent or tools.
- **A07.9 Degrade, never break.** The hub opens and every status derives with no key and no network (spec/06 unchanged). Provider failure ends a batch cleanly, keeps finished items, holds unknown outcomes without repeating them, and falls back to rules, the agent or the owner.
- **A07.10 On by default, never required.** Once the owner enters a key and grants the policy, the layer is on. Every model-assisted capability has a rules or agent path, and release profiles name which capabilities are model-assisted. D14 carries the matching note.
- **A07.11 The web-first application (R40, CP21; adds to A05.3 and R37).**
  - **The core as an application.** From CP21 the core also installs per user with `uv tool install` (a release wheel, or editable from the source checkout for development) and updates by an upgrade or a restart. The hub runs in the browser and installs as a PWA. Its service worker caches no record data, names its cache by the build digest and fetches pages network-first, so an upgrade never serves stale pages.
  - **One hub per home.** `prometheos start | stop | status` keeps one hub process per home.
    - **The lock** is OS-held and exclusive: a locked file handle or named mutex that the OS releases when the process dies. Its process id, port and start time are advisory metadata for `status` only, and no lock is ever broken on their evidence.
    - **Who takes it.** Every hub process: one started by `start`, by the CLI's hub command, or by a desktop shell built under CP21.
    - **Who does not.** MCP servers, CLI acts and job workers are not hubs. They keep D03's transactional multi-process writes under the A04 admission gate, so an agent writes while the hub runs.
    - **A second `start`** on a running hub starts no second hub process: it asks the running hub, over the home's owner channel (spec/08), for a fresh launch code and opens a paired browser on it.
    - **When `start` refuses.** It never moves port. It refuses when the home's fixed port is held by another program, and while a desktop build from before CP21 is running (detected by its process, its single-instance lock or its port), until that build is quit.
    - **Migrations.** A store migration is preceded by a verified backup.
  - **Pairing.** The one-use launch code travels only in a URL fragment, never a query. The page reads it, removes it with `history.replaceState` before any other request and posts it same-origin. It expires after 60 seconds and is never printed to the terminal. A successful pairing may set a remembered-device credential the owner can revoke, so sessions survive a core restart (spec/08).
  - **The desktop shell.** CP19's shell stays as built, and its acceptance stands.
    - The owner decides after the trial week (decision 0086, answers 2 and 3) whether it becomes an optional helper that loads the installed core, or whether a small tray helper replaces it. That work is a later task.
    - Until then the base install needs no shell.
    - The trial week is the owner's use, recorded as an observation, not a check.
  - **Release profile.** Full intended v1 keeps R37 and the CP19 installer, and C08-INSTALL is unchanged. R40 is reported individually until a later amendment, after the trial week, decides whether it replaces the desktop installer in a profile.
  - **The agent package** names the interpreter of the installed tool. It is regenerated only by the owner's setup command, and `status` reports when the installed package names a different interpreter.
- **A07.12 Order and scope.**
  - **Dependencies.** CP21 follows CP19, and CP22 follows CP6, CP10, CP17 and CP18. CP20 now also follows CP22, because its remaining parts use the layer.
  - **What CP22 is proved on.** CP22's checks use only CP17 and CP18 data: the CP17.4 family assist becomes a registered task. Class proposals for families the rules leave in `compete`, and the outline and producer proposals, are measured under CP20's C20-ADOPT.
  - **Work order.** Both are listed before CP20, so they come next (CP21 first).
  - **Fresh evidence.** Accepted receipts are bound to earlier versions, and the code has moved since. Before CP21 or CP22 is accepted, a recorded integration run on 1.7.0 re-evidences the checkpoints they rest on (CP0, CP5, CP6, CP10, CP17, CP18 and CP19). CP21 and CP22 may be developed before that run.
  - **Scope.** Neither is part of Full intended v1 or blocks CP8; their capabilities are reported individually until a release profile includes them.
  - **CP12.** CP22 builds part of CP12's code: the capability interface for generation and classification, the spend ledger, and the embeddings with hybrid retrieval. When CP12 starts it reruns its own C12 checks over that code. Its settings migration carries an existing standing policy forward as `remote_allowed` for exactly the granted task kinds and grants nothing new (C12-BUDGET).
  - **C08-EVAL** reads CP22's per-model evaluation records where present; its targets are its own.
  - **E20** (the owner's labelled real sample) and **E21** (a real model meeting the family-assist targets on gateway-recorded replies, with the owner's OK and a cap) are not required for the full product. A model that misses its targets leaves the task untrusted; it never blocks CP22.
