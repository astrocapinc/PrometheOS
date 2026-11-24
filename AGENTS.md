# PrometheOS development rules

This file governs the new implementation folder only. Current explicit human instructions win. The spec is a chosen design, not evidence that anything is built.

## Ownership and working rhythm

- Primary owns scope, architecture, final acceptance and user communication. Delegate bounded discovery/implementation/tests/browser QA to native subagents when available. One implementation writer per checkout; read-only reviewers may work in parallel. Do not claim independence for self-review.
- Use the settled design. For an unanticipated routine detail, choose the smallest compatible solution and document it. For a contract, data integrity, security or major scope change, write a decision amendment and adapt affected requirements/tests before proceeding. Ask the owner only for consequential product choices that cannot be resolved from this package.
- Keep the user informed at material decisions and at least roughly once a minute during active tool work. Do not convert every checkpoint into a permission request.
- Do not resume Herdr/OpenCode worker sessions. Supporting external coding-agent clients as product integrations is distinct from development orchestration.
- Do not write empty packages for all future features, implement every optional integration or add broad dependencies at bootstrap.

## Start or resume

1. Read `spec/README.md`, this file, `spec/10-delivery.md`, `checkpoints.json`, `development/state.json`, the latest receipt and HANDOFF. Read subsystem specs only as needed.
2. Inspect Git status, changed files, running relevant processes and actual tool versions. Preserve unexplained changes. Establish that another writer is not modifying the same checkout.
3. Run package validation. Validate historical receipts against their recorded candidate manifests and spec versions. For later changes, require the explicit reviewed impact/carry-forward record defined in the delivery spec; do not rewrite old receipts or require an unchanged whole tree forever. Passing unexplained stale evidence is not acceptance.
4. Select the first ready task whose dependencies have `technical=passed` **and** `review=accepted`, with no unresolved incompatible change. `blocked_external` does not prevent independent technical work, but cannot satisfy an external gate.
5. Write the current task, expected outcome, check IDs, scope and exclusions to HANDOFF before substantial edits.

## Implementation and evidence

Implement through one application layer. Default tests and record writes make no model calls or third-party requests. Test meaningful behavior, not a mirror of the implementation. Include a focused regression for each reproduced defect. Use targeted mutation/negative cases for high-risk invariants; do not inherit an hours-long blanket reversal harness.

For each task: run its checks, retain stdout/stderr and exit codes, inspect the artifacts, have a reviewer examine code and evidence, repair demonstrated defects, and save a receipt. A test name, screenshot, count or plausible final message is not a receipt. Browser tests must assert state and use the actual application; screenshots supplement them.

Never invent commands having run. Tests requiring credentials/devices stay unavailable until exercised. A reviewer must not approve their own implementation. If no independent reviewer is available, mark review pending and continue independent work; do not promote the checkpoint to accepted.

Use `development/receipts/CPx/<run-id>.json` and adjacent relative evidence files. Record exact revision or a digest of the dirty implementation, environment, commands, checks, limitations, reviewer and next task. `tools/verify_package.py` is not an implementation test. Implement `tools/verify_receipt.py` in CP0 using the supplied schema, required gate checks and evidence hashes.

## Promotion and continuation

Technical status: `not_started`, `in_progress`, `failed`, `passed`. Independent review: `pending`, `changes_requested`, `accepted`. External evidence: `not_run`, `blocked_external`, `failed`, `passed`, `not_applicable` (only optional capabilities, with reason).

A checkpoint is technically accepted only if all its required checks pass on the declared candidate, scope is met and independent review is accepted. A field gate requires real researcher evidence; never substitute an agent role-playing a user.

After acceptance, update state and HANDOFF atomically and proceed to the next ready task. At session end preserve exact resume commands, known issues, pending external evidence, running process handles, and the next task. Do not repeat valid checks unless changed code, dependencies, environment or spec invalidate them. CP8 deliberately includes a full clean-install regression.

After three materially different failed repair attempts on the same cause, checkpoint the failure and switch to a ready independent task if one exists. Do not silently weaken acceptance thresholds. Escalate a concrete blocking question with its evidence when no meaningful safe work remains.

## Actions and controls

Local source edits, reversible builds, tests, and local previews are within development scope. Dependency installation into the project environment is routine; inspect license/security before adoption. Use minimal extras.

No automatic public repository creation, pushes, PR publication, package publishing, external messages, new paid services, global host configuration or shared-manuscript writes. Local drafts, integration plans and opt-in setup scripts are allowed. Do not forge DCO sign-offs, user approvals, participant identities or legal clearance.

Stop dependent feature work on suspected data loss, corrupt restore, privacy leakage, unstable transaction semantics or a rights issue affecting copied code. Preserve evidence, repair or isolate the problem, and allow unrelated safe tasks to continue. Never turn an unavailable check into success.

## Quality gates

CP0 creates actual project tooling. Afterwards use the documented commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run python tools/check_contracts.py`, and the checkpoint-specific acceptance path. Add frontend checks when the hub exists. Pin exact dependencies in a lockfile; runtime claims require tested versions, not speculative version numbers from input designs.

Tests use temporary project stores and the bundled synthetic fixtures. Never point destructive tests at real research. Application data, recordings, credentials and local state are ignored by Git. Code/spec/fixture changes are reviewable independently of local acceptance evidence.
