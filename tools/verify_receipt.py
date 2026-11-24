"""Validate development receipts and derive checkpoint state from them.

This is NOT a product test. It enforces the evidence rules in spec/10-delivery.md
and AGENTS.md: required check coverage, saved artifact digests, command records,
candidate/spec binding, independent review and reviewed impact chains.

Usage:
    uv run python tools/verify_receipt.py validate RECEIPT
    uv run python tools/verify_receipt.py promote RECEIPT
    uv run python tools/verify_receipt.py impact IMPACT [--register]
    uv run python tools/verify_receipt.py status

``promote`` is the only supported way to set a checkpoint's technical/review state:
the state is derived from a valid, current receipt rather than edited by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tree_manifest

ROOT = Path(__file__).resolve().parents[1]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
STATE_PATH = "development/state.json"
# Earlier sealed specifications and the reviewed amendments between them (decision 0008).
SPEC_HISTORY = "development/spec-history"
# Archived with each sealed version: what validating that version's receipts needs.
ARCHIVED_SPEC_FILES = (
    "checkpoints.json",
    "schemas/receipt.schema.json",
    "schemas/impact.schema.json",
)


class ReceiptError(Exception):
    pass


# Rules added after the first receipts existed apply to every receipt except the
# specific historical receipt that predates them, identified by path *and* candidate
# tree digest. A receipt's own created_at cannot opt out of a rule (CP1 review).
_CP0_R1 = (
    "development/receipts/CP0/cp0-20261004-r1.json",
    "5872aee3c2df3475d67263eeb9064180a5b174660c1d7460b8cc01b08bd02445",
)
GRANDFATHERED: dict[str, set[tuple[str, str]]] = {
    "command_tree_binding": {_CP0_R1},
    "declared_suite_match": {_CP0_R1},
    "nonempty_command_output": {_CP0_R1},
    "suite_fail_closed": {_CP0_R1},
}


def rule_applies(rule: str, receipt_rel: str, tree_sha256: str) -> bool:
    return (receipt_rel, tree_sha256) not in GRANDFATHERED.get(rule, set())


# ----- helpers -----------------------------------------------------------------


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ReceiptError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ReceiptError(f"non-finite number {name}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_no_dupes,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{path.name}: invalid JSON: {exc}") from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm_id(value: str | None) -> str:
    """Identity comparison ignoring case and all punctuation/whitespace."""
    return re.sub(r"[^0-9a-z]+", "", (value or "").casefold())


def safe_relative(root: Path, relative: str, within: str | None = None) -> Path:
    """Resolve a repository-relative POSIX path without escapes or links."""
    if not relative or "\\" in relative or ":" in relative or relative.startswith("/"):
        raise ReceiptError(f"evidence path must be relative POSIX: {relative!r}")
    parts = PurePosixPath(relative).parts
    if any(part in ("..", ".", "") for part in parts):
        raise ReceiptError(f"evidence path may not traverse: {relative!r}")
    if within is not None and not relative.startswith(within.rstrip("/") + "/"):
        raise ReceiptError(f"evidence path {relative!r} is outside {within!r}")
    current = root
    for part in parts:
        current = current / part
        if tree_manifest.is_link_or_reparse(current):
            raise ReceiptError(f"evidence path traverses a link/reparse point: {relative!r}")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ReceiptError(f"evidence path escapes repository: {relative!r}")
    return current


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ReceiptError(f"timestamp lacks timezone: {value}")
    return parsed


class Package:
    """Specification inputs: the current package at the repository root, or an archived
    earlier specification under ``development/spec-history/<version>/`` (decision 0008)."""

    def __init__(self, root: Path, spec_dir: Path | None = None) -> None:
        self.root = root
        base = spec_dir or root
        self.graph = load_json(base / "checkpoints.json")
        self.checkpoints = {cp["id"]: cp for cp in self.graph["checkpoints"]}
        self.gates = {g["id"]: g for g in self.graph["external_gates"]}
        self.receipt_schema = load_json(base / "schemas/receipt.schema.json")
        self.impact_schema = load_json(base / "schemas/impact.schema.json")
        manifest_path = base / "package-manifest.json"
        self.spec_manifest_sha256 = sha256_file(manifest_path)
        self.spec_version = self.graph["spec_version"]
        self.seal_errors: list[str] = []
        self.receipt_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}
        self.impact_cache: dict[tuple[str, bool], tuple[dict[str, Any], list[str]]] = {}
        self.amendment_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}
        self.historical: dict[str, tuple[Package | None, list[str]]] = {}
        manifest = load_json(manifest_path)
        if spec_dir is None:
            self.current: Package = self
            for item in manifest["files"]:
                path = root / item["path"]
                if not path.is_file() or sha256_file(path) != item["sha256"]:
                    self.seal_errors.append(f"sealed specification file changed: {item['path']}")
        else:
            sealed = {item["path"]: item["sha256"] for item in manifest["files"]}
            for rel in ARCHIVED_SPEC_FILES:
                path = spec_dir / rel
                if not path.is_file() or sha256_file(path) != sealed.get(rel):
                    self.seal_errors.append(f"archived spec file differs from its seal: {rel}")
            if manifest.get("spec_version") != self.spec_version:
                self.seal_errors.append("archived manifest and checkpoints.json versions differ")


def schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    return [
        f"schema: {'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    ]


# ----- receipt validation ------------------------------------------------------


def _check_command_record(
    root: Path,
    run_dir: str,
    entry: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    errors: list[str],
    label: str,
    candidate_tree: str,
    receipt_rel: str,
    created_at: str,
) -> None:
    records = [p for p in entry["evidence_paths"] if p.endswith(".cmd.json")]
    if len(records) != 1:
        errors.append(f"{label}: command needs exactly one .cmd.json record")
        return
    try:
        record = load_json(safe_relative(root, records[0], run_dir))
    except (ReceiptError, OSError) as exc:
        errors.append(f"{label}: unreadable command record: {exc}")
        return
    if record.get("kind") != "prometheos-command-record":
        errors.append(f"{label}: not a command record")
        return
    if record.get("command") != entry["command"]:
        errors.append(f"{label}: receipt command differs from recorded command")
    if record.get("exit_code") != entry["exit_code"]:
        errors.append(
            f"{label}: receipt exit code {entry['exit_code']} differs from recorded "
            f"{record.get('exit_code')}"
        )
    command_times: dict[str, datetime] = {}
    for field in ("started_at", "ended_at"):
        value = record.get(field)
        if type(value) is not str or not value.strip():
            errors.append(f"{label}: command record has no {field} nonblank string")
            continue
        try:
            command_times[field] = parse_time(value)
        except (ReceiptError, ValueError):
            errors.append(f"{label}: unreadable command timestamps ({field})")
    started = command_times.get("started_at")
    ended = command_times.get("ended_at")
    if started is not None and ended is not None and started > ended:
        errors.append(f"{label}: command started after it finished")
    try:
        if ended is not None and parse_time(created_at) < ended:
            errors.append(f"{label}: receipt is dated before its own command finished")
    except (ReceiptError, ValueError):
        errors.append(f"{label}: unreadable command timestamps (created_at)")
    if (
        rule_applies("command_tree_binding", receipt_rel, candidate_tree)
        and record.get("tree_sha256") != candidate_tree
    ):
        errors.append(f"{label}: command ran against a different tree than the candidate")
    record_dir = records[0].rsplit("/", 1)[0]
    empty = hashlib.sha256(b"").hexdigest()
    outputs = []
    for stream in ("stdout", "stderr"):
        info = record.get(stream) or {}
        rel = f"{record_dir}/{info.get('file')}"
        outputs.append(info.get("sha256"))
        if rel not in entry["evidence_paths"]:
            errors.append(f"{label}: {stream} output not cited as evidence")
        elif rel in evidence and evidence[rel]["sha256"] != info.get("sha256"):
            errors.append(f"{label}: {stream} digest differs from command record")
    if (
        rule_applies("nonempty_command_output", receipt_rel, candidate_tree)
        and entry["exit_code"] == 0
        and all(digest == empty for digest in outputs)
    ):
        errors.append(f"{label}: passing command produced no output at all")
    if "reused_from" in record:
        _check_reused_source(root, record, label, errors)


def _check_reused_source(root: Path, record: dict[str, Any], label: str, errors: list[str]) -> None:
    """A reused record (decision 0037) must name an original command record (in another run
    folder, or in the same one when one run runs the same gate twice) that ran the same command
    on the same tree, passed, and produced the same output bytes (D38-F3)."""
    source_rel = record.get("reused_from")
    if type(source_rel) is not str or not source_rel.endswith(".cmd.json"):
        errors.append(f"{label}: reused_from does not name a command record")
        return
    try:
        source_path = safe_relative(root, source_rel, "development/receipts")
        source = load_json(source_path)
    except (ReceiptError, OSError) as exc:
        errors.append(f"{label}: reused_from record unreadable: {exc}")
        return
    if source.get("kind") != "prometheos-command-record" or "reused_from" in source:
        errors.append(f"{label}: reused_from must name an original command record")
        return
    for field in ("command", "argv", "tree_sha256", "exit_code", "started_at", "ended_at"):
        if source.get(field) != record.get(field):
            errors.append(f"{label}: reused record's {field} differs from its source")
    for stream in ("stdout", "stderr"):
        info = source.get(stream) or {}
        if info.get("sha256") != (record.get(stream) or {}).get("sha256"):
            errors.append(f"{label}: reused {stream} digest differs from its source")
            continue
        try:
            data = safe_relative(root, f"{source_rel.rsplit('/', 1)[0]}/{info.get('file')}")
            if sha256_file(data) != info.get("sha256"):
                errors.append(f"{label}: the source's {stream} file no longer matches its digest")
        except (ReceiptError, OSError):
            errors.append(f"{label}: the source's {stream} file is missing")


def environment_report(
    root: Path,
    run_dir: str,
    evidence: dict[str, dict[str, Any]],
    errors: list[str],
    candidate_tree: str,
    receipt_rel: str,
    created_at: str,
) -> dict[str, Any] | None:
    """Validate and consume one canonical, command-bound environment stdout.

    Shared by the builder and verifier; a separately assembled receipt has the same
    boundary. Historical uncited empty stderr remains byte/digest checked.
    """
    env_rel = f"{run_dir}/env.cmd.json"
    env_out = f"{run_dir}/env.stdout.txt"
    for path in (env_rel, env_out):
        if path not in evidence:
            errors.append(f"environment report: {path} is not listed in evidence")
    try:
        record = load_json(safe_relative(root, env_rel, run_dir))
        command, argv = record.get("command"), record.get("argv")
        valid_argv = (
            type(argv) is list
            and bool(argv)
            and all(type(part) is str and bool(part.strip()) for part in argv)
        )
        if type(command) is not str or not command.strip():
            errors.append("environment report has no valid command")
        elif not valid_argv:
            errors.append("environment report has no valid argv")
        elif command != shlex.join(argv):
            errors.append("environment report command differs from argv")
        if (
            record.get("name") != "env"
            or record.get("cwd") != "."
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1
        ):
            errors.append("environment report has invalid command semantics")
        if type(record.get("exit_code")) is not int or record["exit_code"] != 0:
            errors.append("environment report did not exit 0")
        paths = [env_rel]
        bound = dict(evidence)
        canonical_stdout = False
        for stream in ("stdout", "stderr"):
            info = record.get(stream) or {}
            expected_file = f"env.{stream}.txt"
            if info.get("file") != expected_file:
                errors.append(f"environment report {stream} must be {expected_file}")
            elif stream == "stdout":
                canonical_stdout = True
            stream_rel = f"{run_dir}/{info.get('file')}"
            stream_path = safe_relative(root, stream_rel, run_dir)
            paths.append(stream_rel)
            if stream_rel not in bound:
                if stream != "stderr" or stream_path.read_bytes() != b"":
                    errors.append(f"environment report: {stream_rel} is not listed in evidence")
                bound[stream_rel] = {"sha256": sha256_file(stream_path)}
        _check_command_record(
            root,
            run_dir,
            {"command": command, "exit_code": record.get("exit_code"), "evidence_paths": paths},
            bound,
            errors,
            "environment report",
            candidate_tree,
            receipt_rel,
            created_at,
        )
        if canonical_stdout:
            env = load_json(safe_relative(root, env_out, run_dir))
            if type(env) is not dict:
                errors.append("environment report stdout must be an object")
            else:
                return env
    except (ReceiptError, OSError, AttributeError, TypeError) as exc:
        errors.append(f"environment report unreadable: {exc}")
    return None


SUITE_PATH = "tools/checkpoint_suites.json"
VERIFIER_PATH = "tools/verify_receipt.py"
# The one argv placeholder of the suite file: the run folder, development/receipts/<CP>/<run-id>.
RUN_DIR_PLACEHOLDER = "{run_dir}"


def _declared_suite(
    root: Path,
    run_dir: str,
    cp_id: str,
    manifest_rel: str,
    evidence: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, list[str]] | None:
    """Commands declared for this checkpoint by the suite file of the candidate tree.

    The run directory keeps a copy (``suite.json``) whose digest must equal the
    candidate manifest entry for tools/checkpoint_suites.json. The suite's only
    placeholder, ``{run_dir}``, stands for this receipt's own repository-relative
    run folder (tools/run_checkpoint.py expands it the same way), so a command
    recorded for another run's folder, or an absolute one, does not match.
    """
    copy_rel = f"{run_dir}/suite.json"
    try:
        manifest = load_json(safe_relative(root, manifest_rel, run_dir))
        tracked = {row["path"]: row["sha256"] for row in manifest.get("files", [])}
    except (ReceiptError, OSError, KeyError, TypeError, AttributeError):
        return None
    if SUITE_PATH not in tracked:
        # Fail closed for real implementation trees (they contain this verifier); only
        # bare specification copies used by tests have no suite at all.
        if VERIFIER_PATH in tracked:
            errors.append("implementation tree has no tools/checkpoint_suites.json")
        return None
    if copy_rel not in evidence:
        errors.append("candidate declares a checkpoint suite but suite.json is not evidence")
        return None
    if evidence[copy_rel]["sha256"] != tracked[SUITE_PATH]:
        errors.append("suite.json is not the candidate's tools/checkpoint_suites.json")
        return None
    try:
        suites = load_json(safe_relative(root, copy_rel, run_dir))["suites"]
    except (ReceiptError, OSError, KeyError, TypeError) as exc:
        errors.append(f"suite.json unreadable: {exc}")
        return None
    return {
        cid: [
            shlex.join(part.replace(RUN_DIR_PLACEHOLDER, run_dir) for part in cmd)
            for cmd in spec["commands"].values()
        ]
        for cid, spec in suites.get(cp_id, {}).items()
    }


def validate_receipt(
    root: Path, receipt_rel: str, package: Package | None = None
) -> tuple[dict[str, Any], list[str]]:
    package = package or Package(root)
    if receipt_rel not in package.receipt_cache:
        package.receipt_cache[receipt_rel] = _validate_receipt(root, receipt_rel, package)
    receipt, errors = package.receipt_cache[receipt_rel]
    return receipt, list(errors)


def _validate_receipt(
    root: Path, receipt_rel: str, package: Package
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = list(package.seal_errors)
    try:
        path = safe_relative(root, receipt_rel, "development/receipts")
        receipt = load_json(path)
    except (ReceiptError, OSError) as exc:
        return {}, [f"cannot load receipt: {exc}"]
    shape = schema_errors(package.receipt_schema, receipt)
    if shape:
        return receipt, shape
    cp_id = receipt["checkpoint_id"]
    run_id = receipt["run_id"]
    if not RUN_ID_RE.fullmatch(run_id):
        errors.append(f"unsafe run_id: {run_id!r}")
    expected_path = f"development/receipts/{cp_id}/{run_id}.json"
    if receipt_rel != expected_path:
        errors.append(f"receipt must be saved at {expected_path}")
    run_dir = f"development/receipts/{cp_id}/{run_id}"
    checkpoint = package.checkpoints.get(cp_id)
    if checkpoint is None:
        return receipt, [*errors, f"unknown checkpoint {cp_id}"]
    if (
        receipt["spec_manifest_sha256"] != package.spec_manifest_sha256
        and receipt["spec_version"] != package.spec_version
        and package.current is package
    ):
        # Bound to an earlier sealed specification: validate it in full against that
        # archived specification, then require a reviewed amendment chain to this one.
        historical, hist_errors = historical_package(package, receipt["spec_version"])
        if historical is None:
            return receipt, [*errors, *hist_errors]
        if historical.spec_manifest_sha256 != receipt["spec_manifest_sha256"]:
            return receipt, [
                *errors,
                f"receipt is bound to a spec {receipt['spec_version']} manifest that is not "
                "the archived one (stale spec)",
            ]
        _, then_errors = validate_receipt(root, receipt_rel, historical)
        if then_errors:
            return receipt, [
                *errors,
                *(f"under spec {historical.spec_version}: {e}" for e in then_errors),
            ]
        return receipt, [*errors, *spec_carry_errors(package, historical, receipt)]
    if receipt["spec_version"] != package.spec_version:
        errors.append("receipt spec_version differs from checkpoints.json")
    if receipt["spec_manifest_sha256"] != package.spec_manifest_sha256:
        errors.append("receipt is bound to a different specification manifest (stale spec)")
    try:
        created = parse_time(receipt["created_at"])
        if created > datetime.now(UTC) + timedelta(minutes=10):
            errors.append("receipt created_at is in the future")
    except (ReceiptError, ValueError) as exc:
        errors.append(f"bad created_at: {exc}")

    # Evidence inventory: every artifact hashed, inside the run directory.
    evidence: dict[str, dict[str, Any]] = {}
    for item in receipt["evidence"]:
        rel = item["path"]
        if rel in evidence:
            errors.append(f"duplicate evidence path {rel}")
            continue
        evidence[rel] = item
        try:
            file_path = safe_relative(root, rel, run_dir)
        except ReceiptError as exc:
            errors.append(str(exc))
            continue
        if not file_path.is_file():
            errors.append(f"missing evidence artifact {rel}")
        elif file_path.stat().st_size == 0 and not rel.endswith((".stderr.txt", ".stdout.txt")):
            errors.append(f"empty evidence artifact {rel}")
        elif sha256_file(file_path) != item["sha256"]:
            errors.append(f"evidence digest mismatch {rel}")

    def cited(paths: list[str], label: str) -> None:
        for rel in paths:
            if rel not in evidence:
                errors.append(f"{label}: {rel} is not listed in evidence with a digest")

    # Candidate binding.
    candidate = receipt["candidate"]
    manifest_rel = candidate["tree_manifest_path"]
    cited([manifest_rel], "candidate")
    if manifest_rel in evidence:
        if evidence[manifest_rel]["sha256"] != candidate["tree_sha256"]:
            errors.append("candidate tree_sha256 is not the digest of its tree manifest")
        try:
            manifest = load_json(safe_relative(root, manifest_rel, run_dir))
            if manifest.get("kind") != "prometheos-tree-manifest":
                errors.append("candidate manifest has the wrong kind")
            # The spec a receipt claims must be the sealed manifest in the tree it ran on,
            # not whatever was sealed when the receipt was built (review a01 D2).
            sealed = {row.get("path"): row.get("sha256") for row in manifest.get("files", [])}
            if sealed.get("package-manifest.json") != receipt["spec_manifest_sha256"]:
                errors.append(
                    "spec_manifest_sha256 is not the package-manifest.json of the candidate tree"
                )
        except (ReceiptError, OSError, AttributeError, TypeError) as exc:
            errors.append(f"candidate manifest unreadable: {exc}")

    candidate_tree = candidate["tree_sha256"]
    env = environment_report(
        root, run_dir, evidence, errors, candidate_tree, receipt_rel, receipt["created_at"]
    )
    if env is not None and receipt["environment"] != {
        key: env.get(key) for key in receipt["environment"]
    }:
        errors.append("receipt environment differs from the environment report")
    suite = (
        _declared_suite(root, run_dir, cp_id, manifest_rel, evidence, errors)
        if rule_applies("declared_suite_match", receipt_rel, candidate_tree)
        else None
    )
    fail_closed = rule_applies("suite_fail_closed", receipt_rel, candidate_tree)

    # Check coverage.
    required = list(checkpoint["required_checks"])
    gates = list(checkpoint["external_gates"])
    ids = [c["id"] for c in receipt["checks"]]
    for cid in required:
        if ids.count(cid) != 1:
            errors.append(f"required check {cid} must appear exactly once (found {ids.count(cid)})")
    for cid in ids:
        if cid not in required:
            errors.append(f"check {cid} does not belong to {cp_id}")
    ext_ids = [c["id"] for c in receipt["external_checks"]]
    for gid in gates:
        if ext_ids.count(gid) != 1:
            errors.append(f"external gate {gid} must appear exactly once")
    for gid in ext_ids:
        if gid not in gates:
            errors.append(f"external check {gid} does not belong to {cp_id}")

    for check in receipt["checks"]:
        label = f"check {check['id']}"
        cited(check["evidence_paths"], label)
        for i, command in enumerate(check["commands"]):
            cited(command["evidence_paths"], f"{label} command {i}")
            _check_command_record(
                root,
                run_dir,
                command,
                evidence,
                errors,
                f"{label} cmd {i}",
                candidate_tree,
                receipt_rel,
                receipt["created_at"],
            )
        status, method = check["status"], check["method"]
        if suite is not None and method == "automated":
            if check["id"] in suite:
                declared = sorted(suite[check["id"]])
                if sorted(c["command"] for c in check["commands"]) != declared:
                    errors.append(f"{label}: commands differ from the declared checkpoint suite")
            elif fail_closed:
                errors.append(f"{label}: automated check is not declared in the checkpoint suite")
        if status == "not_applicable":
            errors.append(f"{label}: required technical checks cannot be not_applicable")
        if status == "passed":
            if method == "automated":
                if not check["commands"]:
                    errors.append(f"{label}: passing automated check has no command")
                for command in check["commands"]:
                    if command["exit_code"] != 0:
                        errors.append(f"{label}: passing check has nonzero exit code")
                    if not command["evidence_paths"]:
                        errors.append(f"{label}: passing command has no output evidence")
            else:
                if not (check["observer"] or "").strip():
                    errors.append(f"{label}: {method} pass needs an observer")
                if not check["evidence_paths"]:
                    errors.append(f"{label}: {method} pass needs a result report")

    for check in receipt["external_checks"]:
        label = f"external {check['id']}"
        cited(check["evidence_paths"], label)
        for i, command in enumerate(check["commands"]):
            cited(command["evidence_paths"], f"{label} command {i}")
            _check_command_record(
                root,
                run_dir,
                command,
                evidence,
                errors,
                f"{label} cmd {i}",
                candidate_tree,
                receipt_rel,
                receipt["created_at"],
            )
        gate = package.gates.get(check["id"], {})
        if check["status"] == "passed":
            if check["method"] not in ("live", "field", "manual"):
                errors.append(f"{label}: external gates need live/field/manual evidence")
            if not (check["observer"] or "").strip() or not check["evidence_paths"]:
                errors.append(f"{label}: external pass needs observer and result report")
        if check["status"] == "not_applicable":
            if gate.get("required_for_full_product", True) or gate.get(
                "required_for_publication", False
            ):
                errors.append(f"{label}: only optional gates may be not_applicable")
            if not check["limitations"]:
                errors.append(f"{label}: not_applicable needs a recorded reason")
        if check["status"] in ("blocked_external", "not_run") and not check["limitations"]:
            errors.append(f"{label}: record the exact missing evidence as a limitation")

    statuses = {c["id"]: c["status"] for c in receipt["checks"]}
    all_passed = all(statuses.get(cid) == "passed" for cid in required)
    if receipt["technical_status"] == "passed" and not all_passed:
        errors.append("technical_status passed but not every required check passed")
    if all_passed and receipt["technical_status"] == "failed":
        errors.append("technical_status failed but every required check passed")

    review = receipt["review"]
    cited(review["evidence_paths"], "review")
    implementer = norm_id(receipt["implementer_id"])
    if review["status"] != "pending":
        reviewer = norm_id(review["reviewer_id"])
        if not reviewer:
            errors.append("review without reviewer_id")
        elif reviewer == implementer:
            errors.append("self-review: reviewer_id equals implementer_id")
        verdicts = [p for p in review["evidence_paths"] if p.endswith("review.json")]
        if len(verdicts) != 1:
            errors.append("review needs exactly one reviewer-written review.json")
        else:
            try:
                verdict = load_json(safe_relative(root, verdicts[0], run_dir))
                if verdict.get("reviewer_id") != review["reviewer_id"]:
                    errors.append("review.json reviewer_id differs from receipt")
                if verdict.get("status") != review["status"]:
                    errors.append("review.json verdict differs from receipt review status")
                if verdict.get("run_id") != run_id:
                    errors.append("review.json reviews a different run")
                if norm_id(verdict.get("reviewer_id")) == implementer:
                    errors.append("self-review recorded in review.json")
            except (ReceiptError, OSError) as exc:
                errors.append(f"review.json unreadable: {exc}")
        if review["status"] == "accepted" and receipt["technical_status"] != "passed":
            errors.append("review accepted a candidate whose technical checks did not pass")
    return receipt, errors


# ----- impact chains -----------------------------------------------------------


def _impact_manifest(root: Path, rel: str, digest: str, label: str, errors: list[str]) -> Any:
    try:
        path = safe_relative(root, rel, "development")
        if sha256_file(path) != digest:
            errors.append(f"{label}_manifest digest differs from {label}_tree_sha256")
        manifest = load_json(path)
        if not isinstance(manifest, dict) or manifest.get("kind") != "prometheos-tree-manifest":
            errors.append(f"{label}_manifest is not a tree manifest")
            return None
        if not isinstance(manifest.get("files"), list):
            errors.append(f"{label}_manifest has no file list")
            return None
        return manifest
    except (ReceiptError, OSError) as exc:
        errors.append(f"{label}_manifest unreadable: {exc}")
        return None


def _carried_into(root: Path, package: Package, check_id: str, source: str, tree: str) -> bool:
    """Does an accepted impact ending at ``tree`` carry ``check_id`` from ``source``?

    Checked one level deep: each of those impacts verifies its own carried sources
    when it is itself validated, so the property holds inductively along a chain.
    """
    for _, impact in accepted_impacts(root, package, shallow=True):
        if impact["to_tree_sha256"] != tree:
            continue
        if any(
            c["id"] == check_id and c["source_receipt"] == source for c in impact["carried_checks"]
        ):
            return True
    return False


def validate_impact(
    root: Path, impact_rel: str, package: Package | None = None, shallow: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """Structural and evidence validation of one reviewed impact record."""
    package = package or Package(root)
    key = (impact_rel, shallow)
    if key in package.impact_cache:
        return package.impact_cache[key]
    result = _validate_impact(root, impact_rel, package, shallow)
    package.impact_cache[key] = result
    return result


def _validate_impact(
    root: Path, impact_rel: str, package: Package, shallow: bool
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        impact_path = safe_relative(root, impact_rel, "development/impact")
        impact = load_json(impact_path)
    except (ReceiptError, OSError) as exc:
        return {}, [f"cannot load impact record: {exc}"]
    shape = schema_errors(package.impact_schema, impact)
    if shape:
        return impact, shape
    old = _impact_manifest(
        root, impact["from_manifest"], impact["from_tree_sha256"], "from", errors
    )
    new = _impact_manifest(root, impact["to_manifest"], impact["to_tree_sha256"], "to", errors)
    if old is not None and new is not None:
        try:
            actual = tree_manifest.diff_paths(old, new)
        except (KeyError, TypeError) as exc:
            errors.append(f"malformed manifest entries: {exc}")
            actual = sorted(impact["changed_paths"])
        if sorted(impact["changed_paths"]) != actual:
            missing = sorted(set(actual) - set(impact["changed_paths"]))
            extra = sorted(set(impact["changed_paths"]) - set(actual))
            errors.append(
                f"changed_paths disagree with manifests (missing={missing}, extra={extra})"
            )
    affected = set(impact["affected_check_ids"])
    known_checks = {c for cp in package.checkpoints.values() for c in cp["required_checks"]}
    for cid in sorted(affected | {c["id"] for c in impact["carried_checks"]}):
        if cid not in known_checks:
            errors.append(f"unknown check id {cid}")
    for carried in impact["carried_checks"]:
        if carried["id"] in affected:
            errors.append(f"check {carried['id']} is both affected and carried forward")
        src, src_errors = validate_receipt(root, carried["source_receipt"], package)
        if src_errors:
            errors.append(f"carried {carried['id']}: source receipt invalid: {src_errors[0]}")
        elif not any(c["id"] == carried["id"] and c["status"] == "passed" for c in src["checks"]):
            errors.append(f"carried {carried['id']}: source receipt did not pass that check")
        elif src["review"]["status"] != "accepted":
            errors.append(f"carried {carried['id']}: source receipt review is not accepted")
        elif src["candidate"]["tree_sha256"] != impact["from_tree_sha256"] and not (
            shallow
            or _carried_into(
                root, package, carried["id"], carried["source_receipt"], impact["from_tree_sha256"]
            )
        ):
            errors.append(
                f"carried {carried['id']}: source receipt is neither on from_tree nor carried "
                "to it by an accepted impact record"
            )
    rerun_passes: set[str] = set()
    for rerun in impact["rerun_receipts"]:
        receipt, rerun_errors = validate_receipt(root, rerun, package)
        if rerun_errors:
            errors.append(f"rerun receipt {rerun} invalid: {rerun_errors[0]}")
            continue
        if receipt["candidate"]["tree_sha256"] != impact["to_tree_sha256"]:
            errors.append(f"rerun receipt {rerun} was not run on to_tree")
            continue
        if receipt["review"]["status"] != "accepted":
            errors.append(f"rerun receipt {rerun} is not review-accepted")
            continue
        rerun_passes |= {c["id"] for c in receipt["checks"] if c["status"] == "passed"}
    for cid in sorted(affected - rerun_passes):
        errors.append(f"affected check {cid} has no passing rerun receipt on to_tree")
    if impact["review_status"] != "pending":
        if not norm_id(impact["reviewer_id"]):
            errors.append("impact review without reviewer_id")
        elif norm_id(impact["reviewer_id"]) == norm_id(impact["author_id"]):
            errors.append("impact self-review")
        verdicts = [p for p in impact["review_evidence_paths"] if p.endswith(".review.json")]
        if len(verdicts) != 1:
            errors.append("impact review needs exactly one reviewer-written *.review.json")
        else:
            try:
                verdict = load_json(safe_relative(root, verdicts[0], "development/impact/reviews"))
                if verdict.get("kind") != "prometheos-impact-review":
                    errors.append("impact review file has the wrong kind")
                if verdict.get("impact_path") != impact_rel:
                    errors.append("impact review names a different impact record")
                if verdict.get("impact_sha256") != sha256_file(impact_path):
                    errors.append("impact record changed after review (digest mismatch)")
                if verdict.get("reviewer_id") != impact["reviewer_id"]:
                    errors.append("impact review reviewer_id differs from impact record")
                if verdict.get("status") != impact["review_status"]:
                    errors.append("impact review verdict differs from impact record")
                if norm_id(verdict.get("reviewer_id")) == norm_id(impact["author_id"]):
                    errors.append("impact self-review recorded in review file")
            except (ReceiptError, OSError) as exc:
                errors.append(f"impact review unreadable: {exc}")
    return impact, errors


def accepted_impacts(
    root: Path, package: Package, shallow: bool = False
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    impact_dir = root / "development/impact"
    if not impact_dir.is_dir():
        return out
    for path in sorted(impact_dir.glob("*.json")):
        rel = path.relative_to(root).as_posix()
        impact, errors = validate_impact(root, rel, package, shallow)
        if not errors and impact.get("review_status") == "accepted":
            out.append((rel, impact))
    return out


def _edge_carries(
    root: Path, package: Package, impact: dict[str, Any], required: set[str], receipt_rel: str
) -> bool:
    """Every required check is carried from this receipt or from one on the edge start."""
    if required & set(impact["affected_check_ids"]):
        return False
    carried = {c["id"]: c["source_receipt"] for c in impact["carried_checks"]}
    for cid in required:
        source = carried.get(cid)
        if source is None:
            return False
        if source == receipt_rel:
            continue
        src, src_errors = validate_receipt(root, source, package)
        if src_errors or src["candidate"]["tree_sha256"] != impact["from_tree_sha256"]:
            return False
    return True


def chain_to(
    root: Path,
    package: Package,
    receipt: dict[str, Any],
    target_tree: str,
    receipt_rel: str | None = None,
) -> tuple[bool, str]:
    """Is ``receipt`` carried forward to ``target_tree`` through accepted impacts?"""
    start = receipt["candidate"]["tree_sha256"]
    if start == target_tree:
        return True, "candidate is the target tree"
    rel = receipt_rel or (
        f"development/receipts/{receipt['checkpoint_id']}/{receipt['run_id']}.json"
    )
    required = set(package.checkpoints[receipt["checkpoint_id"]]["required_checks"])
    edges: dict[str, list[str]] = {}
    for _, impact in accepted_impacts(root, package):
        if _edge_carries(root, package, impact, required, rel):
            edges.setdefault(impact["from_tree_sha256"], []).append(impact["to_tree_sha256"])
    seen, queue = {start}, deque([start])
    while queue:
        node = queue.popleft()
        for nxt in edges.get(node, []):
            if nxt == target_tree:
                return True, "carried forward by accepted impact records"
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False, "stale: no accepted impact chain from receipt candidate to target tree"


# ----- specification amendments (decision 0008) --------------------------------


def historical_package(package: Package, version: str) -> tuple[Package | None, list[str]]:
    """The archived sealed specification ``version``, verified against its own seal."""
    current = package.current
    if version in current.historical:
        return current.historical[version]
    result: tuple[Package | None, list[str]]
    spec_dir = current.root / SPEC_HISTORY / version
    if not VERSION_RE.fullmatch(version) or version == current.spec_version:
        result = (None, [f"receipt is bound to an unknown spec version {version!r} (stale spec)"])
    elif not (spec_dir / "package-manifest.json").is_file() or tree_manifest.is_link_or_reparse(
        spec_dir
    ):
        result = (None, [f"no archived spec {version} under {SPEC_HISTORY} (stale spec)"])
    else:
        try:
            historical = Package(current.root, spec_dir)
        except (ReceiptError, OSError, KeyError, TypeError) as exc:
            result = (None, [f"archived spec {version} unreadable: {exc}"])
        else:
            historical.current = current
            if historical.spec_version != version:
                result = (None, [f"archived spec {version} declares {historical.spec_version}"])
            elif historical.seal_errors:
                result = (None, [f"archived spec {version}: {historical.seal_errors[0]}"])
            else:
                result = (historical, [])
    current.historical[version] = result
    return result


def _archived_manifest(
    root: Path, rel: str, version: str, digest: str, label: str, errors: list[str]
) -> dict[str, str] | None:
    expected = f"{SPEC_HISTORY}/{version}/package-manifest.json"
    if rel != expected:
        errors.append(f"{label}_manifest must be {expected}")
        return None
    try:
        path = safe_relative(root, rel, SPEC_HISTORY)
        if sha256_file(path) != digest:
            errors.append(f"{label}_manifest digest differs from {label}_manifest_sha256")
        manifest = load_json(path)
        if manifest.get("spec_version") != version:
            errors.append(f"{label}_manifest is not spec {version}")
        return {item["path"]: item["sha256"] for item in manifest["files"]}
    except (ReceiptError, OSError, KeyError, TypeError) as exc:
        errors.append(f"{label}_manifest unreadable: {exc}")
        return None


def _archived_graph(
    root: Path, version: str, sealed: dict[str, str] | None, label: str, errors: list[str]
) -> dict[str, Any] | None:
    """The archived checkpoints.json of ``version``, verified against its sealed digest."""
    if sealed is None:
        return None
    try:
        path = safe_relative(root, f"{SPEC_HISTORY}/{version}/checkpoints.json", SPEC_HISTORY)
        if not path.is_file() or sha256_file(path) != sealed.get("checkpoints.json"):
            errors.append(
                f"{label} spec {version}: archived checkpoints.json differs from its seal"
            )
            return None
        graph = load_json(path)
        if not isinstance(graph, dict) or not isinstance(graph.get("checks"), list):
            # Fail closed: without a check list the definition comparison cannot run.
            errors.append(f"{label} spec {version}: archived checkpoints.json has no check list")
            return None
        return graph
    except (ReceiptError, OSError) as exc:
        errors.append(f"{label} spec {version}: archived checkpoints.json unreadable: {exc}")
        return None


def validate_amendment(
    root: Path, amendment_rel: str, package: Package | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Structural and evidence validation of one reviewed specification amendment."""
    package = (package or Package(root)).current
    if amendment_rel in package.amendment_cache:
        return package.amendment_cache[amendment_rel]
    result = _validate_amendment(root, amendment_rel, package)
    package.amendment_cache[amendment_rel] = result
    return result


def _validate_amendment(
    root: Path, amendment_rel: str, package: Package
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        path = safe_relative(root, amendment_rel, f"{SPEC_HISTORY}/amendments")
        amendment = load_json(path)
        schema = load_json(root / "schemas/spec-amendment.schema.json")
    except (ReceiptError, OSError) as exc:
        return {}, [f"cannot load spec amendment: {exc}"]
    shape = schema_errors(schema, amendment)
    if shape:
        return amendment, shape
    if amendment["from_spec_version"] == amendment["to_spec_version"]:
        errors.append("amendment must change the spec version")
    old = _archived_manifest(
        root,
        amendment["from_manifest"],
        amendment["from_spec_version"],
        amendment["from_manifest_sha256"],
        "from",
        errors,
    )
    new = _archived_manifest(
        root,
        amendment["to_manifest"],
        amendment["to_spec_version"],
        amendment["to_manifest_sha256"],
        "to",
        errors,
    )
    if old is not None and new is not None:
        actual = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
        if sorted(amendment["changed_sealed_paths"]) != actual:
            missing = sorted(set(actual) - set(amendment["changed_sealed_paths"]))
            extra = sorted(set(amendment["changed_sealed_paths"]) - set(actual))
            errors.append(
                f"changed_sealed_paths disagree with manifests (missing={missing}, extra={extra})"
            )
    try:
        decision = safe_relative(root, amendment["decision_record"], "development/decisions")
        if not decision.is_file():
            errors.append("decision_record does not exist")
    except ReceiptError as exc:
        errors.append(f"decision_record: {exc}")
    known = {c for cp in package.checkpoints.values() for c in cp["required_checks"]}
    graphs = [
        _archived_graph(root, amendment[f"{side}_spec_version"], sealed, side, errors)
        for side, sealed in (("from", old), ("to", new))
    ]
    for graph in graphs:
        if graph is not None:
            known |= {c["id"] for c in graph["checks"]}
    for cid in amendment["affected_check_ids"]:
        if cid not in known:
            errors.append(f"unknown check id {cid}")
    if graphs[0] is not None and graphs[1] is not None:
        # A rewritten check definition cannot be carried silently (review a01 D4).
        before = {c["id"]: c for c in graphs[0]["checks"]}
        after = {c["id"]: c for c in graphs[1]["checks"]}
        for cid in sorted(before.keys() & after.keys()):
            if before[cid] != after[cid] and cid not in amendment["affected_check_ids"]:
                errors.append(f"check {cid} definition changed but is not listed as affected")
    if amendment["review_status"] != "pending":
        if not norm_id(amendment["reviewer_id"]):
            errors.append("amendment review without reviewer_id")
        elif norm_id(amendment["reviewer_id"]) == norm_id(amendment["author_id"]):
            errors.append("amendment self-review")
        verdicts = [p for p in amendment["review_evidence_paths"] if p.endswith(".review.json")]
        if len(verdicts) != 1:
            errors.append("amendment review needs exactly one reviewer-written *.review.json")
        else:
            try:
                verdict = load_json(safe_relative(root, verdicts[0], f"{SPEC_HISTORY}/reviews"))
                if verdict.get("kind") != "prometheos-spec-amendment-review":
                    errors.append("amendment review file has the wrong kind")
                if verdict.get("amendment_path") != amendment_rel:
                    errors.append("amendment review names a different amendment record")
                if verdict.get("amendment_sha256") != sha256_file(path):
                    errors.append("amendment record changed after review (digest mismatch)")
                if verdict.get("reviewer_id") != amendment["reviewer_id"]:
                    errors.append("amendment review reviewer_id differs from amendment record")
                if verdict.get("status") != amendment["review_status"]:
                    errors.append("amendment review verdict differs from amendment record")
                if norm_id(verdict.get("reviewer_id")) == norm_id(amendment["author_id"]):
                    errors.append("amendment self-review recorded in review file")
            except (ReceiptError, OSError) as exc:
                errors.append(f"amendment review unreadable: {exc}")
    return amendment, errors


def accepted_amendments(root: Path, package: Package) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    amendment_dir = root / SPEC_HISTORY / "amendments"
    if not amendment_dir.is_dir():
        return out
    for path in sorted(amendment_dir.glob("*.json")):
        rel = path.relative_to(root).as_posix()
        amendment, errors = validate_amendment(root, rel, package)
        if not errors and amendment.get("review_status") == "accepted":
            out.append((rel, amendment))
    return out


def spec_carry_errors(package: Package, historical: Package, receipt: dict[str, Any]) -> list[str]:
    """Carry a receipt valid under an earlier spec to the current one, or say why not.

    The checkpoint must still exist, require no check the receipt did not run, and be
    reachable through accepted amendments none of which affects a required check.
    """
    current = package.current
    cp_id = receipt["checkpoint_id"]
    if cp_id not in current.checkpoints:
        return [f"{cp_id} no longer exists in spec {current.spec_version} (stale spec)"]
    required = set(current.checkpoints[cp_id]["required_checks"])
    new_checks = sorted(required - set(historical.checkpoints[cp_id]["required_checks"]))
    if new_checks:
        return [f"spec {current.spec_version} adds checks {new_checks} to {cp_id} (stale spec)"]
    # External gates are part of a checkpoint's contract too (review a01 D3): a gate the
    # receipt never classified, or one whose requirement changed, cannot be carried.
    gates_then = set(historical.checkpoints[cp_id]["external_gates"])
    new_gates = sorted(set(current.checkpoints[cp_id]["external_gates"]) - gates_then)
    if new_gates:
        return [f"spec {current.spec_version} adds gates {new_gates} to {cp_id} (stale spec)"]
    flags = ("required_for_full_product", "required_for_publication")
    for gid in sorted(current.checkpoints[cp_id]["external_gates"]):
        if any(current.gates[gid].get(f) != historical.gates[gid].get(f) for f in flags):
            return [f"spec {current.spec_version} changes the requirement of {gid} (stale spec)"]
    edges: dict[str, list[tuple[str, set[str]]]] = {}
    for _, amendment in accepted_amendments(current.root, current):
        edges.setdefault(amendment["from_manifest_sha256"], []).append(
            (amendment["to_manifest_sha256"], set(amendment["affected_check_ids"]))
        )
    start, target = receipt["spec_manifest_sha256"], current.spec_manifest_sha256
    seen, queue = {start}, deque([start])
    while queue:
        node = queue.popleft()
        for nxt, affected in edges.get(node, []):
            if affected & required or nxt in seen:
                continue
            if nxt == target:
                return []
            seen.add(nxt)
            queue.append(nxt)
    return [
        f"stale spec: no accepted amendment chain carries {cp_id} from spec "
        f"{historical.spec_version} to {current.spec_version} unaffected"
    ]


# ----- state -------------------------------------------------------------------


def load_state(root: Path) -> dict[str, Any]:
    return load_json(root / STATE_PATH)  # type: ignore[no-any-return]


def write_state(root: Path, state: dict[str, Any]) -> None:
    target = root / STATE_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def dependency_problems(
    root: Path, package: Package, state: dict[str, Any], cp_id: str, target_tree: str
) -> list[str]:
    problems = []
    for dep in package.checkpoints[cp_id]["depends_on"]:
        entry = state["checkpoints"][dep]
        if entry["technical"] != "passed" or entry["review"] != "accepted" or not entry["receipt"]:
            problems.append(f"dependency {dep} is not technically passed and review-accepted")
            continue
        dep_receipt, dep_errors = validate_receipt(root, entry["receipt"], package)
        if dep_errors:
            problems.append(f"dependency {dep} receipt invalid: {dep_errors[0]}")
            continue
        ok, why = chain_to(root, package, dep_receipt, target_tree)
        if not ok:
            problems.append(f"dependency {dep}: {why}")
    return problems


def promote(root: Path, receipt_rel: str) -> list[str]:
    package = Package(root)
    receipt, errors = validate_receipt(root, receipt_rel, package)
    if errors:
        return errors
    current_tree = tree_manifest.tree_sha256(root)
    ok, why = chain_to(root, package, receipt, current_tree)
    if not ok:
        return [f"receipt candidate is not the current tree: {why}"]
    state = load_state(root)
    cp_id = receipt["checkpoint_id"]
    problems = dependency_problems(root, package, state, cp_id, receipt["candidate"]["tree_sha256"])
    if problems:
        return problems
    state["implementation_exists"] = True
    state["checkpoints"][cp_id] = {
        "technical": receipt["technical_status"],
        "review": receipt["review"]["status"],
        "receipt": receipt_rel,
    }
    for check in receipt["external_checks"]:
        if check["status"] == "not_run":
            continue
        if package.gates.get(check["id"], {}).get("checkpoint") != cp_id:
            continue  # gate moved to another checkpoint by a later amendment
        state["external_gates"][check["id"]] = {
            "status": check["status"],
            "receipt": receipt_rel,
            "reason": "; ".join(check["limitations"]) or None,
        }
    state["current_task"] = receipt["next_task"]
    write_state(root, state)
    return []


def status(root: Path) -> tuple[list[str], str | None]:
    package = Package(root)
    state = load_state(root)
    current_tree = tree_manifest.tree_sha256(root)
    lines = [f"working tree {current_tree}"]
    lines.extend(f"WARNING {err}" for err in package.seal_errors)
    ready = None
    for cp_id in package.checkpoints:
        entry = state["checkpoints"][cp_id]
        note = ""
        if entry["receipt"]:
            receipt, errors = validate_receipt(root, entry["receipt"], package)
            if errors:
                note = f" INVALID RECEIPT: {errors[0]}"
            else:
                ok, why = chain_to(root, package, receipt, current_tree)
                note = " current" if ok else f" {why}"
        accepted = entry["technical"] == "passed" and entry["review"] == "accepted"
        lines.append(f"{cp_id} technical={entry['technical']} review={entry['review']}{note}")
        if ready is None and not (accepted and note == " current"):
            if not dependency_problems(root, package, state, cp_id, current_tree):
                ready = cp_id
    for gate_id, gate in state["external_gates"].items():
        lines.append(
            f"{gate_id} {gate['status']}" + (f" ({gate['reason']})" if gate["reason"] else "")
        )
    lines.append(f"first ready checkpoint: {ready}")
    return lines, ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate receipts and derive state.")
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("receipt")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("receipt")
    p_impact = sub.add_parser("impact")
    p_impact.add_argument("impact")
    p_impact.add_argument("--register", action="store_true")
    p_amendment = sub.add_parser("amendment")
    p_amendment.add_argument("amendment")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "validate":
            receipt, errors = validate_receipt(root, args.receipt)
            if not errors:
                package = Package(root)
                _ok, why = chain_to(root, package, receipt, tree_manifest.tree_sha256(root))
                print(f"VALID receipt {args.receipt}; currency: {why}")
        elif args.command == "promote":
            errors = promote(root, args.receipt)
            if not errors:
                print(f"PROMOTED {args.receipt}")
        elif args.command == "impact":
            _, errors = validate_impact(root, args.impact)
            if not errors:
                if args.register:
                    state = load_state(root)
                    state["latest_impact_record"] = args.impact
                    write_state(root, state)
                print(f"VALID impact record {args.impact}")
        elif args.command == "amendment":
            amendment, errors = validate_amendment(root, args.amendment)
            if not errors:
                print(f"VALID spec amendment {args.amendment} ({amendment['review_status']})")
        else:
            lines, _ = status(root)
            print("\n".join(lines))
            errors = []
    except ReceiptError as exc:
        errors = [str(exc)]
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
