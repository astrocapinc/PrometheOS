"""External gate E01: one real installed coding host uses PrometheOS through MCP.

Run ONLY with the owner's explicit authorisation: it starts a real Claude Code
session (`claude -p`), which uses the owner's Claude account. It never changes
global host configuration: the MCP server is passed with --mcp-config and
--strict-mcp-config from a file in a disposable folder, and tools are limited to
the PrometheOS MCP tools.

The script builds a disposable synthetic project (fixtures only), asks the host to
(1) read context, (2) import a result file as evidence, (3) propose a correction of
the overbroad claim, and (4) resume from the record in a second fresh session. It
retains the host version, exact prompts, stream-json transcripts, and before/after
record snapshots under an evidence folder, then prints a summary. A human (owner or
reviewer) must still read the transcript; this script does not grade usefulness.

Usage (from the repository, after owner approval):
    uv run python tools/live_host_e01.py --evidence development/receipts/CP3/<run>/e01
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

ROOT = Path(__file__).resolve().parents[1]

PROMPT_1 = (
    "You are working on a synthetic research project recorded in PrometheOS (MCP server "
    "'prometheos'). Use the project skill 'inspect-claim-evidence' where it applies. "
    "1) Call context_get and summarise the objective and the scoped findings. "
    "2) Register the file plant-result.md from root alias 'research' as evidence with "
    "source_import_plan then source_import_commit (retention copy). 3) The claim stating that the "
    "candidate improves energy recovery across operating conditions is overbroad given the plant "
    "result. Create a correction PROPOSAL with apply: a create operation of kind 'correction' "
    "superseding that claim with the existing narrowed replacement claim, citing the plant result "
    "version. Do not try to apply it; the researcher decides. Report the IDs you created."
)
PROMPT_2 = (
    "Fresh session. Using only the PrometheOS MCP tools, find what changed since the last session: "
    "is there a pending correction proposal, and what would the researcher need to decide? Do not "
    "change anything."
)


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)  # type: ignore[call-overload]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--i-have-owner-approval", action="store_true", required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="prometheos-e01-"))
    env = {**os.environ, "PROMETHEOS_HOME": str(work / "home")}
    version = run(["claude", "--version"]).stdout.strip()
    (evidence / "host-version.txt").write_text(version + "\n", encoding="utf-8")

    sys.path.insert(0, str(ROOT / "tools"))
    os.environ["PROMETHEOS_HOME"] = env["PROMETHEOS_HOME"]
    from e01_setup import build_project  # disposable fixture project with claims

    project = build_project(work)
    # Project-local setup through the same code path users run (`host setup --apply`):
    # the shipped skills land in work/.claude/skills; --strict-mcp-config below still
    # pins the server to this checkout's interpreter.
    from prometheos.skills import host_apply, host_plan

    setup = host_plan("claude-code", work, project["project_id"])
    (evidence / "host-setup.json").write_text(
        json.dumps({k: v for k, v in setup.items() if k != "_contents"}, indent=2),
        encoding="utf-8",
    )
    host_apply(setup)
    (evidence / "project.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
    mcp_config = work / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "prometheos": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "prometheos",
                            "mcp",
                            "serve",
                            "--stdio",
                            "--project",
                            project["project_id"],
                        ],
                        "env": {"PROMETHEOS_HOME": env["PROMETHEOS_HOME"]},
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copyfile(mcp_config, evidence / "mcp-config.json")
    snapshot = [
        sys.executable,
        "-m",
        "prometheos",
        "record",
        "list",
        "--limit",
        "200",
        "--project",
        project["project_id"],
        "--json",
    ]
    (evidence / "before.json").write_text(run(snapshot, env=env).stdout, encoding="utf-8")
    for index, prompt in enumerate((PROMPT_1, PROMPT_2), 1):
        (evidence / f"prompt-{index}.txt").write_text(prompt + "\n", encoding="utf-8")
        result = run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                "--mcp-config",
                str(mcp_config),
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__prometheos",
                "--no-session-persistence",
            ],
            cwd=work,
            env=env,
            timeout=1800,
        )
        (evidence / f"session-{index}.jsonl").write_text(result.stdout, encoding="utf-8")
        (evidence / f"session-{index}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        (evidence / f"session-{index}.exit.txt").write_text(f"{result.returncode}\n", "utf-8")
    (evidence / "after.json").write_text(run(snapshot, env=env).stdout, encoding="utf-8")
    calls = []
    for index in (1, 2):
        for line in (evidence / f"session-{index}.jsonl").read_text(encoding="utf-8").splitlines():
            if '"tool_use"' in line and "mcp__prometheos__" in line:
                for part in json.loads(line).get("message", {}).get("content", []):
                    if part.get("type") == "tool_use":
                        calls.append({"session": index, "tool": part["name"]})
    (evidence / "tool-calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    print(json.dumps({"host": version, "tool_calls": calls, "evidence": str(evidence)}, indent=2))
    print("A human must now read session-*.jsonl and before/after.json and record the verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
