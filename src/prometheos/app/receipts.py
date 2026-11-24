"""Execution receipts reported by an external agent (spec/03, spec/05; decision 0009).

A receipt is an ``agent_receipt`` source whose single inline version holds the canonical
JSON of an ``execution_receipt_v1`` payload. Every input and output it names must be a
stored version of the stated source with exactly the stated digest, so the receipt pins
binary artifacts (models, figures, MAT files) that text locators cannot cite. The
receipt records what was *reported*: PrometheOS never upgrades it to an observation,
and intact bytes say nothing about scientific validity.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from pydantic import ValidationError

from prometheos.domain.bodies import ExecutionReceiptV1, SourceBody, VersionBody
from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_internal,
    loads_strict,
)
from prometheos.domain.ids import new_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.store import derived
from prometheos.store.project import ProjectStore, Txn, live

MAX_RECEIPT_BYTES = 1024 * 1024
RANK_NAME = {rank: name for name, rank in SENSITIVITY_ORDER.items()}
RECEIPT_RELATION = "cites"  # receipt source/version -> each artifact version it names


def parse(raw: bytes) -> ExecutionReceiptV1:
    """Strictly parse a submitted receipt; a claim of ``observed`` is refused, not relabelled."""
    if len(raw) > MAX_RECEIPT_BYTES:
        raise PrometheosError(
            "budget_exceeded", "receipt is too large", {"limit": MAX_RECEIPT_BYTES}
        )
    try:
        document = loads_strict(raw)
    except CanonicalJSONError as exc:
        raise PrometheosError("validation_error", f"receipt is not strict JSON: {exc}", {}) from exc
    if isinstance(document, dict) and document.get("observation") == "observed":
        raise PrometheosError(
            "validation_error",
            "an agent-submitted receipt is reported, never observed: PrometheOS did not run it",
            {},
        )
    try:
        return ExecutionReceiptV1.model_validate(document, strict=True)
    except ValidationError as exc:
        raise PrometheosError(
            "validation_error",
            "receipt does not match execution_receipt_v1",
            {"errors": [f"{'/'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]},
        ) from exc


def _search_text(receipt: ExecutionReceiptV1) -> str:
    tools = ", ".join(f"{t.name} {t.version}" for t in receipt.environment.tools)
    lines = [
        f"Reported execution ({receipt.outcome}) by {receipt.reporter}",
        f"Command: {receipt.command}",
        f"Environment: {receipt.environment.os}; {tools}",
        *(f"Limitation: {item}" for item in receipt.limitations),
    ]
    return "\n".join(lines)


def record(
    store: ProjectStore,
    *,
    raw: bytes,
    title: str,
    description: str,
    request_id: str,
    actor: str,
    allowed_roots: set[str],
    max_rank: int,
) -> dict[str, Any]:
    receipt = parse(raw)
    if receipt.working_root is not None and receipt.working_root not in allowed_roots:
        raise PrometheosError(
            "policy_denied",
            "working_root is not an approved research root alias",
            {"alias": receipt.working_root},
        )
    payload = receipt.model_dump(mode="json")
    data = dumps_canonical(payload)
    sha = hashlib.sha256(data).hexdigest()
    source_body = SourceBody(
        title=title,
        origin="agent_receipt",
        origin_uri=None,
        root_binding=None,
        retention="copy",
        description=description,
    ).model_dump(mode="json")
    request_sha = canonical_sha256(
        {
            "op": "source_receipt_record",
            "payload": payload,
            "title": title,
            "description": description,
        }
    )

    def handler(txn: Txn) -> None:
        rank = 0
        cited: list[tuple[str, int]] = []
        for label, artifacts in (("inputs", receipt.inputs), ("outputs", receipt.outputs)):
            for index, artifact in enumerate(artifacts):
                where = f"{label}/{index}"
                # Records above the caller's rank are invisible here (txn.get returns None)
                # and purged records are gone: both read exactly like absent records, so a
                # receipt cannot confirm a hidden record or test its digest (review 0009 H1).
                source = live(txn.get(artifact.source_id))
                version = live(txn.get(artifact.version_id))
                if source is None or source.kind != "source":
                    raise PrometheosError(
                        "not_found", "receipt names a source that does not exist", {"at": where}
                    )
                if version is None or version.kind != "version":
                    raise PrometheosError(
                        "not_found", "receipt names a version that does not exist", {"at": where}
                    )
                cited.append((version.id, version.rev))
                if version.body["source_id"] != artifact.source_id:
                    raise PrometheosError(
                        "validation_error", "version does not belong to that source", {"at": where}
                    )
                if version.body["sha256"] != artifact.sha256:
                    raise PrometheosError(
                        "validation_error",
                        "receipt digest differs from the stored version digest",
                        {"at": where},
                    )
                rank = max(
                    rank,
                    SENSITIVITY_ORDER[source.sensitivity],
                    SENSITIVITY_ORDER[version.sensitivity],
                )
        sensitivity = RANK_NAME[rank]  # derived output inherits its highest input
        source_id, version_id = new_id("source"), new_id("version")
        body_id = txn.insert_body(source_body)
        txn.write_revision(
            source_id,
            "source",
            1,
            body_id,
            sensitivity,
            "active",
            None,
            "source_receipt_record",
            body=source_body,
        )
        txn.result(source_id, 1)
        txn.conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
            "VALUES (?,?,?,?)",
            (sha, data, len(data), txn.seq),
        )
        version_body = VersionBody(
            source_id=source_id,
            sha256=sha,
            byte_length=len(data),
            mime="application/json",
            captured_at=txn.recorded_at,
            storage="body",
            storage_key=f"payload:{sha}",
            availability="retained",
            extraction="none",
            derived_from=None,
            payload_schema="execution_receipt_v1",
            transformation=None,
        ).model_dump(mode="json")
        body_id = txn.insert_body(version_body)
        txn.write_revision(
            version_id,
            "version",
            1,
            body_id,
            sensitivity,
            "active",
            None,
            "source_receipt_record",
            body=version_body,
        )
        derived.index_text(txn.conn, version_id, "version", title, _search_text(receipt))
        txn.result(version_id, 1)
        # The artifact references live in the payload, which the rebuildable body index
        # never sees. Persistent relations make a later sensitivity raise of an artifact
        # reach the receipt and let purge take the receipt with it (review 0009 H2).
        for receipt_id in (source_id, version_id):
            for target_id, target_rev in dict.fromkeys(cited):
                txn.conn.execute(
                    "INSERT INTO relations (source_id, source_rev, target_id, target_rev, "
                    "relation, created_seq) VALUES (?,?,?,?,?,?)",
                    (receipt_id, 1, target_id, target_rev, RECEIPT_RELATION, txn.seq),
                )

    return store.commit(
        request_id=request_id,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )


def is_receipt_record(conn: sqlite3.Connection, record_id: str) -> bool:
    """True for the source or version of an execution receipt (purge closure rule)."""
    row = conn.execute(
        "SELECT r.kind, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
        (record_id,),
    ).fetchone()
    if row is None or row[1] is None:
        return False
    body = loads_internal(row[1])
    if row[0] == "source":
        return bool(body.get("origin") == "agent_receipt")
    return bool(row[0] == "version" and body.get("payload_schema") == "execution_receipt_v1")
