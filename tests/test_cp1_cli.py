"""CP1.4 CLI slice: real subprocess calls, one JSON document on stdout, stable exit codes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cp1_support import FIXTURES

from prometheos.domain.ids import new_id, new_request_id


def synthetic_capability(tmp: Path, project_id: str, operation: str, uses: int = 5) -> str:
    """Test-only owner capability (labelled synthetic), issued in-process."""
    from prometheos.app.service import Service

    grant = Service(home=tmp / "home").grant(
        project_id,
        operation=operation,
        digest=None,
        max_uses=uses,
        label="SYNTHETIC-TEST cli",
        synthetic=True,
    )
    return grant.secret


class Cli:
    def __init__(self, tmp: Path) -> None:
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PROMETHEOS_")}
        for key in ("PROMETHEOS_TEX_ENGINE_DIR", "PROMETHEOS_MODELS_DIR"):
            if key in os.environ:
                self.env[key] = os.environ[key]
        self.env["PROMETHEOS_HOME"] = str(tmp / "home")
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.cwd = tmp

    def __call__(self, *args: str, stdin: bytes | None = None) -> tuple[int, Any, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "prometheos", *args, "--json"],
            cwd=self.cwd,
            env=self.env,
            input=stdin,
            capture_output=True,
            timeout=120,
        )
        out = proc.stdout.decode("utf-8")
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, f"stdout must hold exactly one JSON document: {out!r}"
        return proc.returncode, json.loads(lines[0]), proc.stderr.decode("utf-8")


def test_cli_end_to_end_slice(tmp_path: Path) -> None:
    research = tmp_path / "research"
    research.mkdir()
    shutil.copyfile(FIXTURES / "baseline.md", research / "baseline.md")
    cli = Cli(tmp_path)

    code, init, _ = cli(
        "project",
        "init",
        "--title",
        "CLI study",
        "--objective",
        "Undecided",
        "--root",
        f"research={research}",
        "--link",
        str(research),
    )
    assert code == 0, init
    pid = init["project_id"]
    assert json.loads((research / ".prometheos-link.json").read_text())["project_id"] == pid

    code, listing, _ = cli("project", "list")
    assert code == 0 and [p["project_id"] for p in listing["projects"]] == [pid]

    code, plan, _ = cli(
        "source", "import", "--plan", "--root", "research", "baseline.md", "--retention", "copy"
    )
    assert code == 0 and plan["retained"] == ["baseline.md"]
    code, commit, _ = cli("source", "import", "--commit", plan["plan_id"])
    assert code == 0 and commit["project_seq"] == 2
    version_id = plan["items"][0]["version_id"]

    eid = new_id("entry")
    body = {
        "title": "CLI note",
        "text": "Typed via apply.",
        "category": "note",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed",
        "quantities": [],
        "review": "unreviewed",
    }
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": pid,
            "request_id": new_request_id(),
            "expected_seq": 2,
            "operations": [
                {
                    "op": "create",
                    "kind": "entry",
                    "id": eid,
                    "base_rev": 0,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": body,
                }
            ],
        }
    ).encode()
    req = tmp_path / "req.json"
    req.write_bytes(raw)
    code, applied, _ = cli("apply", "--file", str(req))
    assert code == 0 and applied["results"][0]["id"] == eid
    code, replay, _ = cli("apply", "--file", "-", stdin=raw)
    assert code == 0 and replay["replayed"] is True

    code, stale, _ = cli(
        "apply",
        "--file",
        "-",
        stdin=raw.replace(
            raw[raw.index(b'"request_id": "') + 15 : raw.index(b'"request_id": "') + 51],
            new_request_id().encode(),
        ),
    )
    assert code == 3 and stale["error"]["code"] == "revision_conflict"

    code, record, _ = cli("record", "get", eid)
    assert code == 0 and record["body"]["title"] == "CLI note"
    code, version, _ = cli("record", "get", version_id)
    assert code == 0 and version["body"]["availability"] == "retained"
    code, page, _ = cli("record", "list", "--kind", "entry", "--limit", "1")
    assert code == 0 and len(page["items"]) == 1

    code, check, _ = cli("source", "check", version_id)
    assert code == 0 and check["saved"] is False

    out = tmp_path / "backup.zip"
    cap = tmp_path / "full.cap"
    cap.write_text(synthetic_capability(tmp_path, pid, "full_data_export"), encoding="utf-8")
    code, refused, _ = cli("backup", "create", "--out", str(out))
    assert code == 4 and refused["error"]["code"] == "policy_denied"
    code, made, _ = cli("backup", "create", "--out", str(out), "--capability-file", str(cap))
    assert code == 0 and Path(made["backup"]).is_file()
    code, verified, _ = cli("backup", "verify", str(out))
    assert code == 0 and verified["verified"] is True
    code, refused, _ = cli("backup", "restore", str(out), "--into", str(tmp_path / "copy"))
    assert code == 4 and "interactive" in refused["error"]["message"]  # owner at a terminal
    assert not (tmp_path / "copy").exists()

    code, doctor, _ = cli("doctor")
    assert code == 0
    assert doctor["project"]["integrity_check"] == "ok"
    assert doctor["project"]["journal_mode"] == "delete"


def test_cli_error_classes_and_exit_codes(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    code, err, _ = cli("record", "get", new_id("entry"))
    assert code == 2 and err["ok"] is False  # no project registered
    code, init, _ = cli("project", "init", "--title", "t", "--objective", "o")
    pid = init["project_id"]
    code, err, _ = cli("record", "get", "not-an-id", "--project", pid)
    assert code == 2 and err["error"]["code"] == "not_found"
    code, err, _ = cli("apply", "--file", "-", "--project", pid, stdin=b"{not json")
    assert code == 2 and err["error"]["code"] == "validation_error"
    denied = json.dumps(
        {
            "schema_version": 1,
            "project_id": pid,
            "request_id": new_request_id(),
            "expected_seq": 1,
            "operations": [
                {
                    "op": "create",
                    "kind": "version",
                    "id": new_id("version"),
                    "base_rev": 0,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": {},
                }
            ],
        }
    ).encode()
    code, err, _ = cli("apply", "--file", "-", stdin=denied)
    assert code == 4 and err["error"]["code"] == "policy_denied"
    code, err, _ = cli("source", "import", "--plan", "--commit", "x", "--project", pid)
    assert code == 2


def test_human_mode_writes_errors_to_stderr(tmp_path: Path) -> None:
    env = {**os.environ, "PROMETHEOS_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "prometheos", "record", "get", new_id("entry")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr.startswith("error[")


def test_file_system_errors_become_io_error_envelopes(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    code, init, _ = cli("project", "init", "--title", "t", "--objective", "o")
    code, err, stderr = cli(
        "apply", "--file", str(tmp_path / "missing.json"), "--project", init["project_id"]
    )
    # A request file the caller named that does not exist is a bad argument, not an I/O fault:
    # it names the file and is not retryable (decision 0079, P28). Still an envelope, no trace.
    assert err["error"]["code"] == "validation_error" and err["error"]["retryable"] is False
    assert err["error"]["details"] == {"file": str(tmp_path / "missing.json")}
    assert code == 2 and "Traceback" not in stderr  # validation_error's exit code
