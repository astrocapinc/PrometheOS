# Delivery and independent acceptance

The dependency graph and check IDs in `../checkpoints.json` are canonical. Requirement mapping is `../requirements.json`; executable scenario intent is `../acceptance/scenarios.json`. They describe future work, not successful results. `../development/state.json` begins with every checkpoint unstarted.

## Working unit and estimates

Each task is a bounded implementation/review unit, normally one to three focused agent sessions. A session is not a time or cost guarantee. CP1 storage/recovery and CP6 audio are risk-heavy; split tasks further if needed while retaining their gates. Forecast after CP1 from measured throughput, rework and external availability. Do not promise a fixed completion date from document length.

Build thin vertical slices before breadth. No UI mock counts as a working slice; no backend green suite substitutes for the required hub. Use synthetic data before permissioned real research. Keep all local technical work autonomous within AGENTS; request only concrete missing input or externally consequential authorization. External gates can remain pending while independent engineering proceeds.

Order under A02: CP10 depends on CP5 and CP7, and CP11 on CP10. CP8 depends on CP0–CP7, CP9, CP10 and CP11, so the release candidate covers v1 plus CP10 and CP11. CP12, CP13, CP15 and CP16 each depend on CP8 and are independent slices after it; local generation is optional for literature work, so CP13 does not wait for CP12. CP14 depends on CP13, whose inquiries and adapters it watches. CP6 and CP9 continue in parallel under their unchanged 1.1.0 criteria. If CP13 or CP15 lands before CP12, their optional model-assisted steps run only through the CP6.3 adapter under its existing explicit configuration and egress preview, or return capability_unavailable; no CP13 or CP15 check needs a model. When CP12 brings those calls under its routing policy, its impact record lists the C13 and C15 checks that make model calls (and C06-AI if CP6.3 behaviour changes) as affected and reruns them.

Order under A05 (decision 0032): CP18 (work items, events and the queue) depends on CP10, and CP19 (desktop application and agent package) on CP18, because its package carries CP18's session-start summary and its notifications carry CP18's events. CP17 (adoption) depends on CP18, because its consolidation plan creates work items. CP18, CP19 and CP17 are listed before CP11 in `checkpoints.json`, so the first ready checkpoint follows the owner's milestone order. CP13 and CP15 now depend on CP11 instead of CP8, so deliverables and thin literature discovery can be accepted before the release candidate. CP8 depends on CP0–CP7, CP9–CP11 and CP17–CP19. CP12, CP14 and CP16 keep their dependencies. The owner's milestone order (M0 CP10, then A05, then M1: CP18, CP19, CP17; M2 CP11; then, under A06.1, CP20 the research map; then deliverables, study ledger, literature, meetings and CP8) is a work order inside these dependencies, not a further dependency; CP20 is listed before CP8 in `checkpoints.json` so it is the next ready checkpoint. Work proceeds in thin slices that the owner sees before the next slice starts; each checkpoint is proved on synthetic fixtures and small sample folders, and the owner's real project is a field gate (E15), not the proof of a checkpoint.

Order under A07 (decisions 0086 and 0087): CP21 (the web-first application) depends on CP19, and CP22 (the intelligence layer) on CP6, CP10, CP17 and CP18. CP20 now also depends on CP22, because its remaining parts use the layer. CP21 and CP22 are listed before CP20 in `checkpoints.json`, CP21 first, so the first ready checkpoint follows the owner's order (web-first, then the layer, then the research map's remaining parts). Neither is part of Full intended v1 or blocks CP8. Accepted receipts are bound to earlier versions and the code has moved since, so before CP21 or CP22 is accepted a recorded integration run on 1.7.0 re-evidences CP0, CP5, CP6, CP10, CP17, CP18 and CP19; CP21 and CP22 may be developed before that run.

## Checkpoint task cards

### CP0 — Development contract and runnable shell

Outcome: a fresh-folder project installs and has trustworthy development controls, without pretending CP1 exists.

- **CP0.1** Inspect environment; establish fresh Git repository only if absent, minimal Python packaging, lock, lint/type/test commands and `prometheos --help`/version. Record versions and dependency rights. No model/audio/frontend extras.
- **CP0.2** Implement common IDs, revisions, request/error models and schema export. Add `tools/check_contracts.py` to compare generated schemas, validate examples and reject unknown fields. Define the migration harness, not speculative future tables.
- **CP0.3** Implement receipt validation and state promotion from supplied schema. Verify required check coverage, artifact digests, candidate binding and independent review. Reject invented pass receipts and stale/missing evidence.
- **CP0.4** Run clean install/help/schema/tooling checks and independent review; record CP0 receipt. The bundled package validator checks design consistency only.

### CP1 — Durable capture and recovery

Outcome: create a project, ingest a source/note, retrieve it and restore it safely.

- **CP1.1** SQLite store, project registry, transactions, typed records, source/version/entry capture, metadata audit, canonical request hash and concurrency conflicts.
- **CP1.2** Retained/external source import plans and digest checks, body/media limits, safe paths, missing-source behavior. Use actual temp files.
- **CP1.3** Backup/restore, maintenance lock, migrations, orphan cleanup and process-kill probes before/after commit. Verify restored semantics in another directory and reference-only limitations.
- **CP1.4** CLI slice plus negative/concurrent tests; review code and receipts. No filesystem event replay or network merge.

### CP2 — Useful research memory

Outcome: a deterministic notebook/context survives a scoped correction and meeting reconciliation.

- **CP2.1** Claims, assessments, correction graph, exact quantity bindings and context coverage. Preserve historical source validity when current bytes change.
- **CP2.2** People/roles, typed meetings, items/actions, adoption and dependency transitions; local owner capabilities and no self-authorizing model fields.
- **CP2.3** Knowledge search/backlinks, as-known views, sensitivity filtering, generated-view invalidation and protected purge. Default no provider required.
- **CP2.4** Run the KERS-inspired synthetic journey including two conflicting scopes, late-entered meeting, correction, action and limited context; reviewer verifies actual content, not fixture-count claims.

### CP3 — First real agent integration

Outcome: an existing coding agent uses PrometheOS through a documented transport and original skills.

- **CP3.1** MCP SDK adapter over same services, task prepare/submit/cancel with snapshot/digest/stale controls, tool annotations and bounded outputs.
- **CP3.2** Three original skills and project-local host setup plan. No global config changes by default.
- **CP3.3** Adapter parity and untrusted-context tests; prepare a reproducible live host script that reads context, imports evidence, proposes correction and resumes from it.
- **CP3.4** Run live host gate E01 when available. Technical acceptance may proceed with E01 pending, but no “agent integrated” release claim yet.

### CP4 — Portable community procedures

Outcome: create/import/review/install/update/remove/export skills and render recipes safely.

- **CP4.1** Sidecar/lock schemas, local catalog, package review plans, per-file licenses and provenance. Unknown rights block distributable export.
- **CP4.2** Project-local installation/rollback/update conflicts, bounded archive extraction and manifest-owned removal. No install-time scripts.
- **CP4.3** Recipe inputs/outputs and recorded progress; malicious package tests and second host projection. Run E02 when available.

### CP5 — Required researcher hub

Outcome: actual installed application presents the four research surfaces coherently.

- **CP5.1** FastAPI service, pairing/session/CSRF/Host controls, typed HTTP API and bounded changes long-poll. Test unauthorized requests.
- **CP5.2** Now/Notebook/Knowledge/Papers shell with data from core; source context panel, meeting adoption and correction preview/commit path. Paper shell can show evidence relationships before CP7's full workflow.
- **CP5.3** Keyboard/error/empty/offline states, safe viewers, packaged assets, response targets on recorded Windows hardware. Browser QA exercises real mutations and store results.
- **CP5.4** Gather early researcher feedback without waiting for all CP6/7; fix blocking comprehension defects. Final E03 remains a full-workflow gate.

### CP6 — Audio capture and optional generation

Outcome: recording/transcription/proposal extraction work without making models mandatory.

- **CP6.1** Bounded Windows microphone+loopback feasibility spike; choose/pin capture and decoder dependencies based on actual evidence. Record live E04 or explicit blocker; never silently redefine recording as file import.
- **CP6.2** Recoverable recording segments, imports, local optional transcription, timestamp locators, device loss/cancel behavior and E05 sample validation.
- **CP6.3** One provider adapter, egress preview, secret storage, budgets/timeouts, strict structured output, one repair maximum and model-free fallback. Fake transport tests plus opt-in live call if credentials authorized.
- **CP6.4** Complete meeting reconcile UI and audio accessibility; run canary privacy/leak and outage tests. Unavailable paid provider does not block the external-agent/model-free product path.

### CP7 — Papers and recurring findings

Outcome: research records support a real manuscript workflow and bounded recurring checks.

- **CP7.1** Paper/anchors, local manuscript and PDF preview, source-derived venue requirements, review rounds and pure paper checks. Register outputs from external compile tools.
- **CP7.2** Local submission bundle and Overleaf/local Git handoff plan with conflict/preservation limits. Live cloud writes moved to CP9 under A01 (E07); never an automatic requirement to connect accounts.
- **CP7.3** Fixed job handlers, schedule slots/DST/catch-up, leases, cancellation, budget and unknown-side-effect handling. Meaningful local notifications only.
- **CP7.4** Run the paper correction-to-review journey and real MATLAB evidence gate E06 when available. No scientific validity from fixture success.

### CP9 — LaTeX manuscript workspace and Overleaf Git (A01)

Outcome: the researcher writes and compiles a LaTeX manuscript in the hub, linked to evidence, and synchronises it with Overleaf without losing either side's work.

- **CP9.1** Verify and pin Tectonic (version, license, bundle terms, Windows binary digest, actual network fetch behaviour); `tex engine install/status`; staged compile with shell-escape disabled, timeout and output limits; diagnostics parsed to file and line; PDF, log and SyncTeX registered as generated versions with input provenance. Hostile LaTeX fixtures.
- **CP9.2** Workspace binding to a local folder; bounded file listing and reads; base-digest-checked atomic writes; explicit snapshots; anchor remap on saved changes; completion data (labels, references, BibTeX keys) and outline from a pure query.
- **CP9.3** Hub workspace route: CodeMirror 6 editor, file tree, outline, completion, find/replace, diagnostics panel, PDF.js preview with forward and inverse SyncTeX, anchor/evidence gutter, insert citation or figure from evidence, unsaved-draft and external-change states, keyboard access. Browser tests use the real application and assert stored results.
- **CP9.4** Overleaf Git connector against a local bare repository in tests: link, status, pull with preview, fast-forward and clean merge, divergence stop, exact-plan push, credential-URL rejection, timeouts. Run E07 on an owner sandbox project when authorised.

### CP10 — Agent parity and research continuity (A02)

Outcome: whatever the app can do, a coding agent can do through MCP and the local HTTP API with the same authority; long work and finished results survive a closed chat; a project that will not open can be recovered.

- **CP10.1** Concept mapping and contracts: confirm the RP-00 mapping (spec/03) in a decision note. Add the CP10 contract changes and forward migrations (operation runs, approval requests, backup inventory, `execution`, the assumption category, claim objective, new error codes) with migration tests from the previous schema. Build the operation registry and capability discovery, with every transport generated from or checked against the registry.
- **CP10.2** Authority and untrusted content: typed `approval_required`, stored approval requests, the owner review queue in the hub and `decision review`, exact-plan grants held by the server, resume, deny, expiry and withdrawal, and credential references only. Enforce in code that imported, retrieved and generated text cannot change instructions, call tools, widen egress or create authority, with hostile PDF, abstract, page and model-output fixtures.
- **CP10.3** Durable operation runs: run table and states. Long operations on main (imports and, once merged, transcription and compile) return run IDs. Resume compatibility, budget pause, unknown external outcomes, cancellation semantics, and kill/restart probes between stages.
- **CP10.4** Research continuity: claim objective and assumption links; supersession flags dependents; observe-only `execution` registration with reported status; the awaiting-interpretation queue; artifact availability and backup coverage; Now shows changed, due, running, awaiting-interpretation and approval items with reasons.
- **CP10.5** Recovery: the maintenance service and an MCP/HTTP maintenance mode that open no project database; the hub recovery screen; registry quarantine and replacement records; maintenance approvals held in the per-user registry and granted on the recovery screen or in an interactive owner session; disposable corrupt-store tests. Under A04 (decision 0025) source admission is guarded on Windows (spec/04); the interim POSIX refusal is recorded as an unresolved platform limitation, not a Linux claim.
- **CP10.6** Parity completion and walk: close the inventory across every feature family on main, including administration (backup, restore, migrate, doctor, purge plans, decisions as requests, meetings, actions and agendas, skills, workflows, jobs, papers) and, once merged, audio and the LaTeX workspace. Run C10-WALK, prepare the E09 session script and obtain independent review of the walk transcript. Run E09 when the owner can supervise a real host session; it may share the session with E01.

### CP11 — Local literature library (A02)

Outcome: a researcher organises, searches and cites literature locally with no account, from the app or an agent, and every proposed or reviewed matrix cell points to a page and quotation.

- **CP11.1** Library import: `work` records; BibTeX, RIS and CSV import plans with retained original bytes; DOI, arXiv and PubMed normalisation; per-field provenance; export; synthetic bibliography and PDF fixtures, including malformed and hostile files. Choose and pin parsing and extraction libraries after licence review.
- **CP11.2** Dedup, versions and full text: deterministic identifier duplicates, reviewed similarity candidates, preprint and version relations, idempotent re-import; PDF attachment import and local text extraction with page and span locators, run as durable operation runs; FTS5 search; annotation entries.
- **CP11.3** Evidence matrix and Zotero: `matrix` and `extraction` records; grounding validation of IDs, locators and quote digests; access states; correction flags that reach rows and generated briefs; the read-only local Zotero adapter with a capability probe, tested against a stub of its documented local API; Knowledge literature views; MCP and HTTP parity.
- **CP11.D13** Under A05 (D13) the read-only Zotero adapter is the expected default for researchers who keep their library in Zotero; the local import paths stay for those who do not, and the core still needs no account.
- **CP11.4** No-account offline check, the walk (the proposal's vertical slice) and independent review. Run E10 against the owner's Zotero library once the owner enables Zotero's local API.

### CP18 — Work items, research events and the agent-visible queue (A05)

Outcome: the researcher keeps tasks and assignments with optional deadlines in PrometheOS, things that need attention become typed events, and a coding agent sees both when the researcher opens a session or asks, then asks before doing anything.

- **CP18.1** Work items: the optional `description` and `labels[]` on `action` (spec/03), the hub views to add and edit work items with deadlines, and CLI, MCP and HTTP parity; read access for agents; the session-start summary (a skill or hook, owned and tested here; CP19 packages it for each host) that lists open items and asks the owner whether to work on one. The agent never self-starts or insists because of a deadline: the summary and tool descriptions say so and contain no start directive or urgency escalation, and nothing in PrometheOS starts an agent.
- **CP18.2** Events: the transactional outbox, the typed event catalogue and acknowledgement; the built-in detectors (deadline approaching at 14, 7 and 2 days, approval requested, awaiting interpretation, correction flagged dependents, backup gap, source unavailable, external outcome unknown), each firing once per condition across restarts.
- **CP18.3** The agent-visible queue: one read operation over open work items and unacknowledged events with reasons and input size; acknowledgement and dismissal; proof that PrometheOS launches, schedules and wakes no agent.
- **CP18.4** The walk and independent review. Start E18 (two weeks of the owner's real use).

### CP19 — Desktop application and generic agent package (A05)

Outcome: PrometheOS installs and runs as a desktop application, and any MCP-capable agent connects to it through one generic package.

- **CP19.1** Technology spike recorded as a decision (C19-SPIKE), with measured values and a minimal prototype per candidate: Tauri 2 with the Python core as a sidecar, pywebview and Electron, compared on a per-user Windows installer, tray, notifications, a global hotkey, footprint, licence (D11), packaging the Python 3.14 core, update path and signing needs. Register the chosen framework (spec/09).
- **CP19.2** Shell: window on the loopback hub, sidecar start and stop with health checks, tray state, notifications for events, global capture hotkey, single instance, pairing without a new authority path; quit and restart keep state.
- **CP19.3** Agent package: the MCP server, skills and session-start summary as a Claude Code plugin and as a documented configuration for at least one non-Claude MCP host (Codex CLI, OpenCode or Gemini CLI); an opt-in setup that writes host configuration only when the owner runs it; scripted MCP sessions for each supported configuration.
- **CP19.4** The walk and independent review. Real-host evidence remains E01 and E02, with the owner present.

### CP17 — Generic project adoption (A05)

Outcome: an existing working folder becomes a verified baseline in the record, without PrometheOS writing into it, and stays current as the folder changes.

- **CP17.1** Contracts, inventory and the seeded synthetic corpus generator with golden labels and planted traps; small sample folders as additional fixtures.
- **CP17.2** Deterministic analyzer pack, including one permissively licensed MATLAB/Simulink extraction module (tree-sitter-matlab for `.m`, standard-library ZIP/XML for `.slx`, a text parser for `.mdl`) designed so it can later serve as a Graphify extractor, including `.m` disambiguation between Objective-C and MATLAB; existing Graphify output read only as optional input, with a staleness check. Verify whether Graphify's SCIP-style JSON ingester is a usable no-fork bridge in its current release. Upstream contribution needs owner approval.
- **CP17.3** Fusion and ranking: evidence tiers T0–T8, contradictions kept, families, ranked current members with reasons.
- **CP17.4** Agent and AI analyzers: `agent_task` results through the coding agent's own tools, `ai_task` results with re-read quotes, offline replay.
- **CP17.5** The consolidation plan as work items (CP18), its skill and hub views; baseline acceptance.
- **CP17.6** Incremental re-adoption, vault import and one-way fenced export, the walk and independent review. E15 (the owner's real project, blind labels first) runs last in M1; E16 needs a second real folder of another kind.

### CP21 — The web-first application (A07)

Outcome: PrometheOS runs from a per-user install of the core with the hub in the browser, a change reaches the owner by a restart instead of a reinstall, and one hub process runs per home while agents keep writing through the transactional store. CP21 follows CP19 and is the next checkpoint in the work order.

- **CP21.1** The install and the launcher: `uv tool install` (a release wheel, or editable from the source checkout); `prometheos start | stop | status`; the OS-held instance lock taken by every hub process, with advisory metadata only; MCP, CLI and worker writes unchanged under D03; refusal on a taken port and while a desktop build from before CP21 runs; a verified backup before a store migration; `status` reporting a stale agent package, which only the owner's setup command regenerates (C21-LAUNCH).
- **CP21.2** Pairing: the home's own host name, the launch code in the URL fragment, the remembered-device credential with its binding, rotation, expiry and revocation from the hub and the CLI, and hostile Host, Origin, port and replay tests (C21-PAIRING).
- **CP21.3** The PWA: a manifest and a service worker that caches no record data, names its cache by the build digest and fetches pages network-first; installability checked through the browser's own installability report; the CSP additions (C21-LAUNCH). The owner's trial week beside the installed desktop build is an observation, not a check. Reducing the desktop shell to an optional helper, or replacing it with a tray helper, is a later task that follows the owner's decision after the trial week.
- **CP21.4** The walk and independent review (C21-WALK).

### CP22 — The intelligence layer (A07)

Outcome: small, fast models do typed work that rules cannot settle, under deterministic validators, the owner's standing policy and spend caps, and everything still works without them. CP22 follows CP6, CP10, CP17 and CP18; CP20's remaining parts follow it.

- **CP22.1** Gateway and policy: the OpenRouter profile on the CP6.3 adapter; per-model request profiles from stored model metadata; the request defaults (zero data retention, data collection denied, parameters required, strict schema); fallback only inside the evaluated pairs; the served model and provider recorded; the three-layer budget; the standing policy and its revocation; the credential by reference. Fake-transport tests cover 402, 429, a timeout, an unknown outcome, a served model outside the list and a response without cost (C22-GATEWAY). A recorded spike, with the owner's OK and a spend cap, measures latency, cost and structured-output validity of the candidate models on synthetic input; it is evidence for the model choice, never a required check.
- **CP22.2** Tasks, validators and the cache: the registry, the wire schema derived from each strict model, the validators of A07.3, `ai_results`, and offline replay from cassettes. Targeted mutation tests show that a wrong quote, a changed digest, an unbound number, an unoffered id and a disagreement are each rejected (C22-TASKS).
- **CP22.3** The evaluation harness: golden sets and targets recorded first; `tools/ai_eval.py` on cassettes by default and live only with the owner's OK and a cap; trust per task version, template, wire schema and model on a lower confidence bound (C22-EVAL).
- **CP22.4** First task in use: the CP17.4 family assist as a registered task on all three executors, exercised on CP17's seeded corpora with recorded synthetic cassettes or a deterministic fake model, and shown in the hub with its source tag and fallback line (C22-USE). Whether a real model meets the task's targets is gate E21, run with the owner's OK and a cap. Class proposals for families the rules leave competing, and the outline and producer proposals, are CP20.4 work measured under C20-ADOPT.
- **CP22.5** Retrieval for linking: local embeddings fused with BM25 (and an optional local reranker), measured against BM25 alone by recall@k in a paired comparison fixed first, on a pre-installed model whose digest the receipt records; unit and walk tests use a fixture embedder; related-record proposals as record ids (C22-RETRIEVAL).
- **CP22.6** The walk with no key (everything works and says what it did instead) and with a key on cassettes, and independent review (C22-WALK). E20 is the owner's labelled real sample; E21 is a real model meeting the family-assist targets.

### CP20 — The research map (A06.1)

Outcome: the record shows what is still true and what comes next: each claim's derived status under its question and deliverable, planned checks for anything uncertain, decision cards for value judgements only, and adoption that confirms an outline instead of deciding every family. CP20 follows CP10, CP17 and CP18, and under A07 also CP22; it comes after CP21 and CP22 in the work order and does not block CP8.

- **CP20.1** Producer edges; derived claim status with causes, precedence and basis (holds, corrected, settling, stale, conflicting, unverified, withdrawn, superseded); quantity `name` and `definition`, claim `quantities[]`, check fields expected, measured and tolerance, the whole-artefact `file` locator and the claim `summary`; Papers never offers a stale, superseded or withdrawn claim as usable (C20-STATUS).
- **CP20.2** Entry category `track`, relations `answers`, `addresses` and `draws_on`, derived deliverable stages, the project landing on Now and the paper page with questions and claims (C20-STRUCTURE).
- **CP20.3** Verification actions proposed by the detectors, the owner's read-only verification budget and its enforcement at the agent's start transition, decision cards whose defaults never change the ledger, and "apply all recommended" as one atomic approval (C20-VERIFY).
- **CP20.4** Adoption family classes and their evaluation, `classify` decisions recorded in the baseline, owner work only for real competition, the outline proposal and its one confirmation, and baseline files citable as reference-only versions (C20-ADOPT). Under A07 the class proposals for families the rules leave in `compete`, and the outline and producer proposals, are intelligence-layer tasks (CP22), with the coding agent as their executor when there is no key; C20-ADOPT measures them, and no proposal moves a family out of `compete` without the owner's answer on that family.
- **CP20.5** The walk on synthetic fixtures shaped like a forking research repository (per-track forks, a stale run, a definitional pair, a corrected number) and independent review (C20-WALK). E19 is the owner's real project.

### CP8 — Release candidate and field acceptance

Outcome: a reproducible candidate of Full intended v1 (spec/01: R01–R27 and R35–R37, that is v1 plus CP10, CP11 and CP17–CP19) with honest support claims and real researcher evidence. Under A02, CP8 follows CP10 and CP11; under A05 also CP17–CP19.

- **CP8.1** Fresh Windows wheel install with built hub, upgrade/backup/restore/uninstall regression, Linux CI, dependencies/notices and documented supported versions. Under A04 the Linux part first requires the repair stated in spec/08: a safe POSIX source admission with genuine Linux evidence. The interim POSIX `unsupported` refusal cannot satisfy C08-INSTALL, and no CP8 check is waived or narrowed.
- **CP8.2** Full critical integration/browser/security regression on the candidate, including the parity inventory and a replay of the walks of every accepted checkpoint in the candidate; manual keyboard check and failure recovery. Bind receipts to exact candidate content.
- **CP8.3** E03 researcher trial across four real work sessions: return/orient (including resuming after a closed chat), meeting/action, correction/result/paper, and literature (import, search, matrix). Record user observations, avoided searches, correction effort, baseline friction and counterexamples; repair material failures and rerun affected gates.
- **CP8.A05** Under A05: C08-INSTALL covers the desktop installer and the agent package's opt-in setup; C08-REGRESSION covers every accepted checkpoint in the candidate (including CP13 and CP15 if they were accepted first) and a two-host smoke test; C08-EVAL runs the evaluation suite (grounding precision, lineage accuracy against an accepted synthetic baseline, event triage, proposal acceptance, input size and cost) on recorded fixtures and stores the result per model.
- **CP8.4** Produce release readiness report, known limitations, user guide and next backlog; the capability matrix names the release profile of each claim. Owner separately decides public licensing/publication (E08). Do not publish, send manuscripts or mark unavailable live gates passed.

### CP12 — CPU-first local AI (A02)

Outcome: useful local search, classification, OCR and transcription on an ordinary CPU-only PC, with models managed from the app or an agent and remote AI used only where the researcher allows it.

- **CP12.1** Provider capability interface (generation, embedding, transcription, classification) generalising the CP6.3 adapter; routing policy per project and workflow, with a settings migration that keeps an existing CP6.3 configuration as remote_allowed for that provider's generation only and sets every other route local_only; spend reservations, usage ledger and limits; no silent fallback. The impact record lists C06-AI and the C13 and C15 checks with model calls as affected when their behaviour changes.
- **CP12.2** Model manager: per-user registry; preview, plan, install, resume, offline import, verify, load, unload, remove and benchmark through app, CLI, MCP and HTTP; a register entry for every offered model, weight, tokenizer and language pack; one heavy job at a time under a memory ceiling.
- **CP12.3** CPU-first intelligence: record labelled question sets and quantitative targets before measuring; small chunked embeddings and hybrid retrieval against the BM25 baseline; a TF-IDF/linear classifier with abstention on reviewed labels; page-selective OCR as derived versions; faster-whisper behind the capability interface; compact generation optional and off by default.
- **CP12.4** Offline verification with the network blocked, the walk and independent review. Run E11 on the owner's named hardware.
- **CP12.D13** Under A05 (D13) local text generation is reached through a separately installed runtime such as Ollama as a loopback sidecar (pattern C) rather than a generation runtime shipped by PrometheOS; embeddings, classification, OCR and transcription stay as CP12.3 and the C12 checks specify. Work the coding agent does goes through the CP18 queue, not a separate AI task queue.
- **CP12.A07** CP22 builds part of this checkpoint's code: the capability interface for generation and classification (as the gateway), the usage and spend ledger, and the embeddings with hybrid retrieval. CP12 reruns its own C12 checks over that code when it starts. Its settings migration carries an existing CP22 standing policy forward as remote_allowed for exactly the granted task kinds and grants nothing new (C12-BUDGET).

### CP13 — Literature discovery and review (A02)

Outcome: reproducible literature searches, screening and cited synthesis on free public APIs, with visible coverage and no required account.

- **CP13.1** Inquiries and discovery: `inquiry` and `search` records; adapters for the selected public APIs with metadata for quota, terms, contact requirement and retired endpoints; opt-in per project or workflow; a plan preview that also names the services receiving the owner's contact email; recorded-transport tests; the contact email and its owner-approved service list (initially Crossref and Unpaywall) as a per-user setting.
- **CP13.2** Screening: `screening` records from rules, lexical ranking, the local classifier when CP12 is accepted, or bounded model calls (through the routing policy once CP12 is accepted, before that only through the configured CP6.3 adapter); proposed and confirmed labels; uncertain and insufficient-information outcomes; systematic-review mode with protocol, recall checks and stopping rule; the declared pipeline format limited to designated literature operations (spec/07).
- **CP13.3** Full text and synthesis: open-access location lookup and researcher-supplied documents; extraction into the CP11 matrix; cited synthesis validation; linking reviewed findings to claims without overwriting decisions; exports.
- **CP13.4** The walk (K5, K7) on recorded responses and independent review. Run E12 on a real topic once the owner authorises live discovery.
- **CP13.D13** Under A05 CP13 depends on CP11, not CP8, and stays thin: direct adapters (pattern A) to OpenAlex, Crossref, arXiv and Semantic Scholar where reproducibility matters, and searches an agent makes through vendor tools captured as versioned sources with a replayable call record (pattern B). PrometheOS builds no search or ranking engine.

### CP14 — Literature watches (A02)

Outcome: literature checks arrive when they matter, tied to a question or claim, instead of in bursts.

- **CP14.1** A `literature_watch` handler on CP7 schedules and CP10 runs: cursor state, new or changed items only, relevance notification, quota pause and resume without recharging, no duplicate actions.
- **CP14.2** The walk and independent review.
- **CP14.D13** Under A05 watches stay thin; ingesting alerts from tools the researcher already uses is preferred to polling, and a watch hit becomes a CP18 event.

### CP15 — Meeting-to-deliverable recipes (A02)

Outcome: agendas, briefs, slides and responses come from the same reviewed evidence instead of being rebuilt by hand.

- **CP15.1** Deliverable recipe format, inert templates and a logic-less renderer; rendering provenance (evidence versions, template version); outputs as ordinary editable files registered as generated sources.
- **CP15.2** Recipes for the advisor agenda, short brief, claim and do-not-claim card, slides, speaker notes and reviewer response; exclusion or flags for superseded and unreviewed evidence; stale marking after corrections; optional labelled model drafting through the routing policy once CP12 is accepted, and before that only through the configured CP6.3 adapter.
- **CP15.3** The walk (K6) and independent review.
- **CP15.D13** Under A05 CP15 depends on CP11, not CP8. PrometheOS owns the recipe, provenance and staleness; rendering goes to existing engines (Marp, Pandoc, Tectonic). Proprietary document skills may be named, never copied.

### CP16 — Selected connectors (A02)

Outcome: the researcher's existing library and calendar work with PrometheOS without becoming required.

- **CP16.1** Connector framework: connection records, credential references, field ownership, cursors, tombstones, dedup, conflict surfacing and disconnect; connector content handled as untrusted.
- **CP16.2** Local Zotero connector with incremental sync and attachments by reference; write-back only through an interface verified here, otherwise stated as read-only. Then the optional calendar connector, with OAuth in the provider's own interface, incremental sync tokens and quota metadata.
- **CP16.3** The walk on local stub services and independent review. Run E13 on the owner's Zotero library, and E14 if the owner connects a calendar.

## End-to-end walks (A02)

From CP10 on, every checkpoint has a required walk check (C10-WALK to C22-WALK). After its other checks pass, the checkpoint's scenario runs on the actual built app against a fresh store created from synthetic fixtures, with browser steps that assert stored state. It is then repeated on a second fresh store through MCP, using the official SDK client with the hub stopped. The normalised outcomes of the two stores (records, states, relations, flags and authority results, with IDs and timestamps normalised) must be equivalent. The walk saves a transcript listing each step, the operation called, the response digest and the store assertion, and the independent reviewer reads it with the evidence before accepting. CP8 has no separate walk check: C08-REGRESSION replays the walks of every accepted checkpoint in the candidate (at least CP10, CP11 and CP17–CP19) on the release candidate, and E03 is its field walk. C19-WALK drives the desktop window instead of a browser tab and asserts the same store state. C21-WALK starts the hub with `prometheos start` and continues after a core restart; C22-WALK runs with no key and with recorded cassettes, and makes no model call. Steps that need the owner, real hardware, a real account or a real coding host are external gates and are never role-played.

KERS-inspired scenarios used by the walks (roadmap proposal, section 12):

- **K1** Find the best currently supported result with its comparator, scope, source revision and limitations.
- **K2** Apply an authorised correction and flag dependent brief and manuscript uses.
- **K3** Surface a finished external run whose output has not been interpreted.
- **K4** Resume after a chat closes without reconstructing a long prose handoff.
- **K5** Produce a literature comparison while preserving abstract-only and missing-full-text states.
- **K6** Prepare a concise supervisor agenda and brief from reviewed evidence.
- **K7** Reach a provider quota and continue viewing, searching and organising existing work.
- **K8** Complete the same workflow from the hub and through a real coding agent with equivalent effects and authority. Every walk covers its technical part; the real-agent part is E09.

## Standing invariants (A02)

Parity (C10-PARITY), the run contract (C10-RUNS) and the untrusted-content boundary (C10-AUTH) are properties of the whole candidate, not only of the code that existed when CP10 passed. A later change that adds an operation, a long-running job or a reader of outside text lists these checks as affected in its impact record and reruns them. CP6 and CP9 proceed under their unchanged 1.1.0 criteria. Their operations come under these invariants when they merge with or after CP10: whichever lands second adds the missing registry entries, routes and run handling. CP8's full regression is the last point at which a gap can be closed. The parity inventory test runs in both directions (spec/04), and its reverse direction is what catches CP6, CP9 or any later work that adds a CLI command, MCP tool or HTTP route outside the registry: an entry point that maps to no registry operation and is not on the reviewed exemption list fails the test. Because the test belongs to the full suite, such a gap also fails the full-suite run that C00-BOOT includes.

## Rights inventory at every checkpoint (A02)

Each checkpoint from CP10 on registers every new dependency and asset before use (spec/09), and its independent review checks the register against what the checkpoint and its walk used. CP6 and CP9 keep the 1.1.0 register rules, and C08-RIGHTS checks their entries at release. Unknown rights block distribution or an install offer; they never become a pass.

## Proposal acceptance IDs (A02)

The roadmap proposal's acceptance IDs map to formal checks and gates:

| Proposal ID | Formal check or gate |
|---|---|
| RPA-PARITY | C10-PARITY |
| RPA-HEADLESS | E09; the MCP half of C10-WALK is its technical rehearsal |
| RPA-APP | C10-WALK (browser state assertions) and E03 (real observation) |
| RPA-AUTH | C10-AUTH, re-tested by C11-IMPORT, C13-SCREEN and C16-SYNC |
| RPA-NOACCOUNT | C11-NOACCOUNT |
| RPA-OFFLINE | C12-OFFLINE and C13-SEARCH; the live run is part of E11 |
| RPA-LIT | C11-IMPORT, C11-DEDUP, C13-SEARCH, C13-SCREEN |
| RPA-GROUNDING | C11-GROUNDING, C13-SYNTH |
| RPA-LOCAL | E11, with C12-RETRIEVAL for quality against recorded targets |
| RPA-RECOVERY | C10-RUNS, C10-RECOVERY |
| RPA-BUDGET | C12-BUDGET, C14-WATCH |
| RPA-SYNC | C16-SYNC; the live parts are E13 and E14 |
| RPA-RIGHTS | C08-RIGHTS, C12-MODELS and the per-checkpoint register rule |
| RPA-FIELD | E03, expanded with return-to-project and literature sessions |

## External gates and product claims

| ID | Required evidence | Claim it gates |
|---|---|---|
| E01 | One real installed coding host, version/config, actual tools called, before/after records and retained transcript with sensitive data redacted | First-host integration |
| E02 | Second independently implemented coding host runs same evidence/context contract and original skill; under A05, Claude Code plus at least one non-Claude MCP host (Codex CLI, OpenCode or Gemini CLI) through the generic agent package, with the owner present | Multi-agent-host interoperability |
| E03 | Researcher performs four actual sessions (return/orient including resuming after a closed chat, meeting/action, correction/result/paper, and literature; the literature session added by A02) and confirms orientation, traceability and actionable next work; record avoided searches, correction effort, failures, counterexamples and baseline comparison | Usefulness/product v1 acceptance |
| E04 | Actual Windows microphone and system-loopback recording, playback verification, device-loss recovery | Online-meeting recording |
| E05 | Real permissioned audio import/transcription, sample span verification and correction of an error | Audio workflow |
| E06 | Actual MATLAB/Simulink work by external agent, retained artifacts/receipt, scope and notebook traceability reviewed | MATLAB/Simulink research support |
| E07 | Owner sandbox Overleaf project roundtrip through Git with explicit authorization: pull, edit, compile, approved push, conflicting remote edit stops with explanation; no claim of comment sync (A01, CP9) | Overleaf synchronisation |
| E08 | Owner records licensing/publication decision after concrete release inventory | Public publication only |
| E09 | With the hub closed, a real installed coding host (version and configuration recorded) completes the CP10 walk through MCP: retrieves artifacts, handles a revision conflict, requests an approval that the owner grants through the trusted route and then resumes, and resumes a run after the chat closes. Transcript retained with sensitive data redacted. It may share a session with E01 but is classified separately (A02, CP10) | Agent parity |
| E10 | The owner enables Zotero's local API; PrometheOS reads the owner's real library (installed Zotero version recorded), imports selected items read-only and leaves the library unchanged (A02, CP11) | Local Zotero import |
| E11 | On named owner hardware without a GPU, with the app and ordinary desktop load running: measured latency, peak memory, disk, quality against the recorded targets and cancellation for the CPU-first features, plus an offline run with the network blocked after asset installation (A02, CP12) | CPU-first local AI |
| E12 | With owner authorisation, live queries to the selected public APIs on a real topic. The owner's contact email goes only to the services on the owner-approved list (Crossref and Unpaywall unless the owner approves more), as the plan preview disclosed, and recorded requests show that no other service received it. Coverage, failures and quota behaviour recorded; screening and cited synthesis reviewed by the owner (A02, CP13) | Live literature discovery |
| E13 | The owner's real Zotero library synchronised over time: changed and deleted items, cursor recovery, a field conflict and disconnect, with the library preserved (A02, CP16) | Zotero synchronisation |
| E14 | Optional: the owner connects a real calendar through the provider's consent screen, syncs, and disconnects with local records kept; not_applicable with a reason if the owner uses no calendar connector (A02, CP16) | Calendar connector |
| E15 | The owner's real project adopted: the owner supplies roots, exclusions, re-run budget and tolerances, and records blind labels for a sample of families before seeing the output; inventory, families, ranks, plan and re-runs are compared with those labels; the owner accepts or rejects the baseline; nothing is written into the project folder (A05, CP17) | Adoption of a real project |
| E16 | A second real folder of a different kind (for example a notes vault or another project) adopted under the same rules (A05, CP17) | Generic adoption |
| E18 | Two weeks of the owner's real use of work items, events and the agent-visible queue, with observations of missed, duplicate or unwanted items and of any agent that started work without asking, other than a budget-eligible read-only verification action under A06.1 (A05, CP18) | Work items and queue in daily use |
| E19 | The owner's real project under the research map: the owner first records their own reading of a sample of claims (holds, stale, needs a check, needs my decision) without seeing the derived statuses; the statuses and decision cards are then compared with that reading, the cards are the owner's real value judgements only, and adoption is confirmed in one outline step (A06.1, CP20) | The research map on a real project |
| E21 | With the owner's OK, the owner's key and a spend cap: a live evaluation of the CP17.4 family assist on CP17's seeded corpora, through the gateway, with the targets and decision rule fixed in its evaluation record before the run; the replies recorded under call receipts; the measured precision bound, coverage and spend reported; a model that misses the targets stays untrusted (A07, CP22) | A real model trusted for the family assist |
| E20 | The owner's labelled real sample for the intelligence layer: the owner first labels a sample of real items (adoption families, claim-to-question links, card texts) without seeing the layer's proposals; the proposals are then compared with those labels and the spend recorded; only normal-sensitivity input leaves the PC (A07, CP22) | The intelligence layer on real work |

Full v1 technical acceptance requires CP0–CP11 and CP17–CP19 (A05) and independent reviews; full v1 product acceptance additionally requires E01–E07 (E07 required since A01), E09 and E10 (A02), and E15, E16 and E18 (A05). The self-contained research profile (spec/01) additionally requires CP12–CP16 with E11, E12 and E13, and its own release checkpoint, which a later amendment defines; E14 is optional. In `checkpoints.json`, `required_for_full_product` marks a gate as required for the release profile that contains its checkpoint, so it can never be not_applicable. E08 does not block a private verified product, but public release remains unauthorized until it passes. A human/device/account unavailable gate is blocked_external; it is neither a technical pass nor a reason to stop unrelated work.

The trial need not wait two arbitrary weeks or invent participants. It must use a real researcher and real work. If the researcher cannot find the source of a result, explain a correction, or identify an adopted next step without reading internal logs, the relevant product gate fails even with green unit tests. Time savings may be reported only from measured comparable tasks; otherwise use specific observed outcomes and uncertainty.

## Receipt rules

Every passing check references one or more saved evidence artifacts by relative path and SHA256. Command checks include command, exit code, stdout/stderr artifact; manual/live checks include observer, environment, steps, result and limitation report. Record skipped/blocked/failed honestly. Implemented revision or tree digest and spec-manifest digest bind evidence to a candidate. A later edit invalidates affected checks; CP8 reruns the complete critical suite.

State promotion is a derived result, not an agent-edited green checkbox. Validate schema, required checks, evidence hashes, external classification and distinct implementer/reviewer identity. Independent review is not infallible: primary must inspect material code/evidence before acceptance. When all checks pass but review is pending, state remains technically passed/review pending and cannot satisfy a dependent accepted-checkpoint requirement.

Receipts are immutable historical evidence bound to the candidate actually tested. Later checkpoints necessarily change the tree; they do not automatically erase earlier acceptance. At each change boundary create a reviewed impact record under `development/impact/` conforming to `schemas/impact.schema.json`: old/new candidate manifests, changed paths, affected check IDs, rerun receipt references, unaffected check IDs with reasons and reviewer. Carry forward only checks whose behavior/dependencies/contracts are unaffected. Unknown impact means rerun. Never edit old hashes to match a new tree. CP0's verifier validates these compatibility chains when resolving ready dependencies; absence of a required assessment is stale evidence. CP8 reruns the complete critical suite freshly against the release candidate even when earlier checks were carried forward. Field/host evidence must also be rerun when changes affect the validated capability or supported host version.

At a checkpoint boundary report: user-visible capability now demonstrated, exact tests/live checks performed, unresolved risks, external blockers and next task. Keep raw logs in receipts; concise human summaries link to them.
