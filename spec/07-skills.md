# Skills, recipes and community

PrometheOS is useful to agents first. Ship original research procedures as ordinary Agent Skills with `SKILL.md`, progressive references and examples. The first three are **capture research evidence**, **reconcile a meeting**, and **inspect scoped claim evidence**. They teach the agent how to call PrometheOS; PrometheOS does not call or replace the coding agent.

## Package format

Use the Agent Skills specification for the skill itself. An optional `prometheos-package.json` sidecar describes import/provenance and declarative recipes; it is a PrometheOS format, not a claimed universal standard.

Required sidecar fields: `schema_version=1`, `name` (lowercase letters/digits/hyphens, max64), `version` (SemVer), `description`, `skills[]` (relative directories), `license_expression` (SPDX expression or null), `components[]` (`path`, `sha256`, `license_expression`, `origin_url` nullable, `origin_revision` nullable, `modified` boolean), `capabilities[]`, `recipes[]`. Capabilities are advisory declarations drawn from project_read, project_write, source_read, network, external_write; they are not an OS sandbox. Unknown license is an explicit unresolved state, never inferred from a neighboring file.

Recipe fields: `id,title,inputs` (JSON Schema), `steps[]` (`id,instruction,requires[]`, `expected_artifacts[]`, `checks[]`), `outputs` (JSON Schema). Step instructions are for the host agent and may link to skills. PrometheOS can validate required inputs, render the recipe and record progress/artifacts; v1 does not execute a shell DAG, provision infrastructure, spawn agents or guarantee arbitrary script safety. Source scripts may be distributed only as clearly displayed files subject to the host's controls; install never executes them.

Package lock records exact origin URL/revision/archive digest, imported file digests, licenses, modifications, installation root and trust review date. A registry may later index these static packages; v1 uses a local catalog and explicit URL/local-folder/archive imports. Community sharing works through portable exports and pull requests without an account or central service.

## Lifecycle and security

1. **Create:** scaffold an original SKILL.md, references, examples and manifest. Author chooses a license; no silent license assignment to imported content.
2. **Inspect:** display files, declared capabilities, origin, licenses, script/network references and suspicious instructions. Static inspection cannot certify safety.
3. **Plan import/install:** normalize paths, reject traversal/absolute paths/symlinks outside package, enforce compressed/uncompressed size/count limits, resolve collisions and missing licenses. No code runs.
4. **Install:** copy reviewed files to a project-local host projection using a recorded plan digest, with rollback and exact lock. Missing rights/provenance blocks redistribution/adaptation; an unresolved executable package is quarantined pending review. Referencing a remote repository as a link is still possible. From CP10 (A03), an agent may prepare install, update and remove plans, but each apply step returns `approval_required` unless backed by the owner's exact-plan grant. The owner sees the files, executable flags, inspection findings and per-file rights, together with the concrete projection changes. The grant binds the plan digest, package digest and catalogue status; resume rechecks them and the projection plan before one apply attempt, spending the server-held grant before filesystem effects. A changed binding needs a new request, and a failed attempt is not retried under the same grant. A paired owner session or interactive CLI may apply directly after showing the same plan. Static inspection and declared rights do not substitute for the owner's decision to install host instructions.
5. **Adapt/verify/revoke:** adaptation creates a new local package identity with parent digest and modification records. Verify recomputes manifest digests and license completeness without executing code. Revoke adds origin+digest to a local denylist, refuses new installations and quarantines existing catalog entries; warn that already copied host files may remain loaded until host refresh. Offer a reviewed projection removal plan without deleting user edits. Restoring trust needs an explicit owner decision; no central registry is required.
6. **Use:** host agent loads selected skill with bounded context and calls authorized APIs. PrometheOS never promotes package instructions above user/project policy.
7. **Update/remove:** compare exact changed files/licenses/capabilities; create a new plan, preserve local modifications or require explicit conflict resolution. Removal affects only manifest-owned unmodified files, never unrelated user files.
8. **Export/share:** include lock, notices, modification record and license files; refuse distributable export when any included component's rights are unresolved. Local drafts remain possible.

CP3 supplies original skills and one tested host setup; CP7 adds the paper-readiness skill when its operations exist; CP4 supplies import/create/update/remove/export, recipes and project-local projections for a second host. Avoid “install all skills” by default; show purpose and input cost, load only the relevant skill. Supporting a format does not prove every host executes it correctly; record live host versions and evidence.

## Cooperation and scope

The six user-provided repositories are research inputs and potential dependencies, not an automatic vendor list. Scientific skills repositories can contribute useful procedures after per-file rights/provenance review. OpenScience/K-Dense/EvoScientist and AI-Scientist systems may produce artifacts that PrometheOS records; do not import their whole orchestration stack to obtain one useful procedure.

Prefer integration with a stable interface over forks. Prefer a focused dependency over a broad embedded runtime. Fork only for a documented missing capability with maintenance cost and upstream plan. A rewrite is not a magic licensing escape: independently implement an interface from permitted specifications, and record what code/text was copied or adapted.

## Community governance

Use contribution guidelines, DCO sign-off, issue/skill templates, schema checks, example fixtures and a maintainer review. One maintainer can review an external contribution; there is no artificial requirement for three maintainers/packages before participation. Do not mark one's own package “independently audited.” Trust labels mean: origin identified, license reviewed, automated checks passed, human review completed, live use observed; show each separately.

Reject credential exfiltration, hidden side effects, fabricated scientific claims, destructive defaults and licenses the distributor cannot honor. Security reports have a private contact route when a public project exists. Governance, hosting and publishing are release decisions; development can proceed with local package fixtures before a community exists.

## Declared pipelines and deliverable recipes (A02)

A02 lets PrometheOS itself run bounded, declared pipelines (literature discovery, screening, extraction and watches from CP13 and CP14) and render deliverable recipes (CP15). Both are data, not code. The sidecar gains optional `pipelines[]` and recipe `templates[]` entries at those checkpoints.

- A pipeline declares `id`, `inputs` (JSON Schema), `outputs` (JSON Schema), `steps[]` that each name one operation designated for pipelines in the operation registry, with its parameters, `limits` (records, pages, calls, tokens, time, concurrency, disk) and `review_points[]` where a person must confirm before the pipeline continues. Only literature operations are designated: discovery queries, deduplication, screening, full-text lookup, extraction into a matrix, watch refresh and the reads they need. Validation rejects a declaration that names any other operation. A protected operation is never run as a step: where a pipeline reaches one, it stops at a review point and the step returns `approval_required`. A package cannot add or designate an operation, widen a step's egress or authority, or embed code. A step that would exceed a limit pauses as a durable operation run instead of continuing.
- A deliverable template is an inert text file (for example Markdown or LaTeX) filled from named evidence fields by a logic-less renderer. Templates cannot read files, run code or call operations. Each output records the template's package digest and version together with the evidence versions used. Binary office formats need a separately reviewed dependency.
- PrometheOS ships its own pipelines and templates. Imported ones go through the same inspect, plan, licence and provenance rules as skills, and a pipeline's remote steps still need the project's egress opt-in. Host-executed recipes above remain host-executed; a declared pipeline is not a general workflow engine or agent loop.
