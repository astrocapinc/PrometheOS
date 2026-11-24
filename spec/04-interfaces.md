# CLI, MCP and local API contracts

All transports call the same application operations. UTF-8 JSON is the machine format, Markdown a human export. CLI stdout contains only the requested payload in `--json` mode; diagnostics go to stderr. No prompts when stdin is not a TTY. `--help` works without a project, credentials or server.

## Stable request protocol

`prometheos apply --file request.json --json` is the canonical batch mutation. Request schema v1:

```json
{
  "schema_version": 1,
  "project_id": "project_00000000-0000-4000-8000-000000000001",
  "request_id": "00000000-0000-4000-8000-000000000002",
  "expected_seq": 0,
  "operations": [
    {
      "op": "create",
      "kind": "entry",
      "id": "entry_00000000-0000-4000-8000-000000000003",
      "base_rev": 0,
      "sensitivity": "normal",
      "occurred_at": null,
      "body": {
        "title": "Initial research question",
        "text": "Compare the proposed controller with the recorded baseline.",
        "category": "question",
        "evidence": [],
        "claim_refs": [],
        "author_ref": null,
        "origin": "typed",
        "quantities": [],
        "review": "unreviewed"
      }
    }
  ]
}
```

Allowed operations are create (envelope above), revise (`id,base_rev,body,sensitivity,occurred_at`), archive (`id,base_rev`), transition (`id,base_rev,to,reason,evidence[]`), correction_apply (`id,base_rev`), and relation_add (`source_ref,target_ref,relation`), and project_update (`base_rev,settings`). Unknown op/kind/fields fail. Revisions are full typed replacements, not arbitrary JSON Patch. Domain transitions and protected effects cannot be bypassed by create, revise, relation_add, imports or task submission. Raw relations do not change scientific state. Relations are limited to cites, supports, challenges, supersedes, depends_on, used_in, plus version_of from CP11 (A02) and answers, addresses and draws_on from CP20 (A06.1); validate kind pairs in domain schemas. Specialized importer, package and job operations still use the transaction service with the same request envelope/idempotency semantics.

Canonical hashing: UTF-8 JSON with sorted object keys, compact separators, no NaN/Infinity, strings preserved without Unicode normalization; parse duplicate JSON keys as errors. Hash the full request. Arrays preserve order. Credentials are transport metadata excluded from persisted hashes/logs. Maximums are in architecture. Implement a documented canonicalization helper shared by tools and tests.

Success: `{ok:true, schema_version:1, project_id, request_id, project_seq, results:[{op_index,id,rev}], replayed:false}`. Identical retry returns the original result with replayed=true without a new sequence. Error: `{ok:false,error:{code,message,retryable,details},project_seq}`. Details contain IDs/revisions, not secret excerpts. Codes: validation_error, not_found, revision_conflict, request_key_conflict, policy_denied, stale_task, source_unavailable, budget_exceeded, busy, io_error, unsupported. A02 adds approval_required (exit 4; details carry the approval request ID, plan digest, required scope, owner route and expiry) and capability_unavailable (exit 5; details name the capability and its blocker, such as offline, not installed, not configured or quota exhausted). Exit codes: 0 success, 2 usage/validation, 3 conflict/stale, 4 policy, 5 unavailable/I/O, 6 busy/retryable. No partial success in a batch.

## Public operations

The table is the v1 surface, introduced progressively. Names separated by `/` are separate commands. Help documents exact schemas and examples when introduced. No undocumented verb aliases needed for acceptance.

| CLI | Behavior | Checkpoint |
|---|---|---|
| `project init/info/list` | Create one store and registry binding / pure reads | CP1 |
| `apply` | Typed transaction | CP1 |
| `record get/list` | Envelope or paginated records, optional as-of sequence | CP1 |
| `source import --plan` / `--commit PLAN` | Explicit selection, retention/sensitivity preview; commit registers source+version+optional extracted text atomically | CP1 |
| `source check` | Observe current bytes; writes check record only with `--save`; default read-only | CP1 |
| `backup create/verify`, `backup restore --into` | Explicit snapshot, verification and staged restore; no overwrite default | CP1 |
| `doctor` | Read-only environment/store report with redacted paths in exports | CP1 |
| `context show/next/agenda`, `notebook show/export`, `knowledge search/links` | Bounded pure queries; export writes only specified output file | CP2 |
| `correction propose/apply`, `action transition` | Conveniences creating canonical transactions | CP2 |
| `decision review/grant/revoke` | Local owner decision channel described below | CP2 |
| `task prepare/submit/cancel/get` | Persist bounded external-agent task; result is a proposal transaction, not unrestricted instructions | CP3 |
| `mcp serve --stdio` | Official SDK transport; local project scoped | CP3 |
| `skill create/inspect/plan/install/list/adapt/update/verify/revoke/export/remove` | Local skill lifecycle, no script execution | CP3/4 |
| `workflow start/step/status/export` | Create/revise/query run records; host executes steps | CP4 |
| `retention purge plan/commit` | Owner-only digest-bound dependency closure and idempotent purge | CP2 |
| `hub serve` | Loopback service, browser pairing | CP5 |
| `meeting capture/import/reconcile` | Typed capture, raw-media import, proposal extraction/review | CP2/6 |
| `audio devices/record/transcribe` | Optional explicit local recording/transcription | CP6 |
| `paper check/export` | Pure readiness analysis and chosen local bundle export | CP7 |
| `job plan/add/list/disable`, `jobs tick` | Fixed handler schedules and bounded due-slot execution | CP7 |
| `tex engine status/install` | Report or explicitly install the pinned Tectonic release into the PrometheOS home, digest verified | CP9 |
| `paper workspace bind/files/read/write/snapshot` | Bind a paper to a local LaTeX folder; bounded file reads; base-digest-checked atomic writes; explicit version snapshot | CP9 |
| `paper compile` | Compile a staged copy of the workspace; register PDF/log/SyncTeX as generated versions with input provenance; return structured diagnostics | CP9 |
| `paper overleaf link/status/pull/push` | Overleaf Git clone/fetch/preview/integrate; push only with an exact-plan owner capability; never force | CP9 |
| `capabilities` | Pure capability discovery: feature, version, availability, required authority, local/remote, cost class, blocker | CP10 |
| `approval list/get/resume/withdraw` | Agent side of pending approvals; the owner grants or denies through `decision review` or the hub queue | CP10 |
| `run list/get/cancel/resume` | Durable operation runs | CP10 |
| `execution register/report/list` | Observe-only external scientific executions; never starts, stops or restarts anything | CP10 |
| `artifact coverage` | Pure availability and backup-coverage report per artifact | CP10 |
| `maintenance inspect/verify/restore/recover-journal/switch/migrate` | Inspect and recover without opening the damaged database; restore into a new store; switch or migrate only through the applicable owner decision | CP10 |
| `library import --plan` / `--commit PLAN`, `library search/export/dedup` | Bibliography and PDF import, local search, export, reviewed duplicate candidates | CP11 |
| `zotero status/import` | Read-only local Zotero adapter with a capability probe | CP11 |
| `matrix create/extract/review/export` | Evidence matrix with grounding validation | CP11 |
| `model list/plan/install/import/load/unload/remove/benchmark` | Local model manager | CP12 |
| `provider list/configure/test`, `route show/set` | Provider capabilities and routing policy; credential references only | CP12 |
| `inquiry create/update`, `discover plan/run`, `screen`, `synthesize`, `review export` | Literature discovery and review; discovery runs only after its plan is shown | CP13 |
| `watch add/list/pause/resume` | Literature watches over saved inquiries or claims | CP14 |
| `deliverable plan/render/list` | Meeting-to-deliverable recipes | CP15 |
| `connector list/connect/status/sync/disconnect` | Optional connectors; credentials are entered in the provider's or OS's own interface | CP16 |
| `start`, `stop`, `status`, `device list/revoke` | The web-first launcher and the remembered devices (A07); owner only; a second `start` on a running hub opens a paired browser on it and starts no second hub; `start` refuses while the home's port is taken by another program or while a desktop build from before CP21 runs | CP21 |
| `adoption root sensitivity show/set` | The sensitivity of an adoption root (A07); lowering it is a protected owner decision bound to the root alias, its resolved folder and the value (re-pointing the alias returns it to restricted), and an agent's request answers `approval_required`; raising it is not protected | CP22 |
| `ai policy show/grant/revoke`, `ai run`, `ai results`, `ai eval` | The intelligence layer (A07): the standing policy (grant and revoke are owner decisions), a task run over named subjects with a dry-run cost estimate, stored results and their validation outcomes, evaluation results per task and model | CP22 |

A02 rows name operation families; exact command, tool and route names are fixed and documented at their checkpoint.

`source import` returns a persisted plan with digest, selected roots, expected_seq and expiry. A commit rechecks bytes and authorization; changed bytes require a new plan. No recursive whole-disk discovery. Stream large files outside the JSON request. URL fetch is opt-in, size/time/redirect-limited, and blocks private network addresses unless owner explicitly configured that root.

Project-bound agents discover only their bound project: `project list` does not reveal other registry projects' IDs or titles. Creating a project is an owner operation; an agent's discovery entry point returns a typed refusal and creates nothing outside its bound project. A declared request schema confers no authority. Redacted path labels are display values and are refused as settings or path input through every transport.

Pure queries never append a hidden audit event or mark content viewed. If saving an analysis is useful, use explicit `--save` generating an entry transaction. Queries accept `--project`, `--at-seq`, `--limit`, `--cursor` where meaningful. Cursor is opaque and bound to filters/snapshot, not a mutable offset. `context show --purpose PURPOSE --budget N` returns text plus provenance/coverage manifest; private/restricted content excluded from agent contexts by default. The same caller policy applies to every read path: record_get/list, source previews, search/links, as-of reads, MCP resources, task retrieval and all exports. Direct ID lookup is not a bypass; conceal excluded existence where necessary. Owner backups require a separate explicit full-data scope.

## Decision capabilities

Transport identity and research authorship are separate. The server issues local capability records with opaque random secret, project ID, allowed operations/record IDs or narrow scope, maximum uses, expiry and policy revision. Store only a secret hash. Agent request payloads cannot create capabilities, impersonate the owner, or authorize themselves with `confirmed:true`.

The owner reviews a concrete proposal digest through paired hub UI or an interactive local `decision review` session. The UI issues an exact-plan capability, or the owner may set a clearly displayed standing policy for a narrow reversible class of operations. The client presents the secret as transport authorization; logs/context never include it. Revocation and expiry checked within the write transaction; capability use and mutations commit together. Agents can freely draft proposals without prompts. Routine data capture permitted by project policy needs no repeated confirmation. External data transmission and publication are separate scopes, never inherited from ordinary write access.

This is protection against accidental/untrusted model instructions, **not** a security boundary against malicious software already running as the same OS user. A coding host's explicit trusted approval signal could be integrated later; v1 does not infer one from model-generated text. CP0/2 tests use test-only capabilities clearly labeled synthetic.

A03 gives the paired owner one narrow root-setting review exception to display-path redaction. Only `GET /api/v1/approvals/{approval_id}` for that owner's root-setting approval view and `POST /api/v1/decisions/preview` for a root-setting decision preview may expose the exact old and proposed local root paths, in a typed `root_changes[]` list of `{alias, old_path nullable, new_path nullable}`. The field is populated only for a valid owner-rank `project_update` preview; agent views carry `root_changes=[]`. Its values come from the server's stored settings and validated proposed settings, not arbitrary effect strings. The exception is checked by principal, route and structured field; no agent reply, approval list, ordinary owner read, error or run view gains it. The owner can review a moved, added or removed root before deciding; labels never serve as stored path input.

## Tasks and context handoff

`task prepare` persists purpose, input snapshot, version bindings, rendered context digest and instructions version. It returns `task_id`, context, output JSON Schema, allowed operations and omitted-input report. The coding agent performs its own reasoning and uses its own provider. `task submit` accepts only that purpose's structured proposal and exact task bindings. Reject stale changed dependencies; unrelated changes may be revalidated against unchanged input refs, recording the new sequence before commit. One task may commit once; identical replay is idempotent. Expired, cancelled or purged inputs cannot be submitted. Persist result_request_id and task transition in the same transaction. A submitted task does not mean its proposed correction/commitment was adopted.

## MCP and hub API

`context next` returns candidates `{id,why_now,source_refs[],expected_output,dependencies[],effort,adopted_action_id nullable}` plus the common snapshot/coverage manifest; it never adopts them. `context agenda --meeting ID` returns prior adopted commitments with completion evidence, unresolved questions and proposed discussion items. Both are pure purpose-specific context queries. `workflow` convenience commands operate on the run body in the data spec. `retention purge plan` computes the affected dependency closure and backup/export limitations; commit requires an exact-plan owner capability, rechecks sequence/dependencies and returns an idempotent purge receipt without retaining deleted content. Expose these through the shared app service; no direct SQL in CLI handlers.

MCP tools: `project_info`, `record_get`, `record_list`, `source_import_plan`, `source_import_commit`, `context_get`, `knowledge_search`, `notebook_get`, `apply`, `task_prepare`, `task_submit`, `paper_check`. Advertise exact input/output schemas and SDK readOnly/destructive/idempotent annotations consistent with behavior. Resource `prometheos://project/{id}/context` is filtered the same as context_get. Never expose every historical record as an automatic prompt. Host configuration examples are generated project-local; no silent global settings changes. Under A02 every registry operation other than an owner-channel operation is reachable through MCP, grouped into operation-family tools with exact schemas and annotations; maintenance tools are also served by a maintenance mode that opens no project database.

Local HTTP: GET `/api/v1/project`, `/records/{id}`, `/records`, `/context`, `/notebook`, `/knowledge/search`, `/papers/{id}/check`; POST `/transactions`, `/tasks/prepare`, `/tasks/{id}/submit`, `/sources/plans`, `/sources/plans/{id}/commit`; owner-only decision routes and media endpoints at their checkpoints. Generate OpenAPI from the same models. Requests use the same envelopes and error codes. Query status maps 400 validation, 404 absent, 409 stale/conflict, 403 denied, 422 domain violation, 503 busy/unavailable. API is loopback only in v1. Under A02 every registry operation has a route under `/api/v1` generated from the same registry, including capabilities, approvals, runs and executions; owner-channel routes accept only the paired owner session, and maintenance routes are served without opening a project database.

Hub update delivery uses GET `/api/v1/changes?after_seq=N`: metadata-only changed IDs and latest sequence, long-poll max 20 seconds. On gaps, reconnect or old cursor, invalidate relevant queries and reload snapshots. No custom real-time event persistence or WebSocket infrastructure. Clients may cancel long polls on navigation. Files are requested by source/version ID after policy checks, never arbitrary path URL parameters.

## Parity and capability discovery (A02)

An operation registry is the single declaration of every application operation: request and response schemas, required authority, whether it mutates, its operation family and capability group, and its routes. CLI commands, MCP tools, HTTP routes and hub calls are generated from it or tested against it. Every operation is reachable through CLI, MCP and HTTP. The exception is owner-channel operations (issuing or revoking a grant, entering a credential): they are reached only through trusted owner channels, never through MCP or an agent-scoped route. Grant decisions have paired hub and interactive CLI routes; agents reach them as approval requests. A03 gives credential entry the narrow CLI-only exception: `provider secret set` accepts a hidden prompt value or an explicitly selected process environment value only within the same interactive local owner act; a supplied capability never permits piped or non-TTY entry. Entry may also happen in the provider's or OS's own interface. No HTTP route or hub form accepts a credential value. The registry records this reviewed missing-route/app-path reason; credential references and health remain reachable through the ordinary transports. Every operation that an ordinary product workflow needs also has an app path. Agent-handoff operations such as task prepare and submit need none, and presentation-only behaviour such as scrolling needs no machine operation, although the underlying navigation, filtering, selection and export do.

The inventory test runs in both directions. Forward, it fails when a registry operation lacks a required route or app path, or when transports disagree on records, errors or authority, including for denied requests. In reverse, it enumerates every CLI command, MCP tool and resource, and HTTP route that the built application actually exposes, and fails when one maps to no registry operation. The only unmapped entry points allowed are on an exemption list of transport and presentation entry points: commands that start a transport (`mcp serve`, `hub serve`), CLI help and version, pairing and session routes, the changes long-poll and static asset routes. That list is versioned with the registry and reviewed whenever it changes. The reverse direction is what catches a command or route added outside the registry.

MCP keeps its tool list understandable through operation-family tools with typed sub-operations and optional capability groups, without hiding functionality. Large results page or return resource references. Generated artifacts are readable through bounded reads or resource links, never only as screenshots. `capabilities` reports, per feature, the installed version, availability, required authority, local or remote execution, cost class and current blocker. Discovery never reveals secret values or restricted content.

## Pending approvals (A02)

An agent can prepare, request, inspect and resume every workflow. When a request needs authority the caller does not hold, the service stores the exact canonical plan and returns `approval_required` with the approval request ID, plan digest, required scope, owner route and expiry. Nothing commits. The owner reviews the same plan in the hub review queue or an interactive `decision review` session and grants or denies it. OAuth consent or OS credential entry may happen in the provider's or the OS's own interface. A grant is an exact-plan capability held by the server and bound to that approval request; the agent never receives its secret. `approval resume` executes the stored plan once, in one transaction with the capability use, after rechecking expiry, revocation and the plan's base revisions. A changed plan needs a new request. Denied, expired and withdrawn requests cannot resume. Approval requests are bound to their project and caller rank, like replayed requests (decision 0010); those raised while no project database is open live in the per-user registry (maintenance service below). Tools return credential references and health, never secret values. As before, this guards against untrusted model instructions, not against malicious software running as the same OS user.

Under A06.1 an approval request and an owner-facing suggestion may carry a decision `card`: `{what, why, recommendation, default_text, default_at nullable}`, bounded text with generated wording labelled. The hub shows it with the actions. A default changes only derived views (for example, showing a claim as settling with its corrected number) and generated drafts (A02 deliverable recipes, card text); it never changes a record, a manuscript workspace file or a paper anchor, never grants, applies a correction, transitions a record or changes support. "Apply all recommended" is one owner approval over a listed set: previewed and digest-bound like any approval, applied in one transaction that rechecks every item's base revision and applies none if one changed.

## Operation runs (A02)

Long extraction, transcription, local inference, discovery, sync and rendering operations return a run ID at once (`oprun_<UUID4>`, not a record ID). The same ID exposes status, progress, bounded logs, artifacts, limitations and cancellation through app, CLI, MCP and HTTP, and survives a closed chat or a restart. States are queued, running, waiting_for_input, paused_budget, completed, failed, cancelled and unknown_external. A run records the operation, input digests, configuration digest, workflow or recipe version, budget reservation, external-effect marker and lease. Resume continues only when input digests, model and configuration and workflow version are unchanged; anything else needs a new run. A cancellation request is recorded as requested, not as proof that a provider or external process stopped. A billable or external write whose outcome is uncertain becomes unknown_external and is never retried automatically. Status heartbeats do not advance the project sequence; research-visible results commit through the transaction service. Operation runs are distinct from recipe `run` records, job slots and external `execution` records, which are only observed.

## Project source admission (A04)

A04 (decision 0025) fixes how PrometheOS admits a project's original store files, its source: `record.sqlite` and any present `record.sqlite-journal`, `-wal` or `-shm` file. Admission precedes every open, read, write, digest, copy and journal classification of an existing store's source, including those made by doctor, backup, migration and the maintenance service. The per-user registry and PrometheOS's own scratch, staging and verification copies are not sources.

On Windows the source is observed as one coherent read-only snapshot. The database and each present side file are opened read-only with write and delete sharing denied; their physical file identities and final locations are bound and revalidated, and digests and copies come from those same held handles. Only an actual not-found result means that a file is absent; a metadata or access failure refuses. Admission never takes, probes or imitates a SQLite lock and never opens the original with SQLite to classify it.

Admission refusals are typed, change nothing (source files, registry and approvals) and are never reported as a hot journal:

- `busy`: an existing writable or conflicting handle on a source file, including an idle SQLite connection held by another program; a change of identity, location or side-file set during admission; a side file outside the database's location; a maintenance lock requested while the same thread holds an admission, or a store's gate requested out of the physical-identity order below; or the cross-process gate below not created or acquired within the existing five-second busy budget.
- `io_error`: a source file that cannot be read, or an uncertain rollback-journal header (below).
- `unsupported`: a source file without a provable physical identity, an unsupported journal mode (below), or any original-source admission on POSIX in this candidate (below).

Supported PrometheOS processes serialise their project connections through a cross-process operating-system gate keyed by the database's physical identity, so aliases of one file, such as hard links, share one gate. A connection holds its admission until it actually closes. While it is live the database cannot be deleted or replaced, and a store whose database was replaced must be opened again. The first connection to a store, and any connection after all of the thread's connections to that store have closed, needs a fresh guarded check. Further connections to that store on the same thread and through the same final path may share the live admission (backup's snapshot connections, migration's pre-migration backup); a connection through another path to the same file refuses with `busy` while that admission is live. Admission is never shared across threads or processes, and an uncertain transaction end or close prevents sharing.

The acquisition order is: maintenance locks; then the admission gates of the stores involved, in a fixed physical-identity order (a switch admits its damaged and restored stores together); then the per-user registry's write transaction. Release is in reverse order. The refusals above enforce the first two steps. Taking the registry write transaction last is an implementation obligation that the maintenance code follows, not a runtime check.

A project connection, and with it admission, is held only for bounded store reads and transactions. Device capture, provider work, compiler work, Git operations, job-handler execution and waits for an owner decision run without a live project connection. The existing busy budget and the recording and polling deadlines are therefore unchanged.

A rollback journal that is absent, empty, or whose eight-byte header magic field is entirely zero is a completed form and classifies as none while it is held stable. A journal whose header holds the SQLite journal magic is hot even when the rest is truncated: it is never applied to the original, which stays unopened, and it is recovered only on a guarded copy (maintenance service below). Any other nonempty header, including a truncated one, refuses with `io_error`.

Only the rollback (DELETE) journal mode chosen in spec/00 D03 is supported. The mode is unsupported in either of two cases:

- a `-wal` or `-shm` file is present, even an empty or orphaned one;
- the main database begins with a complete SQLite header whose file-format version bytes (offsets 18 and 19) are not the rollback value 1 (WAL declares 2).

An unsupported mode refuses ordinary admission with `unsupported`, and no original is opened to verify the mode. A missing database, or a main file without a complete SQLite header, is not by itself a mode refusal.

Open, doctor, inspect, and switch and migrate eligibility share one precedence over the guarded snapshot:

1. An uncertain journal header refuses with `io_error`.
2. An unsupported mode is `unsupported_journal_mode`, or the `newer_schema` hint below when its complete header names a newer schema. It outranks a missing or non-SQLite main file and a hot journal.
3. A missing database is `missing`.
4. A hot journal is `needs_journal_recovery`.
5. A main file without a complete SQLite header is `corrupt`.
6. Otherwise open's schema checks and, for inspect, an integrity check on a copy decide.

The results of step 2 never authorise a mutation and never make a store eligible for the recovery switch. Steps 3 to 5, and a copy found corrupt, unreadable, not a store or holding another project, remain damaged stores that the recovery switch may replace.

The gate coordinates supported PrometheOS processes only. A program of the same user that is not PrometheOS and starts writing the database directly after the guard is released is not excluded; this is the existing same-user limit of spec/08. Writable memory mappings, custom SQLite VFSs and filesystems without the tested sharing behaviour are not certified, and an unavailable observation is a refusal, never a safe result.

POSIX has no tested equivalent of the sharing guard or of a common gate namespace in this candidate. There, original-source admission returns `unsupported` for every project store, including a database without side files, and touches no SQLite lock region; registry and scratch-copy work still run. This interim refusal is a support regression against the required Linux target, not a narrowed target; spec/08 and the CP8.1 task card in spec/10 state the required repair.

## Maintenance service (A02)

`maintenance inspect/verify/restore/recover-journal` and their MCP and HTTP routes work without opening the damaged project database. They read the per-user registry, the store's files and backup archives. Inspect reports integrity, schema version and lock state; any database probe runs on a copy. Verify checks a selected backup's paths, hashes, schema and database integrity in a staging directory. Restore writes a new store. An agent's restore uses only a new PrometheOS-managed folder under its home; an agent cannot name a target or declare it local. A trusted owner may select another new location explicitly. No restore overwrites research or changes the registry by itself. A hot rollback journal is reported as `needs_journal_recovery`, with `opens=false`; recovery copies the database, journal and retained media, lets SQLite recover only the copy, verifies it and restores it into a new store. The original database and journal are preserved. When a project cannot open, the hub shows these recovery actions.

Under A04 the maintenance service reads the source only through the guarded admission above; when that admission refuses, inspect refuses with the same typed error instead of reporting a health. When the journal mode is unsupported, open refuses `unsupported` with `store_health=unsupported_journal_mode`, and inspect, doctor and the hub recovery screen stay usable without opening the original or creating a side file.

Inspect then reports:

- `health=unsupported_journal_mode`, `opens=false` and `journal.mode_supported=false`;
- `schema_version=null`;
- the raw main-header value as `header_schema_version`, a hint and not a verified schema (null when there is no complete SQLite header);
- `integrity_checked=false`, where an `integrity` of `unreadable` means not checked (`missing` when there is no database) and `integrity_detail` gives the reason.

When that complete header names a schema newer than this build supports, open refuses with the newer-schema refusal. Inspect then reports `health=newer_schema`, with the header value as `schema_version` and `schema_version_source=main_header_hint`. This is a conservative refusal hint that verifies neither the WAL contents nor the schema.

In a package that also has a hot rollback journal, the hot state stays visible as `journal.state=hot`. The package is still `unsupported_journal_mode`, and an unsupported mode authorises no journal copy, managed recovery output, migration or switch; each refuses `unsupported`. `unsupported_journal_mode` is not a damaged-store health and never makes a store eligible for a recovery switch, and a current or lower header hint never authorises a migration request or grant. Cached inspect results are bound to the rollback state, mode support and file signatures and confer no mutation or connection authority.

When source admission or the open checks refuse to open a registered project's store, the refusal keeps its typed code, message and details and carries the selected project ID and the recovery pointer (CLI, MCP maintenance mode, HTTP and the hub recovery screen). This includes the `busy`, `io_error` and `unsupported` admission refusals and a SQLite busy during the open's identity read. The recovery screen and the maintenance routes therefore stay reachable. This context asserts no store health, verified identity or switch eligibility. Doctor reports an `io_error` or `unsupported` open refusal as `opens=false` with the pointer, returns `busy` as `busy`, and makes any read after the identity check through a fresh admission.

Maintenance switching requires a damaged registered store and a verified replacement. A store that opens, needs migration or requires newer software is not switched through this recovery operation. An owner who deliberately rolls back a healthy store uses the explicit `backup restore` owner path, whose preview names the current sequence and the backup sequence and date. An agent's switch target must be a PrometheOS-managed restore. Switching is protected and returns `approval_required` unless the trusted owner performs it. The damaged store is quarantined without being overwritten or opened for writing and stays listed for inspection. A database file whose digest cannot be read gives a typed `io_error`; no switch request, grant use or registry switch is authorised with an unreadable marker. The owner must first restore read access so a fresh plan can bind the actual digest. A missing database may bind the existing `missing` identity.

Because no project database is open, switch and migrate approval requests live in the per-user registry (`maintenance_approvals`), not in a project store. A switch request binds the project ID, the damaged store's location and database digest, its health, schema and current sequence when readable, the backup archive and verified manifest digests, the new store's location and database digest, the caller rank and the plan digest, and it expires. The replacement's retained media must match the verified backup's digests. A migrate request binds the project ID, the registered store's location and database digest, from/to schema versions, the caller rank and the plan digest, and expires; it has no backup or target-store binding. The owner grants either on the hub's recovery screen (a paired owner session in maintenance mode) or in an interactive local maintenance session. A changed bound value needs a new request. The grant is held in the registry and used once: a switch consumes it in the registry transaction that switches; a migration rechecks the registered location, database digest and migratable health, spends the grant before the one migration attempt, then creates and verifies the pre-migration backup before changing the store. Migration never opens a damaged database for writing. A failed or uncertain attempt is not repeated under the same grant.

A04 adds a source binding to both request types: the physical identity (as decimal strings), size and digest of the database and of each journal or side file present, observed under one guard. A store in an unsupported journal mode gets no switch or migrate request. The final switch rechecks supported mode, the registered location and the complete source binding under the guard, inside the registry transaction that uses the grant. A granted migration makes the same checks and spends the grant inside the maintenance lock and admission that the migration then keeps through its verified pre-migration backup, its schema transaction and the close of its source connections, so no supported PrometheOS writer can enter in between.

Journal recovery re-inspects the store under one guard and refuses with `busy` if the store's maintenance lock is held or the store no longer needs journal recovery. It copies the database and journal from that guarded snapshot, and the retained media while that admission is held, before creating managed output; the recovered backup verifies every media digest. A pending request recorded without a complete source binding cannot be used; a new request is needed.

## Adapter egress (A02)

Discovery adapters, connectors and remote model routes are egress. Each runs only where the project or workflow has opted in. Before the first request it shows the provider and the exact query or data scope, and it records query, filters, provider, time, pagination and failures. Quotas, rate limits, terms and contact requests are adapter metadata, not product promises, and are enforced before calls. The owner's contact email for polite access is a per-user owner setting held locally together with an owner-approved list of services allowed to receive it; the initial list is Crossref and Unpaywall (decision 0010). Adapter metadata may request an email, but that never sends it: the address goes only to services on the approved list, and adding a service needs the owner's explicit approval. The discovery plan preview names every service that will receive the address. An unapproved service never receives it; its adapter runs without the address where the service allows that, and otherwise reports capability_unavailable until the owner approves the service. An offline or exhausted adapter returns capability_unavailable or budget_exceeded, and local work continues. The operating policy is in spec/08.

**The intelligence layer (A07, CP22).** A remote task run inside the owner's standing policy is egress the owner granted once for that task kind, where the project's and workflow's route permits it. It needs no per-call preview, and the policy, the destination, the data policy sent and each call with its input bundle digest are recorded and shown in the hub.

- **Restricted or private input** to a layer task answers the existing error code `policy_denied`; there is no per-call path for it.
- **Other runs outside the policy** (another task kind, a model not on the evaluated list) answer `approval_required` with the CP6.3 per-call preview; a reached cap answers `budget_exceeded`.
- **Agents** may call `ai run` only where the policy says agents may start runs, and may read results. An agent's attempt to grant or widen the policy answers `approval_required`, and its attempt to revoke it answers `policy_denied`. A model's output never reaches an operation other than storing a validated proposal.
- **Bundles** name files only by root alias and relative path, never by an absolute path.
- **Without a key or a policy,** `ai run` runs the rules executor and reports what it did instead.

## Work items, events and the agent-visible queue (A05)

- **Work items are actions.** They use the existing action operations and parity rules. The optional `description` and `labels[]` are accepted on create and update.
- **Untrusted description.** A description is returned as untrusted researcher text (the untrusted-content boundary applies), never as instructions to an agent.
- **Events.** One read operation lists events by type, subject and acknowledgement state with pagination. Acknowledging an event is an owner operation that agents may request.
- **The queue.** One read operation returns the agent-visible queue: open work items (with deadline, reason and description size) and unacknowledged events (with type, subject and reason), ordered by due time and then age, each with a stable reference.
- **Deadlines.** Reported in UTC with the owner's configured time zone label.
- **Nothing executes.** None of these operations starts, schedules or claims agent work. An agent that decides, with the owner, to work on an item updates the action through the normal operations; the one exception is a budget-eligible read-only verification action (A06.1, below), which the agent may start without a per-item question.
- **The event catalogue.** CP18.2 registers it as a contract schema checked by `check_contracts`. Each type has a subject kind, a closed data object and its emitter. An event's data never carries record text or a local path; readers fetch details through the normal operations. The initial catalogue:

| Type | Subject | Data | Emitted by |
|---|---|---|---|
| `work_item.created` | action | status, due_at | action create with a work-item field or label |
| `work_item.changed` | action | changed fields' names, status, due_at | action revise or transition |
| `deadline.approaching` | action | due_at, days_before (14, 7 or 2) | deadline detector, once per deadline and threshold |
| `approval.requested` | approval request | operation, scope | an operation answering `approval_required` |
| `approval.decided` | approval request | decision | owner grant or deny |
| `result.awaiting_interpretation` | execution | state | execution report detector |
| `correction.flagged_dependents` | correction | dependent count by kind | correction apply |
| `backup.coverage_gap` | project | uncovered version count | backup coverage detector |
| `source.unavailable` | source | availability state | artifact availability detector |
| `run.outcome_unknown` | operation run | stage | run recovery marking an external outcome unknown |

Later checkpoints extend the catalogue through the same contract (for example CP17 plan steps and CP14 watch hits); a type is never removed or redefined in place.

**Budgeted verification (A06.1, CP20).** The owner's `verification_budget` admits read-only checks only. A **read-only check** re-reads, recomputes or compares from retained or referenced artefacts; it runs no simulation and no model, edits no model, script or parameter file, and writes only new result files inside the output folder the owner names in the budget (for the owner's project, `reports/`). An action is budget-eligible when its `verification.kind` is `read_only`, its `estimated_minutes` is set and within `max_minutes_each`, and either a PrometheOS detector created it and no agent has since revised its `verification` fields, or the owner adopted it (the owner's recorded transition from proposed, under owner scope) after the last such revision; an agent-created verification action, and any action whose `verification` fields an agent revised, is ineligible until the owner adopts it, and an agent cannot adopt one. The agent's transition of an eligible action to `in_progress` is accepted without an approval while the sum of the estimates of the verification actions agents started that calendar day (in the project's time zone) stays within `max_minutes_per_day`; an ineligible start, or one past the daily total, answers `approval_required` for the owner. The agent reports the outcome through a receipt and an assessment with expected, measured and tolerance. PrometheOS cannot tell who opened an agent session; A05's rule that agents act only when the owner opens a session or asks is unchanged. PrometheOS never launches, schedules or wakes an agent, and never starts a simulation.

## Generic MCP surface and agent package (A05)

- **One surface for every host.** The MCP server is the same for every host. Operations are named and typed by the registry, with no host-specific operation, and capability discovery describes the surface.
- **The package** contains:
  - the server launch configuration;
  - the original PrometheOS skills;
  - a session-start summary that reads the queue and asks the owner whether to work on an item;
  - documentation for Claude Code and for at least one non-Claude MCP host.
- **Opt-in setup.** Writing a host's configuration is a setup step the owner runs; nothing changes a host's global configuration otherwise.
- **The protocol.** The package uses the standard MCP protocol revisions the SDK supports.
