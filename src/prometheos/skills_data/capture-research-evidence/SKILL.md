---
name: capture-research-evidence
description: Record a research result in PrometheOS after running a simulation, analysis or script. Use when you produced outputs (figures, CSV/MAT files, logs) that should become evidence, or when the user asks to log, register or note a result in the research notebook.
compatibility: Needs the PrometheOS MCP server (prometheos mcp serve --stdio) or the prometheos CLI.
license: Apache-2.0
metadata:
  author: prometheos
  version: "1.3"
---

# Record a result with traceable evidence

## Steps

1. **Register the outputs** you created, from an approved research root:
   `source_import_plan {"root_alias": "...", "paths": ["results/run3.csv"], "retention": "copy",
   "wait_seconds": 20}`. Imports are operation runs: the answer is a run (`run_id`, `state`).
   When `state` is `completed`, the plan is in its `result`; if it is still `queued` or
   `running`, read it later with `run_get {"run_id": "..."}`. Check the plan's `retained` /
   `reference_only` lists, then `source_import_commit {"plan_id": "<result.plan_id>",
   "wait_seconds": 20}` and wait for that run to be `completed` too (its `result` is the
   commit). Large binary outputs may stay `reference`; reference-only files can be cited but
   not quoted or used for quantities. A `failed` run carries its `error`.
2. **Record what ran** as an execution receipt, after registering its outputs. The simplest
   way names the files by path: `evidence_manage {"request": {"op": "receipt_from_paths",
   "run": {...the fields below except inputs and outputs...}, "inputs": [{"root_alias": "...",
   "path": "...", "role": "script"}], "outputs": [...], "title": "run3", "request_id":
   "<new UUID4>"}}`. Each path is pinned to its recorded version. A file not recorded yet, or
   changed since, comes back in `needs_import`: import it, then call again with the same
   `request_id`. To name versions yourself instead:
   `source_receipt_record {"title": "run3", "request_id": "<new UUID4>", "receipt": {
   "schema_version": 1, "observation": "reported", "reporter": "<you and your tools>",
   "command": "...", "working_root": "<root alias>", "environment": {"os": "...", "tools":
   [{"name": "MATLAB", "version": "..."}]}, "started_at": null, "ended_at": null,
   "exit_status": 0, "outcome": "succeeded", "inputs": [], "outputs": [{"source_id": "...",
   "version_id": "...", "sha256": "...", "role": "model"}], "limitations": ["..."]}}`
   (CLI: `prometheos source receipt --file receipt.json --title run3 --request-id <UUID4>`).
   Each input/output must name a registered version with its exact `sha256` (see the
   `sources` section of your task context). Models, figures and MAT files are linked this
   way. A receipt is `reported`, never `observed`: do not claim MATLAB or anything else ran
   unless it did, and record failed runs too (`outcome: "failed"`).
3. **Prepare the notebook draft**: `task_prepare {"purpose": "notebook_draft"}`. Read its
   `instructions`, `output_schema` and context.
4. **Cite the receipt** in the entry evidence with a JSON locator
   (`{"type": "json", "pointer": ""}`, or `/outputs/0` for one output). JSON numbers are
   cited, not bound: bind numbers from your text outputs.
5. **Bind every number** you report as a structured quantity:
   `{"decimal": "2.40", "unit": "kJ", "source_ref": {source_id, version_id, locator},
   "source_pointer": "text:<line>:<start>:<end>"}`. The pointed characters must be exactly
   `2.40 kJ` in the retained file. Copy, never round or reformat (2.4 != 2.40).
6. **Submit**: `task_submit {"task_id": "...", "result": {"purpose": "notebook_draft",
   "entry": {...}}, "request_id": "<new UUID4>"}`. Keep the request ID; resubmitting the same
   one after a timeout is safe. If you get `stale_task`, prepare a new task.

## Rules

- State the result's scope and limitations. A simplified model or one operating regime does
  not establish general performance.
- Entries are stored as unreviewed agent text. Do not write "approved" or "verified by the
  researcher" - it grants nothing and misleads readers.
- Never include credentials or secrets in recorded text or environment listings.

## Example quantity

File line 7: `Recovered energy with the candidate: 2.40 kJ.` -> characters 37..44 are `2.40 kJ`,
so `"source_pointer": "text:7:37:44"` with locator `{"type": "text", "start_line": 7,
"end_line": 7}`.
