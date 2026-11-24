"""External gate E02: a second, independently implemented coding host (OpenCode) uses the
same evidence/context contract and an original skill.

Run ONLY with the owner's explicit authorisation: it starts real `opencode run` sessions,
which use whatever model provider the owner has configured for OpenCode (possibly paid).
It never changes global host configuration: everything is written into a disposable
project folder (`opencode.json` with the PrometheOS MCP server, the bundled skills under
`.opencode/skills/`), and the host runs with `--pure` (no external plugins). Edits, shell
and web tools are denied in that project-local config; the host can only use the
PrometheOS MCP tools and read the projected skills.

The disposable project is the same synthetic one as E01. The host is asked to follow the
`inspect-claim-evidence` skill, read context, import a result as evidence, propose (not
apply) a correction, then resume in a second session. Host version, exact config,
prompts, JSON event transcripts and before/after record snapshots are retained. A human
must read the transcripts and record the verdict; this script grades nothing.

Usage (from the repository, after owner approval):
    uv run python tools/live_host_e02.py --i-have-owner-approval \\
        --evidence development/receipts/CP4/<run>/e02
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

PROMPT_1 = (
    "You are working on a synthetic research project recorded in PrometheOS (MCP server "
    "'prometheos'). Follow the project skill 'inspect-claim-evidence'. 1) Call context_get and "
    "summarise the objective and the scoped findings. 2) Register plant-result.md from root "
    "alias 'research' as evidence with source_import_plan then source_import_commit. 3) The claim "
    "that the candidate improves energy recovery across operating conditions is overbroad given "
    "the plant result. Create a correction PROPOSAL with apply (kind 'correction', superseding "
    "it with the existing narrowed claim, citing the plant result version). Do not apply it; the "
    "researcher decides. Report the IDs you created."
)
PROMPT_2 = (
    "New session. Using only the PrometheOS MCP tools, find what changed since the last session: "
    "is there a pending correction proposal, and what does the researcher need to decide? Do not "
    "change anything."
)


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)  # type: ignore[no-any-return]


def host_env(work: Path) -> dict[str, str]:
    """The owner's provider credentials (XDG data) stay available; their global OpenCode
    configuration (XDG config, OPENCODE_CONFIG* overrides) is isolated."""
    return {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("OPENCODE_CONFIG")},
        "PROMETHEOS_HOME": str(work / "home"),
        "XDG_CONFIG_HOME": str(work / "xdg-config"),
    }


def prepare(work: Path) -> dict[str, Any]:
    """Build the disposable project and set OpenCode up through the product's own
    `host setup --host opencode` code path; record the few harness adjustments."""
    sys.path.insert(0, str(ROOT / "tools"))
    from e01_setup import build_project

    from prometheos.skills import host_apply, host_plan

    project = build_project(work)
    setup = host_plan("opencode", work, project["project_id"])
    host_apply(setup)
    config_path = work / "opencode.json"
    generated = json.loads(config_path.read_text(encoding="utf-8"))
    config = json.loads(json.dumps(generated))
    server = config["mcp"]["prometheos"]
    # Harness-only adjustments: run this checkout's interpreter against the disposable
    # store, and deny edit/shell/web tools so only PrometheOS tools can act.
    server["command"] = [sys.executable, "-m", *server["command"][1:]]
    server["environment"] = {"PROMETHEOS_HOME": str(work / "home")}
    config["permission"] = {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {
        "project": project,
        "config": config,
        "generated_by_host_setup": generated,
        "harness_adjustments": ["command interpreter", "environment", "permission"],
        "setup_files": setup["files"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--i-have-owner-approval", action="store_true", required=True)
    parser.add_argument("--model", default=None, help="provider/model, else OpenCode's default")
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="prometheos-e02-"))
    env = host_env(work)
    version = run(["opencode", "--version"]).stdout.strip()
    (evidence / "host-version.txt").write_text(version + "\n", encoding="utf-8")
    prepared = prepare(work)
    project = prepared["project"]
    (evidence / "project.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
    shutil.copyfile(work / "opencode.json", evidence / "opencode.json")
    (evidence / "host-setup.json").write_text(
        json.dumps(
            {k: prepared[k] for k in ("generated_by_host_setup", "harness_adjustments",
                                      "setup_files")},
            indent=2,
        ),
        encoding="utf-8",
    )  # fmt: skip
    snapshot = [
        sys.executable, "-m", "prometheos", "record", "list", "--limit", "200",
        "--project", project["project_id"], "--json",
    ]  # fmt: skip
    (evidence / "before.json").write_text(run(snapshot, env=env).stdout, encoding="utf-8")
    for index, prompt in enumerate((PROMPT_1, PROMPT_2), 1):
        (evidence / f"prompt-{index}.txt").write_text(prompt + "\n", encoding="utf-8")
        cmd = ["opencode", "run", "--format", "json", "--pure", "--dir", str(work)]
        if args.model:
            cmd += ["--model", args.model]
        result = run([*cmd, prompt], cwd=work, env=env, timeout=1800)
        (evidence / f"session-{index}.jsonl").write_text(result.stdout, encoding="utf-8")
        (evidence / f"session-{index}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        (evidence / f"session-{index}.exit.txt").write_text(f"{result.returncode}\n", "utf-8")
    (evidence / "after.json").write_text(run(snapshot, env=env).stdout, encoding="utf-8")
    calls = [
        {"session": index, "line": line[:300]}
        for index in (1, 2)
        for line in (evidence / f"session-{index}.jsonl").read_text("utf-8").splitlines()
        if "prometheos" in line and "tool" in line
    ]
    (evidence / "tool-events.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    print(json.dumps({"host": version, "tool_events": len(calls), "evidence": str(evidence)}))
    print("A human must now read session-*.jsonl and before/after.json and record the verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
