---
name: rerun-paper
description: Rerun the analyses behind a paper and leave every number traced - run the generator scripts the researcher approves, register what they read and wrote, record the run by file path, then check the manuscript's numbers and report what moved. Use when the researcher asks to rerun, refresh, regenerate or update a paper's results, figures, tables or macros, or to make a paper's numbers verifiable.
compatibility: Needs the PrometheOS MCP server (prometheos mcp serve --stdio) or the prometheos CLI, and whatever runs the researcher's scripts (for example a MATLAB MCP server).
license: Apache-2.0
metadata:
  author: prometheos
  version: "1.0"
---

# Rerun a paper and keep every number traced

A number in a paper is traced when it comes from a file a generator wrote (macros such as
`generated/macros.tex`, generated tables) and a recorded run says which script wrote that exact
file from which inputs. PrometheOS checks this; you do the running and the recording.

## Steps

1. **See where the paper stands.** Call `papers_read {"request": {"op": "numbers", "paper_id": "..."}}`.
   Read its headline:
   - how many numbers are traced to a run;
   - how many come from a generated file that no recorded run wrote;
   - how many come from a run whose inputs changed since;
   - how many are typed by hand.

   `macro_files` names each generated file and its runs. Also read
   `claim_status {"paper_id": "..."}` for claims that are stale or conflicting.
2. **Propose the run list and ask once.** Name each generator script (for example
   `make_report_data.m`), what it reads (scripts, parameter files, `.mat` artefacts) and what it
   writes (the `generated/` files, figures). Include upstream runs whose inputs changed. Run
   nothing until the researcher says yes. Long or expensive runs need their explicit yes.
3. **Run, with the researcher's tools.** For example the MATLAB MCP
   (`run_matlab_file`, `evaluate_matlab_code`). Keep the exact command, the tool and its
   version, start and end times, the exit status and any warnings. If a run fails, record it as
   failed; never retry silently with changed inputs.
4. **Register the files.**
   - Use one `source_import_plan` per root and retention mode: a plan has one `root_alias` and
     one `retention`. Use `"copy"` for text and `"reference"` for very large binaries.
   - A file that is already recorded keeps its first retention mode, so put it in a plan with
     that mode.
   - Plans and commits are operation runs. Pass `"wait_seconds": 20`, or read them with
     `run_get` until `completed`, then `source_import_commit {"plan_id": ...}` the same way.
   - If a commit says every file is unchanged, that is fine: the recorded versions already
     match.
5. **Record the run by path.** Call
   `evidence_manage {"request": {"op": "receipt_from_paths", "run": {"reporter": "<you and your tools>",
   "command": "...", "working_root": "<alias>", "environment": {"os": "...", "tools": [...]},
   "started_at": "...", "ended_at": "...", "exit_status": 0, "outcome": "succeeded",
   "limitations": ["..."]}, "inputs": [{"root_alias": "...", "path": "...", "role":
   "script"}], "outputs": [{"root_alias": "...", "path": "generated/macros.tex", "role":
   "macros"}], "title": "make_report_data", "request_id": "<new UUID4>"}}`.
   - If the answer lists `needs_import`, import those files and call again with the same
     `request_id`.
   - If the call answers `request_key_conflict`, or a response was lost and the files have
     changed since, stop and tell the researcher. Never switch to a new `request_id`: the run
     may already be recorded against the earlier bytes.
   - A receipt is what you report; never record a run that did not happen.
6. **Check the numbers again** with `papers_read {"request": {"op": "numbers", ...}}`. The macros and tables
   the run wrote should now read "traced to a recorded run".
   - Report the headline and the numbers still typed by hand, with file and line. Propose
     moving each into the generator as a macro; the researcher decides.
   - Never edit the manuscript or the generator without being asked.
7. **Report what moved.** `now_get` lists the new outputs waiting for interpretation. Where a
   claim's bound number has a newer version, it drafts the old against the new value.
   - Name each claim whose number changed. A claim that quotes a macro line is listed under
     that macro in the numbers report.
   - Propose a correction or a new assessment with the existing tools (the
     `capture-research-evidence` skill). Supporting or correcting a claim is the researcher's
     decision.

## Rules

- Only the researcher's own scripts and data. Never download or install anything to make a run
  work; ask instead.
- Do not round, reformat or retype numbers. A number belongs in the manuscript only through the
  generator.
- Keep the run's limitations honest: model fidelity, the cycles used, anything skipped.
