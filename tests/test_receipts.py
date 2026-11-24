"""Receipt validation and derived state promotion (check C00-RECEIPT).

Each test builds a disposable copy of the specification package, runs real
commands through tools/run_check.py, assembles receipts with
tools/build_receipt.py and then tampers with them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import build_receipt
import pytest
import run_check
import tree_manifest
import verify_receipt
from conftest import REPO, requires_private_records

# Every test here builds on the repository's own receipts, state or decisions (decision 0072).
pytestmark = requires_private_records


@pytest.mark.parametrize("exit_code", [1, None, False])
def test_build_receipt_refuses_failed_or_missing_environment_exit(
    root: Path, exit_code: Any
) -> None:
    rel = make_run(root, run_id="environment-failure")
    (root / rel).unlink()  # this test's disposable receipt, never repository evidence
    run_dir = root / rel.removesuffix(".json")
    path = run_dir / "env.cmd.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["exit_code"] = exit_code
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(SystemExit, match="environment report did not exit 0"):
        build_receipt.build(rel.removesuffix(".json"), root)
    assert not (root / rel).exists()


def test_build_receipt_refuses_environment_stdout_digest_mismatch(root: Path) -> None:
    rel = make_run(root, run_id="environment-tamper")
    (root / rel).unlink()
    run_dir = root / rel.removesuffix(".json")
    env = json.loads((run_dir / "env.stdout.txt").read_text(encoding="utf-8"))
    env["python"] = "fabricated-version"
    (run_dir / "env.stdout.txt").write_text(json.dumps(env), encoding="utf-8")
    with pytest.raises(SystemExit, match="stdout digest differs"):
        build_receipt.build(rel.removesuffix(".json"), root)
    assert not (root / rel).exists()


@pytest.mark.parametrize("exit_code", [7, None, False, "0", "missing"])
def test_verifier_refuses_environment_failure_even_with_valid_stdout(
    root: Path, exit_code: Any
) -> None:
    rel = make_run(root, run_id="environment-verifier")
    record_rel = rel.removesuffix(".json") + "/env.cmd.json"
    path = root / record_rel
    record = json.loads(path.read_text(encoding="utf-8"))
    if exit_code == "missing":
        record.pop("exit_code")
    else:
        record["exit_code"] = exit_code
    path.write_text(json.dumps(record), encoding="utf-8")
    # Rehash the deliberately forged evidence so the failure must be the environment's
    # nonzero/missing exit, rather than the generic evidence hash mismatch.
    edit(
        root,
        rel,
        lambda receipt: [
            item.update(sha256=verify_receipt.sha256_file(path))
            for item in receipt["evidence"]
            if item["path"] == record_rel
        ],
    )
    assert has(validate(root, rel), "environment report did not exit 0")


def test_verifier_accepts_the_explicit_environment_integer_zero(root: Path) -> None:
    rel = make_run(root, run_id="environment-zero")
    record = json.loads((root / rel.removesuffix(".json") / "env.cmd.json").read_bytes())
    assert type(record["exit_code"]) is int and record["exit_code"] == 0
    assert validate(root, rel) == []


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("missing_record", "environment report unreadable"),
        ("output_digest", "stdout digest differs from command record"),
        ("candidate", "different tree than the candidate"),
        ("kind", "not a command record"),
        ("environment", "environment differs from the environment report"),
    ],
)
def test_verifier_binds_environment_evidence(root: Path, fault: str, expected: str) -> None:
    rel = make_run(root, run_id="environment-binding")
    run_dir = rel.removesuffix(".json")
    record_rel = run_dir + "/env.cmd.json"
    record_path = root / record_rel
    record = json.loads(record_path.read_bytes())
    if fault == "missing_record":
        record_path.unlink()
    elif fault == "output_digest":
        out_rel = run_dir + "/env.stdout.txt"
        output_path = root / out_rel
        output = json.loads(output_path.read_bytes())
        output["python"] = "fabricated-version"
        output_path.write_text(json.dumps(output), encoding="utf-8")
        edit(
            root,
            rel,
            lambda receipt: [
                item.update(sha256=verify_receipt.sha256_file(output_path))
                for item in receipt["evidence"]
                if item["path"] == out_rel
            ],
        )
    elif fault == "environment":
        edit(root, rel, lambda receipt: receipt["environment"].update(python="fabricated"))
    else:
        record["tree_sha256" if fault == "candidate" else "kind"] = "fabricated"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        edit(
            root,
            rel,
            lambda receipt: [
                item.update(sha256=verify_receipt.sha256_file(record_path))
                for item in receipt["evidence"]
                if item["path"] == record_rel
            ],
        )
    assert has(validate(root, rel), expected)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("command", "missing", "no valid command"),
        ("command", None, "no valid command"),
        ("command", [], "no valid command"),
        ("command", "", "no valid command"),
        ("command", " ", "no valid command"),
        ("command", "fabricated", "command differs from argv"),
        ("argv", "missing", "no valid argv"),
        ("argv", None, "no valid argv"),
        ("argv", "python", "no valid argv"),
        ("argv", [], "no valid argv"),
        ("argv", [""], "no valid argv"),
        ("argv", [" "], "no valid argv"),
        ("argv", [False], "no valid argv"),
        ("argv", ["python", 1], "no valid argv"),
        ("name", "other", "invalid command semantics"),
        ("cwd", "elsewhere", "invalid command semantics"),
        ("schema_version", True, "invalid command semantics"),
    ],
)
def test_environment_command_is_present_and_bound_in_verifier_and_builder(
    root: Path, field: str, value: Any, expected: str
) -> None:
    rel = make_run(root, run_id="environment-command-binding")
    record_rel = rel.removesuffix(".json") + "/env.cmd.json"
    path = root / record_rel
    record = json.loads(path.read_bytes())
    if value == "missing":
        record.pop(field)
    else:
        record[field] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    edit(
        root,
        rel,
        lambda receipt: [
            item.update(sha256=verify_receipt.sha256_file(path))
            for item in receipt["evidence"]
            if item["path"] == record_rel
        ],
    )
    assert has(validate(root, rel), expected)
    (root / rel).unlink()
    with pytest.raises(SystemExit, match=expected):
        build_receipt.build(rel.removesuffix(".json"), root)
    assert not (root / rel).exists()


def test_one_authoritative_environment_stdout_binds_verifier_and_builder(root: Path) -> None:
    rel = make_run(root, run_id="environment-alternate-output")
    run_dir = rel.removesuffix(".json")
    record_rel = run_dir + "/env.cmd.json"
    record_path = root / record_rel
    record = json.loads(record_path.read_bytes())
    alternate = root / run_dir / "alternate.stdout.txt"
    output = json.loads((root / run_dir / "env.stdout.txt").read_bytes())
    output["python"] = "alternate-command-version"
    alternate.write_text(json.dumps(output), encoding="utf-8")
    record["stdout"] = {"file": alternate.name, "sha256": verify_receipt.sha256_file(alternate)}
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def forge(receipt: dict[str, Any]) -> None:
        for item in receipt["evidence"]:
            if item["path"] == record_rel:
                item["sha256"] = verify_receipt.sha256_file(record_path)
        receipt["evidence"].append(
            {
                "path": run_dir + "/" + alternate.name,
                "sha256": verify_receipt.sha256_file(alternate),
                "description": "actual alternate environment command stdout",
            }
        )

    edit(root, rel, forge)
    assert has(validate(root, rel), "stdout must be env.stdout.txt")
    (root / rel).unlink()
    with pytest.raises(SystemExit, match=r"stdout must be env\.stdout\.txt"):
        build_receipt.build(run_dir, root)
    assert not (root / rel).exists()


OK = [sys.executable, "-c", "print('ok')"]
FAIL = [sys.executable, "-c", "import sys; print('bad'); sys.exit(1)"]
ENV = [
    sys.executable,
    "-c",
    "import json; print(json.dumps({'os': 'TestOS', 'architecture': 'x', 'python': '3',"
    " 'tool_versions': [], 'code_under_test': {'prometheos_package': 'src/prometheos',"
    " 'package_in_candidate': True, 'environment': '.venv', 'pythonpath': []}}))",
]
CP0_CHECKS = ["C00-BOOT", "C00-CONTRACT", "C00-RECEIPT"]
# The sealed specification version the fixture copies (amendment-proof; decision 0011).
SPEC_VERSION = json.loads((REPO / "checkpoints.json").read_text(encoding="utf-8"))["spec_version"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    manifest = json.loads((REPO / "package-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        dest = repo / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / item["path"], dest)
    shutil.copyfile(REPO / "package-manifest.json", repo / "package-manifest.json")
    (repo / "development").mkdir(exist_ok=True)
    initial_state = json.loads((REPO / "development/state.json").read_text(encoding="utf-8"))
    for cp in initial_state["checkpoints"].values():
        cp.update(technical="not_started", review="pending", receipt=None)
    for gate in initial_state["external_gates"].values():
        gate.update(status="not_run", receipt=None, reason=None)
    initial_state["implementation_exists"] = False
    (repo / "development/state.json").write_text(json.dumps(initial_state), encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return repo


def make_run(
    root: Path,
    cp: str = "CP0",
    run_id: str = "r1",
    checks: dict[str, list[str]] | None = None,
    failing: tuple[str, ...] = (),
    review: dict[str, Any] | None = None,
    implementer: str = "impl-a",
    external: list[dict[str, Any]] | None = None,
    suite: bool = False,
    argv: dict[str, list[str]] | None = None,
) -> str:
    checks = checks if checks is not None else {cid: [cid.lower()] for cid in CP0_CHECKS}
    argv = argv or {}
    rel = f"development/receipts/{cp}/{run_id}"
    run_dir = root / rel
    run_dir.mkdir(parents=True)
    tree_manifest.write_manifest(run_dir / "tree-manifest.json", root)
    if suite:
        (run_dir / "suite.json").write_bytes((root / "tools/checkpoint_suites.json").read_bytes())
    run_check.main(["--run-dir", str(run_dir), "--root", str(root), "--name", "env", "--", *ENV])
    plan_checks = []
    for cid, names in checks.items():
        for name in names:
            run_check.main(
                [
                    "--run-dir",
                    str(run_dir),
                    "--root",
                    str(root),
                    "--name",
                    name,
                    "--",
                    *argv.get(name, FAIL if name in failing else OK),
                ]
            )
        plan_checks.append(
            {"id": cid, "method": "automated", "commands": names, "summary": f"{cid} ran"}
        )
    plan = {
        "implementer_id": implementer,
        "checks": plan_checks,
        "external_checks": external or [],
        "limitations": ["synthetic test receipt"],
        "next_task": "next",
    }
    (run_dir / "receipt-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    if review is not None:
        verdict = {"run_id": run_id, "summary": "reviewed", **review}
        (run_dir / "review.json").write_text(json.dumps(verdict), encoding="utf-8")
    build_receipt.build(rel, root)
    return f"{rel}.json"


ACCEPT = {"status": "accepted", "reviewer_id": "reviewer-b"}


def validate(root: Path, rel: str) -> list[str]:
    return verify_receipt.validate_receipt(root, rel)[1]


def edit(root: Path, rel: str, mutate: Any) -> None:
    path = root / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def state(root: Path) -> dict[str, Any]:
    return json.loads((root / "development/state.json").read_text(encoding="utf-8"))


def has(errors: list[str], fragment: str) -> bool:
    return any(fragment in e for e in errors)


def test_valid_receipt_validates_and_promotes(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    assert validate(root, rel) == []
    assert verify_receipt.promote(root, rel) == []
    entry = state(root)["checkpoints"]["CP0"]
    assert entry == {"technical": "passed", "review": "accepted", "receipt": rel}
    assert state(root)["implementation_exists"] is True


def test_pending_review_promotes_technical_only(root: Path) -> None:
    rel = make_run(root)
    assert verify_receipt.promote(root, rel) == []
    assert state(root)["checkpoints"]["CP0"]["review"] == "pending"


def test_missing_and_duplicate_and_foreign_checks_rejected(root: Path) -> None:
    rel = make_run(root, checks={"C00-BOOT": ["boot"], "C00-CONTRACT": ["contract"]})
    assert has(validate(root, rel), "required check C00-RECEIPT must appear exactly once")
    edit(root, rel, lambda d: d["checks"].append(dict(d["checks"][0])))
    assert has(validate(root, rel), "C00-BOOT must appear exactly once (found 2)")
    edit(root, rel, lambda d: d["checks"].append(dict(d["checks"][0], id="C01-TXN")))
    assert has(validate(root, rel), "C01-TXN does not belong to CP0")


def test_failed_command_cannot_be_claimed_as_pass(root: Path) -> None:
    rel = make_run(root, failing=("c00-receipt",), review=None)
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    assert data["technical_status"] == "failed"
    assert validate(root, rel) == []  # an honest failed receipt is valid

    def forge(d: dict[str, Any]) -> None:
        d["technical_status"] = "passed"
        for check in d["checks"]:
            check["status"] = "passed"
            for command in check["commands"]:
                command["exit_code"] = 0

    edit(root, rel, forge)
    assert has(validate(root, rel), "differs from recorded 1")


def test_pass_with_nonzero_exit_rejected(root: Path) -> None:
    rel = make_run(root, failing=("c00-boot",))
    edit(root, rel, lambda d: d["checks"][0].update(status="passed"))
    errors = validate(root, rel)
    assert has(errors, "passing check has nonzero exit code")


def test_tampered_and_missing_artifacts_rejected(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    run_dir = root / rel.removesuffix(".json")
    (run_dir / "c00-boot.stdout.txt").write_text("forged output\n", encoding="utf-8")
    assert has(validate(root, rel), "evidence digest mismatch")
    (run_dir / "c00-contract.cmd.json").unlink()
    errors = validate(root, rel)
    assert has(errors, "missing evidence artifact")


def test_uncited_and_escaping_evidence_rejected(root: Path) -> None:
    rel = make_run(root)
    edit(root, rel, lambda d: d["checks"][0]["evidence_paths"].append("src/app.py"))
    assert has(validate(root, rel), "not listed in evidence")

    def escape(d: dict[str, Any]) -> None:
        d["evidence"].append(
            {
                "path": "development/receipts/CP0/r1/../../../../src/app.py",
                "sha256": "0" * 64,
                "description": "escape",
            }
        )

    edit(root, rel, escape)
    assert has(validate(root, rel), "may not traverse")

    def outside(d: dict[str, Any]) -> None:
        d["evidence"][-1]["path"] = "src/app.py"

    edit(root, rel, outside)
    assert has(validate(root, rel), "outside")


def test_linked_evidence_rejected(root: Path) -> None:
    rel = make_run(root)
    run_dir = root / rel.removesuffix(".json")
    link = run_dir / "linked.txt"
    try:
        os.symlink(root / "src/app.py", link)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    def cite(d: dict[str, Any]) -> None:
        d["evidence"].append(
            {
                "path": "development/receipts/CP0/r1/linked.txt",
                "sha256": verify_receipt.sha256_file(root / "src/app.py"),
                "description": "link",
            }
        )

    edit(root, rel, cite)
    assert has(validate(root, rel), "link/reparse")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_junction_evidence_rejected(root: Path) -> None:
    import subprocess

    rel = make_run(root)
    run_dir = root / rel.removesuffix(".json")
    junction = run_dir / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(root / "src")],
        capture_output=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr

    def cite(d: dict[str, Any]) -> None:
        d["evidence"].append(
            {
                "path": "development/receipts/CP0/r1/junction/app.py",
                "sha256": verify_receipt.sha256_file(root / "src/app.py"),
                "description": "through a junction",
            }
        )

    edit(root, rel, cite)
    assert has(validate(root, rel), "link/reparse")


@pytest.mark.parametrize("reviewer", ["impl-a", "Impl_A", " IMPL A "])
def test_self_review_rejected(root: Path, reviewer: str) -> None:
    rel = make_run(root, review={"status": "accepted", "reviewer_id": reviewer})
    assert has(validate(root, rel), "self-review")


def test_review_verdict_must_match_reviewer_artifact(root: Path) -> None:
    rel = make_run(root, review={"status": "changes_requested", "reviewer_id": "reviewer-b"})
    assert validate(root, rel) == []
    edit(root, rel, lambda d: d["review"].update(status="accepted"))
    assert has(validate(root, rel), "verdict differs")


def test_review_cannot_accept_failed_candidate(root: Path) -> None:
    rel = make_run(root, failing=("c00-boot",), review=ACCEPT)
    assert has(validate(root, rel), "did not pass")


def write_impact(
    root: Path,
    impact: dict[str, Any],
    name: str = "i1",
    verdict: dict[str, Any] | None = None,
) -> str:
    """Write an impact record, then a reviewer verdict bound to its digest."""
    impact_dir = root / "development/impact"
    (impact_dir / "reviews").mkdir(parents=True, exist_ok=True)
    rel = f"development/impact/{name}.json"
    review_rel = f"development/impact/reviews/{name}.review.json"
    impact = dict(impact, review_evidence_paths=[review_rel])
    (root / rel).write_text(json.dumps(impact), encoding="utf-8")
    review = {
        "kind": "prometheos-impact-review",
        "impact_path": rel,
        "impact_sha256": verify_receipt.sha256_file(root / rel),
        "reviewer_id": impact["reviewer_id"],
        "status": impact["review_status"],
        "summary": "reviewed diff",
    }
    review.update(verdict or {})
    (root / review_rel).write_text(json.dumps(review), encoding="utf-8")
    return rel


def base_impact(root: Path, rel: str) -> dict[str, Any]:
    impact_dir = root / "development/impact"
    impact_dir.mkdir(parents=True, exist_ok=True)
    new_digest = tree_manifest.write_manifest(impact_dir / "i1-to.json", root)
    return {
        "schema_version": 1,
        "from_manifest": f"{rel.removesuffix('.json')}/tree-manifest.json",
        "from_tree_sha256": json.loads((root / rel).read_text())["candidate"]["tree_sha256"],
        "to_manifest": "development/impact/i1-to.json",
        "to_tree_sha256": new_digest,
        "changed_paths": ["src/app.py"],
        "affected_check_ids": [],
        "rerun_receipts": [],
        "carried_checks": [
            {"id": cid, "source_receipt": rel, "reason": "constant change only"}
            for cid in CP0_CHECKS
        ],
        "author_id": "impl-a",
        "reviewer_id": "reviewer-b",
        "review_status": "accepted",
        "review_evidence_paths": [],
    }


def impact_errors(root: Path, rel: str) -> list[str]:
    return verify_receipt.validate_impact(root, rel)[1]


def test_stale_candidate_rejected_until_reviewed_impact_carries_it(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    before = state(root)
    (root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert has(verify_receipt.promote(root, rel), "not the current tree")
    assert state(root) == before  # failed promotion writes nothing

    impact = base_impact(root, rel)
    bad = write_impact(root, dict(impact, changed_paths=["src/wrong.py"]))
    assert has(impact_errors(root, bad), "changed_paths disagree")
    assert verify_receipt.promote(root, rel) != []

    for reviewer in ("IMPL-A", "impl.a", "Impl A"):
        bad = write_impact(root, dict(impact, reviewer_id=reviewer))
        assert has(impact_errors(root, bad), "impact self-review")

    good = write_impact(root, impact)
    assert impact_errors(root, good) == []
    assert verify_receipt.promote(root, rel) == []


def test_impact_review_must_be_a_bound_reviewer_verdict(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    (root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    impact = base_impact(root, rel)
    # Arbitrary existing files are not a review.
    forged = dict(impact, review_evidence_paths=["development/state.json"])
    (root / "development/impact/i1.json").write_text(json.dumps(forged), encoding="utf-8")
    assert has(impact_errors(root, "development/impact/i1.json"), "reviewer-written")
    assert verify_receipt.promote(root, rel) != []
    # A review of an earlier version of the record does not cover later edits.
    good = write_impact(root, impact)
    tampered = json.loads((root / good).read_text())
    tampered["carried_checks"][0]["reason"] = "edited after review"
    (root / good).write_text(json.dumps(tampered), encoding="utf-8")
    assert has(impact_errors(root, good), "changed after review")
    # Verdict text must agree with the record.
    mismatch = write_impact(root, impact, verdict={"status": "changes_requested"})
    assert has(impact_errors(root, mismatch), "verdict differs")


def test_affected_checks_need_reruns_and_cannot_be_carried(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    (root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    impact = base_impact(root, rel)
    impact["affected_check_ids"] = ["C00-BOOT"]
    impact["carried_checks"] = impact["carried_checks"][1:]
    rel_impact = write_impact(root, impact)
    assert has(impact_errors(root, rel_impact), "no passing rerun receipt")
    assert has(verify_receipt.promote(root, rel), "stale")


def test_carried_checks_must_come_from_the_edge_start(root: Path) -> None:
    unrelated = make_run(root, run_id="old", review=ACCEPT)
    (root / "src/app.py").write_text("VALUE = 5\n", encoding="utf-8")
    rel = make_run(root, run_id="mid", review=ACCEPT)
    (root / "src/app.py").write_text("VALUE = 6\n", encoding="utf-8")
    impact = base_impact(root, rel)
    # Carry the checks from a receipt on an unrelated earlier tree, not this edge start.
    impact["carried_checks"] = [
        {"id": cid, "source_receipt": unrelated, "reason": "borrowed"} for cid in CP0_CHECKS
    ]
    rel_impact = write_impact(root, impact)
    assert has(impact_errors(root, rel_impact), "neither on from_tree")
    ok, _ = verify_receipt.chain_to(
        root,
        verify_receipt.Package(root),
        json.loads((root / unrelated).read_text()),
        impact["to_tree_sha256"],
    )
    assert not ok  # ... but cannot carry a receipt whose tree is not the edge start
    assert has(verify_receipt.promote(root, rel), "stale")  # nor this one


def test_carried_source_receipt_needs_accepted_review(root: Path) -> None:
    rel = make_run(root)  # review pending
    (root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    rel_impact = write_impact(root, base_impact(root, rel))
    assert has(impact_errors(root, rel_impact), "review is not accepted")


def test_malformed_impact_manifest_is_an_error_not_a_crash(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    impact = base_impact(root, rel)
    bad_manifest = root / "development/impact/i1-to.json"
    bad_manifest.write_text('{"kind": "prometheos-tree-manifest"}', encoding="utf-8")
    impact["to_tree_sha256"] = verify_receipt.sha256_file(bad_manifest)
    rel_impact = write_impact(root, impact)
    assert has(impact_errors(root, rel_impact), "no file list")


def test_command_records_from_another_tree_rejected(root: Path) -> None:
    rel = make_run(root)
    run_dir = root / rel.removesuffix(".json")
    record_path = run_dir / "c00-boot.cmd.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["tree_sha256"] = "f" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def rehash(d: dict[str, Any]) -> None:
        for item in d["evidence"]:
            item["sha256"] = verify_receipt.sha256_file(root / item["path"])

    edit(root, rel, rehash)
    assert has(validate(root, rel), "different tree than the candidate")


def test_silent_passing_command_rejected(root: Path) -> None:
    silent = [sys.executable, "-c", "pass"]
    rel_dir = root / "development/receipts/CP0/s"
    rel_dir.mkdir(parents=True)
    tree_manifest.write_manifest(rel_dir / "tree-manifest.json", root)
    run_check.main(["--run-dir", str(rel_dir), "--root", str(root), "--name", "env", "--", *ENV])
    for cid in CP0_CHECKS:
        run_check.main(
            ["--run-dir", str(rel_dir), "--root", str(root), "--name", cid.lower(), "--", *silent]
        )
    plan = {
        "implementer_id": "impl-a",
        "checks": [
            {"id": c, "method": "automated", "commands": [c.lower()], "summary": "s"}
            for c in CP0_CHECKS
        ],
        "limitations": [],
        "next_task": None,
    }
    (rel_dir / "receipt-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    build_receipt.build("development/receipts/CP0/s", root)
    assert has(validate(root, "development/receipts/CP0/s.json"), "no output at all")


def test_commands_must_match_declared_suite(root: Path) -> None:
    suite = {
        "schema_version": 1,
        "suites": {"CP0": {cid: {"summary": "s", "commands": {"x": OK}} for cid in CP0_CHECKS}},
    }
    (root / "tools/checkpoint_suites.json").write_text(json.dumps(suite), encoding="utf-8")
    rel = make_run(root, review=ACCEPT, suite=True)
    assert validate(root, rel) == []
    suite["suites"]["CP0"]["C00-BOOT"]["commands"] = {"x": FAIL}
    (root / "tools/checkpoint_suites.json").write_text(json.dumps(suite), encoding="utf-8")
    rel2 = make_run(root, run_id="r2", review=ACCEPT, suite=True)
    assert has(validate(root, rel2), "differ from the declared checkpoint suite")
    # Swapping in a different suite copy is detected through the manifest digest.
    run_dir = root / rel.removesuffix(".json")
    (run_dir / "suite.json").write_text(json.dumps(suite), encoding="utf-8")

    def rehash(d: dict[str, Any]) -> None:
        for item in d["evidence"]:
            item["sha256"] = verify_receipt.sha256_file(root / item["path"])

    edit(root, rel, rehash)
    assert has(validate(root, rel), "is not the candidate's")


ECHO = [sys.executable, "-c", "import sys; print(sys.argv[1:])"]


def write_suite(root: Path, commands: dict[str, dict[str, list[str]]]) -> None:
    suite = {
        "schema_version": 1,
        "suites": {
            "CP0": {cid: {"summary": "s", "commands": cmds} for cid, cmds in commands.items()}
        },
    }
    (root / "tools/checkpoint_suites.json").write_text(json.dumps(suite), encoding="utf-8")


def test_run_dir_placeholder_is_the_receipts_own_relative_run_folder(root: Path) -> None:
    """Review cp10-browser R3-F1: ``{run_dir}`` in a declared command stands for the receipt's own
    repository-relative run folder. A command recorded for another run's folder, an absolute
    (machine-dependent) folder or the unexpanded template does not match the declaration."""
    commands = {cid: {cid.lower(): OK} for cid in CP0_CHECKS}
    commands["C00-BOOT"] = {"browser": [*ECHO, "--evidence", "{run_dir}/browser-evidence"]}
    write_suite(root, commands)

    def recorded(folder: str) -> dict[str, list[str]]:
        return {"c00-boot": [*ECHO, "--evidence", f"{folder}/browser-evidence"]}

    own = "development/receipts/CP0/r1"
    rel = make_run(root, run_id="r1", review=ACCEPT, suite=True, argv=recorded(own))
    assert validate(root, rel) == []
    for run_id, folder in (
        ("r2", own),  # another run's folder
        ("r3", (root / "development/receipts/CP0/r3").as_posix()),  # this run's, but absolute
        ("r4", "{run_dir}"),  # the template itself
    ):
        errors = validate(
            root, make_run(root, run_id=run_id, review=ACCEPT, suite=True, argv=recorded(folder))
        )
        assert has(errors, "C00-BOOT: commands differ from the declared checkpoint suite"), run_id
        assert not has(errors, "C00-CONTRACT"), run_id


def test_checkpoint_run_with_run_dir_builds_a_valid_receipt_that_binds_its_evidence(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, tools/run_checkpoint.py -> build_receipt -> validate, on a suite whose
    browser-like command keeps its evidence in ``{run_dir}/<name>-evidence`` (review cp10-browser
    R3-F1 and R3-F5): the receipt is valid, and the boundary evidence is bound by digest."""
    import run_checkpoint

    for name in ("run_check.py", "tree_manifest.py"):
        shutil.copyfile(REPO / "tools" / name, root / "tools" / name)
    stub = (
        "import pathlib, sys; folder = pathlib.Path(sys.argv[2]); folder.mkdir(parents=True); "
        "(folder / 'browser-boundary.json').write_text('{\"all_passed\": true}'); "
        "(folder / 'other.txt').write_text('not bound'); print('evidence in', folder)"
    )
    commands = {cid: {cid.lower(): OK} for cid in CP0_CHECKS}
    commands["C00-BOOT"] = {
        "browser-x": [sys.executable, "-c", stub, "--evidence", "{run_dir}/browser-x-evidence"]
    }
    write_suite(root, commands)
    monkeypatch.setattr(run_checkpoint, "ROOT", root)
    monkeypatch.setattr(run_checkpoint, "ENV_REPORT", ENV)
    assert run_checkpoint.main(["CP0", "r1"]) == 0
    rel = "development/receipts/CP0/r1"
    record = json.loads((root / rel / "browser-x.cmd.json").read_text(encoding="utf-8"))
    assert record["argv"][-1] == f"{rel}/browser-x-evidence"  # the same on every machine
    plan = json.loads((root / rel / "receipt-plan.json").read_text(encoding="utf-8"))
    extras = {c["id"]: c.get("extra_evidence") for c in plan["checks"]}
    assert list(extras.pop("C00-BOOT")) == ["browser-x-evidence/browser-boundary.json"]
    assert set(extras.values()) == {None}
    build_receipt.build(rel, root)
    assert validate(root, f"{rel}.json") == []
    receipt = json.loads((root / f"{rel}.json").read_text(encoding="utf-8"))
    bound = {item["path"]: item["sha256"] for item in receipt["evidence"]}
    evidence = f"{rel}/browser-x-evidence/browser-boundary.json"
    assert bound[evidence] == verify_receipt.sha256_file(root / evidence)
    boot = next(c for c in receipt["checks"] if c["id"] == "C00-BOOT")
    assert boot["evidence_paths"] == [evidence]
    assert not any(path.endswith("other.txt") for path in bound)
    # The same records presented as another run: the command names r1's folder, not r2's.
    shutil.copytree(root / rel, root / "development/receipts/CP0/r2")
    build_receipt.build("development/receipts/CP0/r2", root)
    errors = validate(root, "development/receipts/CP0/r2.json")
    assert has(errors, "C00-BOOT: commands differ from the declared checkpoint suite")
    # The boundary evidence is bound: changing it afterwards invalidates the receipt.
    (root / evidence).write_text('{"all_passed": false}', encoding="utf-8")
    assert has(validate(root, f"{rel}.json"), "evidence digest mismatch")


def test_changed_specification_makes_receipt_stale(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    edit(root, rel, lambda d: d.update(spec_manifest_sha256="a" * 64))
    assert has(validate(root, rel), "stale spec")
    rel2 = make_run(root, run_id="r2", review=ACCEPT)
    with (root / "spec/01-product.md").open("a", encoding="utf-8") as handle:
        handle.write("\nUnreviewed edit.\n")
    assert has(validate(root, rel2), "sealed specification file changed")


def test_dependency_must_be_accepted_and_current(root: Path) -> None:
    cp1 = {
        cid: [cid.lower()]
        for cid in ["C01-TXN", "C01-SOURCE", "C01-RECOVERY", "C01-RESTORE", "C01-PATH"]
    }
    rel1 = make_run(root, cp="CP1", run_id="c1", checks=cp1, review=ACCEPT)
    assert validate(root, rel1) == []
    assert has(verify_receipt.promote(root, rel1), "dependency CP0 is not technically passed")
    rel0 = make_run(root, run_id="p", review=None)
    assert verify_receipt.promote(root, rel0) == []
    assert has(verify_receipt.promote(root, rel1), "dependency CP0 is not technically passed")
    rel0b = make_run(root, run_id="a", review=ACCEPT)
    assert verify_receipt.promote(root, rel0b) == []
    assert verify_receipt.promote(root, rel1) == []


def test_external_automated_pass_and_unjustified_blocks_rejected(root: Path) -> None:
    checks = {cid: [cid.lower()] for cid in ["C03-PARITY", "C03-TASK", "C03-SKILL"]}
    external = [{"id": "E01", "status": "passed", "method": "automated", "summary": "fixture host"}]
    rel = make_run(root, cp="CP3", run_id="e", checks=checks, external=external)
    assert has(validate(root, rel), "external gates need live/field/manual evidence")
    edit(
        root,
        rel,
        lambda d: d["external_checks"][0].update(status="blocked_external", limitations=[]),
    )
    assert has(validate(root, rel), "record the exact missing evidence")


def test_required_check_cannot_be_not_applicable(root: Path) -> None:
    rel = make_run(root)
    edit(root, rel, lambda d: d["checks"][0].update(status="not_applicable"))
    assert has(validate(root, rel), "cannot be not_applicable")


def test_template_and_misplaced_receipts_rejected(root: Path) -> None:
    template_dir = root / "development/receipts/CP0"
    template_dir.mkdir(parents=True)
    shutil.copyfile(
        root / "templates/receipt-template.json", template_dir / "TEMPLATE-DO-NOT-SUBMIT.json"
    )
    errors = validate(root, "development/receipts/CP0/TEMPLATE-DO-NOT-SUBMIT.json")
    assert errors
    rel = make_run(root, review=ACCEPT)
    misplaced = root / "development/receipts/CP1/r1.json"
    misplaced.parent.mkdir(parents=True)
    shutil.copyfile(root / rel, misplaced)
    assert has(validate(root, "development/receipts/CP1/r1.json"), "must be saved at")


def test_duplicate_keys_in_receipt_rejected(root: Path) -> None:
    rel = make_run(root)
    text = (root / rel).read_text(encoding="utf-8")
    (root / rel).write_text(text.replace('"run_id"', '"run_id": "x", "run_id"', 1))
    assert has(validate(root, rel), "duplicate JSON key")


def test_receipts_are_never_overwritten(root: Path) -> None:
    rel = make_run(root)
    with pytest.raises(SystemExit):
        build_receipt.build(rel.removesuffix(".json"), root)


def test_tree_manifest_scope(root: Path) -> None:
    (root / "development/receipts/CP0").mkdir(parents=True)
    (root / "development/receipts/CP0/x.json").write_text("{}", encoding="utf-8")
    (root / "development/decisions").mkdir()
    (root / "development/decisions/0001.md").write_text("d", encoding="utf-8")
    (root / "development/dependencies.json").write_text("{}", encoding="utf-8")
    (root / "src/__pycache__").mkdir()
    (root / "src/__pycache__/app.pyc").write_bytes(b"x")
    (root / "local.sqlite").write_bytes(b"x")
    (root / "dogfood/Some Project").mkdir(parents=True)
    (root / "dogfood/Some Project/model.slx").write_bytes(b"owner research")
    paths = {row["path"] for row in tree_manifest.build_manifest(root)["files"]}
    assert not any(p.startswith("dogfood/") for p in paths)
    assert "src/app.py" in paths
    assert "development/dependencies.json" in paths
    assert "development/decisions/0001.md" in paths
    assert not any(p.startswith("development/receipts") for p in paths)
    assert "development/state.json" not in paths
    assert not any("__pycache__" in p or p.endswith(".sqlite") for p in paths)
    before = tree_manifest.tree_sha256(root)
    (root / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert tree_manifest.tree_sha256(root) != before


def test_backdating_cannot_disable_rules(root: Path) -> None:
    rel = make_run(root)
    record_path = root / rel.removesuffix(".json") / "c00-boot.cmd.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("tree_sha256")  # what a pre-rule command record looked like
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def rehash(d: dict[str, Any], created: str) -> None:
        d["created_at"] = created
        for item in d["evidence"]:
            item["sha256"] = verify_receipt.sha256_file(root / item["path"])

    edit(root, rel, lambda d: rehash(d, "2026-10-04T00:00:00Z"))
    errors = validate(root, rel)
    assert has(errors, "different tree than the candidate")  # still enforced
    assert has(errors, "dated before its own command finished")


def test_grandfathering_is_bound_to_path_and_tree(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = make_run(root)
    record_path = root / rel.removesuffix(".json") / "c00-boot.cmd.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("tree_sha256")
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def rehash(d: dict[str, Any]) -> None:
        for item in d["evidence"]:
            item["sha256"] = verify_receipt.sha256_file(root / item["path"])

    edit(root, rel, rehash)
    tree = json.loads((root / rel).read_text())["candidate"]["tree_sha256"]
    monkeypatch.setitem(verify_receipt.GRANDFATHERED, "command_tree_binding", {(rel, tree)})
    assert validate(root, rel) == []
    monkeypatch.setitem(verify_receipt.GRANDFATHERED, "command_tree_binding", {(rel, "0" * 64)})
    assert has(validate(root, rel), "different tree than the candidate")


def test_implementation_trees_must_declare_every_automated_check(root: Path) -> None:
    (root / "tools/verify_receipt.py").write_text("# stand-in verifier\n", encoding="utf-8")
    rel = make_run(root)
    assert has(validate(root, rel), "has no tools/checkpoint_suites.json")
    suite = {
        "schema_version": 1,
        "suites": {"CP0": {"C00-BOOT": {"summary": "s", "commands": {"x": OK}}}},
    }
    (root / "tools/checkpoint_suites.json").write_text(json.dumps(suite), encoding="utf-8")
    rel2 = make_run(root, run_id="r2", suite=True)
    errors = validate(root, rel2)
    assert has(errors, "C00-CONTRACT: automated check is not declared")
    assert not has(errors, "C00-BOOT")


def test_command_record_without_end_time_rejected(root: Path) -> None:
    rel = make_run(root)
    record_path = root / rel.removesuffix(".json") / "c00-boot.cmd.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("ended_at")
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def rehash(d: dict[str, Any]) -> None:
        for item in d["evidence"]:
            item["sha256"] = verify_receipt.sha256_file(root / item["path"])

    edit(root, rel, rehash)
    assert has(validate(root, rel), "has no ended_at")


# ----- specification amendments (decision 0008) --------------------------------


def archive_spec(root: Path) -> str:
    """Archive the fixture's current sealed spec the way tools/seal_package.py does."""
    version = json.loads((root / "checkpoints.json").read_text(encoding="utf-8"))["spec_version"]
    dest = root / verify_receipt.SPEC_HISTORY / version
    for rel in ("package-manifest.json", *verify_receipt.ARCHIVED_SPEC_FILES):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dest / rel)
    return f"{verify_receipt.SPEC_HISTORY}/{version}/package-manifest.json"


def amend_spec(root: Path, version: str, mutate: Any = None) -> tuple[str, str]:
    """Archive the current spec, change it (text plus optional graph edit), reseal, archive."""
    old_rel = archive_spec(root)
    graph = json.loads((root / "checkpoints.json").read_text(encoding="utf-8"))
    graph["spec_version"] = version
    if mutate is not None:
        mutate(graph)
    (root / "checkpoints.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")
    with (root / "spec/01-product.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\nAmendment to {version}.\n")
    manifest = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    manifest["spec_version"] = version
    for item in manifest["files"]:
        item["sha256"] = verify_receipt.sha256_file(root / item["path"])
    (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return old_rel, archive_spec(root)


def write_amendment(
    root: Path,
    old_rel: str,
    new_rel: str,
    name: str = "a1",
    verdict: dict[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """Write an amendment record with a reviewer verdict bound to its digest."""

    def sealed(rel: str) -> dict[str, str]:
        files = json.loads((root / rel).read_text(encoding="utf-8"))["files"]
        return {f["path"]: f["sha256"] for f in files}

    old, new = sealed(old_rel), sealed(new_rel)
    history = verify_receipt.SPEC_HISTORY
    (root / history / "amendments").mkdir(parents=True, exist_ok=True)
    (root / history / "reviews").mkdir(parents=True, exist_ok=True)
    (root / "development/decisions").mkdir(parents=True, exist_ok=True)
    (root / "development/decisions/0099-amendment.md").write_text("decision", encoding="utf-8")
    rel = f"{history}/amendments/{name}.json"
    review_rel = f"{history}/reviews/{name}.review.json"
    amendment = {
        "schema_version": 1,
        "kind": "prometheos-spec-amendment",
        "from_spec_version": old_rel.split("/")[-2],
        "from_manifest": old_rel,
        "from_manifest_sha256": verify_receipt.sha256_file(root / old_rel),
        "to_spec_version": new_rel.split("/")[-2],
        "to_manifest": new_rel,
        "to_manifest_sha256": verify_receipt.sha256_file(root / new_rel),
        "decision_record": "development/decisions/0099-amendment.md",
        "changed_sealed_paths": sorted(
            p for p in old.keys() | new.keys() if old.get(p) != new.get(p)
        ),
        "affected_check_ids": [],
        "rationale": "test amendment",
        "author_id": "impl-a",
        "reviewer_id": "reviewer-b",
        "review_status": "accepted",
        "review_evidence_paths": [review_rel],
    }
    amendment.update(overrides)
    (root / rel).write_text(json.dumps(amendment), encoding="utf-8")
    review = {
        "kind": "prometheos-spec-amendment-review",
        "amendment_path": rel,
        "amendment_sha256": verify_receipt.sha256_file(root / rel),
        "reviewer_id": amendment["reviewer_id"],
        "status": amendment["review_status"],
        "summary": "reviewed amendment",
    }
    review.update(verdict or {})
    (root / review_rel).write_text(json.dumps(review), encoding="utf-8")
    return rel


def amendment_errors(root: Path, rel: str) -> list[str]:
    return verify_receipt.validate_amendment(root, rel)[1]


def test_old_spec_receipt_needs_reviewed_unaffected_amendment(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0")
    assert has(validate(root, rel), "no accepted amendment chain carries CP0")

    pending = write_amendment(root, old_rel, new_rel, review_status="pending", reviewer_id=None)
    assert amendment_errors(root, pending) == []
    assert has(validate(root, rel), "no accepted amendment chain")

    write_amendment(root, old_rel, new_rel, affected_check_ids=["C00-RECEIPT"])
    assert has(validate(root, rel), "no accepted amendment chain")

    good = write_amendment(root, old_rel, new_rel)
    assert validate(root, rel) == []
    assert verify_receipt.main(["--root", str(root), "amendment", good]) == 0


def test_amendment_record_is_bound_to_real_manifests_and_review(root: Path) -> None:
    make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0")
    bad = write_amendment(root, old_rel, new_rel, changed_sealed_paths=["spec/02-architecture.md"])
    assert has(amendment_errors(root, bad), "changed_sealed_paths disagree")
    bad = write_amendment(root, old_rel, new_rel, from_manifest_sha256="a" * 64)
    assert has(amendment_errors(root, bad), "from_manifest digest differs")
    bad = write_amendment(root, old_rel, new_rel, to_manifest="package-manifest.json")
    assert has(amendment_errors(root, bad), "to_manifest must be")
    bad = write_amendment(root, old_rel, new_rel, affected_check_ids=["C99-NOPE"])
    assert has(amendment_errors(root, bad), "unknown check id C99-NOPE")
    for reviewer in ("IMPL-A", "impl.a"):
        bad = write_amendment(root, old_rel, new_rel, reviewer_id=reviewer)
        assert has(amendment_errors(root, bad), "amendment self-review")
    bad = write_amendment(root, old_rel, new_rel, verdict={"amendment_sha256": "0" * 64})
    assert has(amendment_errors(root, bad), "changed after review")
    bad = write_amendment(root, old_rel, new_rel, verdict={"kind": "prometheos-impact-review"})
    assert has(amendment_errors(root, bad), "wrong kind")
    bad = write_amendment(root, old_rel, new_rel, decision_record="development/decisions/none.md")
    assert has(amendment_errors(root, bad), "decision_record does not exist")


def test_old_spec_receipt_is_revalidated_under_its_archived_spec(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0")
    write_amendment(root, old_rel, new_rel)
    assert validate(root, rel) == []

    # Evidence tampering is still caught, reported against the spec the receipt used.
    run_dir = root / rel.removesuffix(".json")
    (run_dir / "c00-boot.stdout.txt").write_text("forged\n", encoding="utf-8")
    assert has(validate(root, rel), f"under spec {SPEC_VERSION}: evidence digest mismatch")


def test_tampered_or_missing_archive_makes_receipt_stale(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0")
    write_amendment(root, old_rel, new_rel)
    archived = root / verify_receipt.SPEC_HISTORY / SPEC_VERSION / "checkpoints.json"
    original = archived.read_bytes()
    graph = json.loads(original)
    graph["checkpoints"][0]["required_checks"] = ["C00-BOOT"]
    archived.write_text(json.dumps(graph), encoding="utf-8")
    assert has(validate(root, rel), "archived spec file differs from its seal")
    archived.write_bytes(original)
    shutil.rmtree(archived.parent)
    assert has(validate(root, rel), f"no archived spec {SPEC_VERSION}")


def test_amendment_adding_a_required_check_makes_receipt_stale(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)

    def add_check(graph: dict[str, Any]) -> None:
        graph["checks"].append({"id": "C00-NEW", "checkpoint": "CP0", "criterion": "new"})
        graph["checkpoints"][0]["required_checks"].append("C00-NEW")

    old_rel, new_rel = amend_spec(root, "9.0.0", add_check)
    write_amendment(root, old_rel, new_rel)
    assert has(validate(root, rel), "adds checks ['C00-NEW'] to CP0")


def test_receipt_spec_must_match_its_archive_and_versions_chain(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    old_rel, mid_rel = amend_spec(root, "9.0.0")
    write_amendment(root, old_rel, mid_rel, name="a1")
    mid_again, new_rel = amend_spec(root, "9.1.0")
    assert mid_again == mid_rel
    assert has(validate(root, rel), "no accepted amendment chain")  # current -> 9.1.0 missing
    write_amendment(root, mid_rel, new_rel, name="a2")
    assert validate(root, rel) == []  # carried along current -> 9.0.0 -> 9.1.0
    edit(root, rel, lambda d: d.update(spec_manifest_sha256="b" * 64))
    assert has(validate(root, rel), "not the archived one")


def test_promotion_ignores_gates_moved_to_another_checkpoint(root: Path) -> None:
    def move_e07_to_cp0(graph: dict[str, Any]) -> None:
        for cp in graph["checkpoints"]:
            cp["external_gates"] = [g for g in cp["external_gates"] if g != "E07"]
        graph["checkpoints"][0]["external_gates"].append("E07")
        next(g for g in graph["external_gates"] if g["id"] == "E07").update(
            checkpoint="CP0", required_for_full_product=False
        )

    _, _ = amend_spec(root, "8.0.0", move_e07_to_cp0)
    external = [
        {
            "id": "E07",
            "status": "not_applicable",
            "method": "manual",
            "summary": "no connector",
            "limitations": ["no connector in this version"],
        }
    ]
    rel = make_run(root, review=ACCEPT, external=external)
    assert validate(root, rel) == []

    def move_back(graph: dict[str, Any]) -> None:
        cp0_gates = graph["checkpoints"][0]["external_gates"]
        cp0_gates.remove("E07")
        next(cp for cp in graph["checkpoints"] if cp["id"] == "CP9")["external_gates"].append("E07")
        next(g for g in graph["external_gates"] if g["id"] == "E07").update(
            checkpoint="CP9", required_for_full_product=True
        )

    old_rel, new_rel = amend_spec(root, "9.0.0", move_back)
    write_amendment(root, old_rel, new_rel)
    impact = base_impact(root, rel)
    from_manifest = json.loads((root / impact["from_manifest"]).read_text(encoding="utf-8"))
    to_manifest = json.loads((root / impact["to_manifest"]).read_text(encoding="utf-8"))
    write_impact(
        root, dict(impact, changed_paths=tree_manifest.diff_paths(from_manifest, to_manifest))
    )
    assert verify_receipt.promote(root, rel) == []
    assert state(root)["external_gates"]["E07"]["status"] == "not_run"
    assert state(root)["checkpoints"]["CP0"]["receipt"] == rel


# Regressions from review a01 (D1-D5).


@pytest.mark.parametrize("reviewer", [None, "", "---"])
def test_amendment_and_impact_need_a_named_reviewer(root: Path, reviewer: str | None) -> None:
    rel = make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0")
    bad = write_amendment(root, old_rel, new_rel, reviewer_id=reviewer)
    assert has(amendment_errors(root, bad), "amendment review without reviewer_id")
    assert has(validate(root, rel), "no accepted amendment chain")

    rel2 = make_run(root, run_id="r2", review=ACCEPT)
    (root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    impact = write_impact(root, dict(base_impact(root, rel2), reviewer_id=reviewer or ""))
    errors = impact_errors(root, impact)
    assert errors  # "" fails the schema already; "---" reaches the normalised check
    if reviewer == "---":
        assert has(errors, "impact review without reviewer_id")
    assert has(verify_receipt.promote(root, rel2), "not the current tree")


def test_receipt_spec_binding_must_match_its_candidate_tree(root: Path) -> None:
    old_rel, new_rel = amend_spec(root, "9.0.0")
    write_amendment(root, old_rel, new_rel)
    rel = make_run(root, review=ACCEPT)  # really ran under 9.0.0
    assert validate(root, rel) == []

    def relabel(d: dict[str, Any]) -> None:  # claim the older, carried spec instead
        d["spec_version"] = SPEC_VERSION
        d["spec_manifest_sha256"] = verify_receipt.sha256_file(root / old_rel)

    edit(root, rel, relabel)
    assert has(validate(root, rel), "is not the package-manifest.json of the candidate tree")


def test_carry_refused_when_gates_are_added_or_made_required(root: Path) -> None:
    def gate_on_cp0(required: bool) -> Any:
        def mutate(graph: dict[str, Any]) -> None:
            for cp in graph["checkpoints"]:
                cp["external_gates"] = [g for g in cp["external_gates"] if g != "E07"]
            graph["checkpoints"][0]["external_gates"].append("E07")
            next(g for g in graph["external_gates"] if g["id"] == "E07").update(
                checkpoint="CP0", required_for_full_product=required
            )

        return mutate

    rel = make_run(root, review=ACCEPT)
    old_rel, new_rel = amend_spec(root, "9.0.0", gate_on_cp0(False))
    write_amendment(root, old_rel, new_rel)
    assert has(validate(root, rel), "adds gates ['E07'] to CP0")

    external = [
        {
            "id": "E07",
            "status": "not_applicable",
            "method": "manual",
            "summary": "optional then",
            "limitations": ["optional in 9.0.0"],
        }
    ]
    rel2 = make_run(root, run_id="r2", review=ACCEPT, external=external)
    old_rel, new_rel = amend_spec(root, "9.1.0", gate_on_cp0(True))
    write_amendment(root, old_rel, new_rel, name="a2")
    assert has(validate(root, rel2), "changes the requirement of E07")


def test_rewritten_check_definition_must_be_declared_affected(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)

    def rewrite(graph: dict[str, Any]) -> None:
        check = next(c for c in graph["checks"] if c["id"] == "C00-RECEIPT")
        check["criterion"] = "Something weaker."

    old_rel, new_rel = amend_spec(root, "9.0.0", rewrite)
    bad = write_amendment(root, old_rel, new_rel)
    assert has(amendment_errors(root, bad), "C00-RECEIPT definition changed")
    assert has(validate(root, rel), "no accepted amendment chain")
    good = write_amendment(root, old_rel, new_rel, affected_check_ids=["C00-RECEIPT"])
    assert amendment_errors(root, good) == []
    assert has(validate(root, rel), "no accepted amendment chain")  # affected: rerun needed


def run_seal(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "tools/seal_package.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_sealing_a_new_version_requires_the_previous_one_archived(root: Path) -> None:
    graph_path = root / "checkpoints.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    version = graph["spec_version"]
    graph["spec_version"] = "9.0.0"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    result = run_seal(root)
    assert result.returncode != 0 and f"spec {version} is not archived" in result.stderr
    assert not (root / verify_receipt.SPEC_HISTORY / version).exists()

    graph["spec_version"] = version  # undo the edit, archive first, then amend
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert run_seal(root, "--archive-current").returncode != 0  # file no longer matches seal
    original = json.loads((REPO / "checkpoints.json").read_text(encoding="utf-8"))
    original_bytes = (REPO / "checkpoints.json").read_bytes()
    graph_path.write_bytes(original_bytes)
    assert run_seal(root, "--archive-current").returncode == 0
    archived = root / verify_receipt.SPEC_HISTORY / original["spec_version"]
    assert (archived / "package-manifest.json").read_bytes() == (
        root / "package-manifest.json"
    ).read_bytes()
    original["spec_version"] = "9.0.0"
    graph_path.write_text(json.dumps(original), encoding="utf-8")
    sealed = run_seal(root)
    assert sealed.returncode == 0, sealed.stderr
    assert (root / verify_receipt.SPEC_HISTORY / "9.0.0/package-manifest.json").is_file()


def test_archived_spec_without_check_list_fails_closed(root: Path) -> None:
    checks = json.loads((REPO / "checkpoints.json").read_text(encoding="utf-8"))["checks"]
    _, mid_rel = amend_spec(root, "9.0.0", lambda graph: graph.pop("checks"))
    _, new_rel = amend_spec(root, "9.1.0", lambda graph: graph.update(checks=checks))
    bad = write_amendment(root, mid_rel, new_rel)
    assert has(amendment_errors(root, bad), "archived checkpoints.json has no check list")


def test_build_receipt_refuses_a_spec_resealed_after_the_run(root: Path) -> None:
    rel = make_run(root, review=ACCEPT)
    (root / rel).unlink()
    amend_spec(root, "9.0.0")
    with pytest.raises(SystemExit, match="resealed since this run"):
        build_receipt.build(rel.removesuffix(".json"), root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", "missing"),
        ("started_at", None),
        ("started_at", 123),
        ("started_at", True),
        ("started_at", []),
        ("started_at", ""),
        ("started_at", "not-a-time"),
        ("started_at", "2026-10-08T10:00:00"),
        ("ended_at", "missing"),
        ("ended_at", None),
        ("ended_at", 123),
        ("ended_at", True),
        ("ended_at", []),
        ("ended_at", ""),
        ("ended_at", "not-a-time"),
        ("ended_at", "2026-10-08T10:00:00"),
        ("started_at", "after-end"),
        ("ended_at", "after-receipt"),
    ],
)
def test_environment_chronology_refuses_hash_consistent_fault_in_both_paths(
    root: Path, field: str, value: Any
) -> None:
    rel = make_run(root, run_id="environment-chronology-negative")
    assert validate(root, rel) == []
    record_rel = rel.removesuffix(".json") + "/env.cmd.json"
    record_path = root / record_rel
    record = json.loads(record_path.read_bytes())
    if value == "missing":
        record.pop(field)
    elif value == "after-end":
        record[field] = (
            verify_receipt.parse_time(record["ended_at"]) + timedelta(seconds=1)
        ).isoformat()
    elif value == "after-receipt":
        record[field] = (
            verify_receipt.parse_time(json.loads((root / rel).read_bytes())["created_at"])
            + timedelta(days=1)
        ).isoformat()
    else:
        record[field] = value
    record_path.write_text(json.dumps(record), encoding="utf-8")
    edit(
        root,
        rel,
        lambda receipt: [
            item.update(sha256=verify_receipt.sha256_file(record_path))
            for item in receipt["evidence"]
            if item["path"] == record_rel
        ],
    )
    errors = validate(root, rel)
    (root / rel).unlink()
    builder_error = None
    try:
        build_receipt.build(rel.removesuffix(".json"), root)
    except SystemExit as exc:
        builder_error = str(exc)
    builder_wrote_receipt = (root / rel).exists()
    assert errors and builder_error is not None and not builder_wrote_receipt, (
        f"Hash-consistent {field}={value!r} chronology fault: "
        f"verifier_errors={errors!r}, builder_error={builder_error!r}, "
        f"builder_wrote_receipt={builder_wrote_receipt!r}"
    )
    assert not has(errors, "digest mismatch")


@pytest.mark.parametrize("offset", [0, 3])
def test_environment_chronology_accepts_equal_and_offset_aware_times(
    root: Path, offset: int
) -> None:
    rel = make_run(root, run_id="environment-chronology-positive")
    record_rel = rel.removesuffix(".json") + "/env.cmd.json"
    record_path = root / record_rel
    record = json.loads(record_path.read_bytes())
    ended = verify_receipt.parse_time(record["ended_at"])
    record["started_at"] = ended.astimezone(timezone(timedelta(hours=offset))).isoformat()
    record_path.write_text(json.dumps(record), encoding="utf-8")
    edit(
        root,
        rel,
        lambda receipt: [
            item.update(sha256=verify_receipt.sha256_file(record_path))
            for item in receipt["evidence"]
            if item["path"] == record_rel
        ],
    )
    assert validate(root, rel) == []
    (root / rel).unlink()
    built = build_receipt.build(rel.removesuffix(".json"), root)
    assert built == root / rel
    assert validate(root, rel) == []


def test_non_environment_command_chronology_also_refuses_reversed_times(root: Path) -> None:
    rel = make_run(root, run_id="check-chronology-negative")
    assert validate(root, rel) == []
    record_rel = rel.removesuffix(".json") + "/c00-boot.cmd.json"
    record_path = root / record_rel
    record = json.loads(record_path.read_bytes())
    record["started_at"] = (
        verify_receipt.parse_time(record["ended_at"]) + timedelta(seconds=1)
    ).isoformat()
    record_path.write_text(json.dumps(record), encoding="utf-8")
    edit(
        root,
        rel,
        lambda receipt: [
            item.update(sha256=verify_receipt.sha256_file(record_path))
            for item in receipt["evidence"]
            if item["path"] == record_rel
        ],
    )
    errors = validate(root, rel)
    assert errors, "The shared guard accepted reversed ordinary-check chronology"
    assert not has(errors, "digest mismatch")


def test_identical_passing_records_are_reused_within_one_integration_run(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Decision 0037: a command whose argv equals a passing record of the same tree (in this run
    or an earlier run folder named by --reuse-from) is copied, not rerun, with ``reused_from``;
    the receipt still validates. Run-specific ({run_dir}) commands, failed records and records of
    another tree always run fresh."""
    import run_checkpoint

    for name in ("run_check.py", "tree_manifest.py"):
        shutil.copyfile(REPO / "tools" / name, root / "tools" / name)
    counter = tmp_path_factory.mktemp("outside-the-tree") / "runs.txt"
    expensive = [
        sys.executable,
        "-c",
        f"import pathlib; p = pathlib.Path({str(counter)!r}); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); print('suite passed')",
    ]
    browser = [*ECHO, "--evidence", "{run_dir}/browser-evidence"]
    write_suite(
        root,
        {
            "C00-BOOT": {"boot-full-suite": expensive, "browser": browser},
            "C00-CONTRACT": {"gate-full-suite": expensive},
            "C00-RECEIPT": {"receipt": OK, "browser-again": browser},
        },
    )
    monkeypatch.setattr(run_checkpoint, "ROOT", root)
    monkeypatch.setattr(run_checkpoint, "ENV_REPORT", ENV)
    assert run_checkpoint.main(["CP0", "r1"]) == 0
    assert counter.read_text() == "x"  # the second identical command of r1 reused the first
    r1 = "development/receipts/CP0/r1"
    again = json.loads((root / r1 / "browser-again.cmd.json").read_text(encoding="utf-8"))
    assert "reused_from" not in again  # a {run_dir} command runs fresh even within one run
    reused = json.loads((root / r1 / "gate-full-suite.cmd.json").read_text(encoding="utf-8"))
    assert reused["reused_from"] == f"{r1}/boot-full-suite.cmd.json"
    assert reused["name"] == "gate-full-suite" and reused["stdout"]["file"].startswith("gate-")
    assert (root / r1 / "gate-full-suite.stdout.txt").read_text().strip() == "suite passed"

    assert run_checkpoint.main(["CP0", "r2", "--reuse-from", r1]) == 0
    assert counter.read_text() == "x"  # nothing expensive ran again
    r2 = "development/receipts/CP0/r2"
    for name in ("boot-full-suite", "gate-full-suite", "receipt"):
        record = json.loads((root / r2 / f"{name}.cmd.json").read_text(encoding="utf-8"))
        assert record["reused_from"].startswith(f"{r1}/"), name  # always the original, never a copy
    browser_record = json.loads((root / r2 / "browser.cmd.json").read_text(encoding="utf-8"))
    assert "reused_from" not in browser_record  # run-specific evidence runs fresh
    build_receipt.build(r2, root)
    assert validate(root, f"{r2}.json") == []

    # The verifier follows reused_from to the original's bytes (review int6 D37-F1).
    source_out = root / r1 / "boot-full-suite.stdout.txt"
    original = source_out.read_bytes()
    source_out.write_bytes(b"tampered\n")
    assert has(validate(root, f"{r2}.json"), "no longer matches its digest")
    source_out.write_bytes(original)
    assert validate(root, f"{r2}.json") == []

    # A source bound to another tree, and a deleted source, are each reported (D38-F3).
    source_record = root / r1 / "boot-full-suite.cmd.json"
    kept = source_record.read_bytes()
    changed = json.loads(kept)
    changed["tree_sha256"] = "f" * 64
    source_record.write_text(json.dumps(changed), encoding="utf-8")
    assert has(validate(root, f"{r2}.json"), "tree_sha256 differs from its source")
    source_record.unlink()
    assert has(validate(root, f"{r2}.json"), "reused_from record unreadable")
    source_record.write_bytes(kept)
    assert validate(root, f"{r2}.json") == []

    # Copies are never reused (D37-F2): a run that may reuse only r2's copies runs the suite again.
    assert run_checkpoint.main(["CP0", "r2b", "--reuse-from", r2]) == 0
    assert counter.read_text() == "xx"
    rerun = json.loads(
        (root / "development/receipts/CP0/r2b/boot-full-suite.cmd.json").read_text(encoding="utf-8")
    )
    assert "reused_from" not in rerun

    # A failed record, or one bound to another tree, is never reused.
    for path in (root / r1).glob("*.cmd.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["name"] == "boot-full-suite":
            record["tree_sha256"] = "0" * 64
        if record["name"] == "receipt":
            record["exit_code"] = 1
        path.write_text(json.dumps(record), encoding="utf-8")
    assert run_checkpoint.main(["CP0", "r3", "--reuse-from", r1]) == 0
    assert counter.read_text() == "xxx"  # the full suite ran again, once (then reused in r3)
    r3 = "development/receipts/CP0/r3"
    fresh = json.loads((root / r3 / "receipt.cmd.json").read_text(encoding="utf-8"))
    assert "reused_from" not in fresh
