"""Record identifiers: ``<kind>_<UUID4>`` with canonical lowercase hyphenated UUIDs."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal, get_args

from pydantic import StringConstraints

Kind = Literal[
    "project",
    "source",
    "version",
    "entry",
    "claim",
    "assessment",
    "correction",
    "person",
    "role",
    "meeting",
    "item",
    "action",
    "paper",
    "anchor",
    "task",
    "job",
    "package",
    "run",
    "execution",  # A02, CP10: observe-only external scientific execution
    "work",  # A02, CP11 (decision 0060): a bibliographic work
    "matrix",  # A02, CP11 S11.4 (decision 0064): an evidence matrix
    "extraction",  # A02, CP11 S11.4: one matrix row (a work's cells)
]
KINDS: tuple[str, ...] = get_args(Kind)

UUID4_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_KIND_ALTERNATION = "|".join(KINDS)
RECORD_ID_PATTERN = rf"^(?:{_KIND_ALTERNATION})_{UUID4_PATTERN}$"
_RECORD_ID_RE = re.compile(RECORD_ID_PATTERN)
_UUID4_RE = re.compile(rf"^{UUID4_PATTERN}$")


def kind_id_pattern(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return rf"^{kind}_{UUID4_PATTERN}$"


RecordId = Annotated[str, StringConstraints(pattern=RECORD_ID_PATTERN)]
RequestId = Annotated[str, StringConstraints(pattern=rf"^{UUID4_PATTERN}$")]
ProjectId = Annotated[str, StringConstraints(pattern=kind_id_pattern("project"))]

# Operational identifiers (A02, CP10; decision 0015). They name rows of operational tables,
# never records: they cannot appear in a record reference or a relation.
OPERATIONAL_PREFIXES: tuple[str, ...] = ("oprun", "apreq", "mapreq", "quarantine", "evt")
OperationRunId = Annotated[str, StringConstraints(pattern=rf"^oprun_{UUID4_PATTERN}$")]
ApprovalRequestId = Annotated[str, StringConstraints(pattern=rf"^apreq_{UUID4_PATTERN}$")]
MaintenanceApprovalId = Annotated[str, StringConstraints(pattern=rf"^mapreq_{UUID4_PATTERN}$")]
QuarantineId = Annotated[str, StringConstraints(pattern=rf"^quarantine_{UUID4_PATTERN}$")]


def new_operational_id(prefix: str) -> str:
    if prefix not in OPERATIONAL_PREFIXES:
        raise ValueError(f"unknown operational id prefix: {prefix}")
    return f"{prefix}_{uuid.uuid4()}"


def new_id(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return f"{kind}_{uuid.uuid4()}"


def new_request_id() -> str:
    return str(uuid.uuid4())


def derived_request_id(*parts: str) -> str:
    """A UUID4-shaped request key derived from ``parts`` (stable across retries).

    Used where a service builds a request whose content depends on the snapshot (a job
    slot's finding, a saved paper check): the derived key can only collide with the same
    operation on the same inputs, never with an unrelated request."""
    import hashlib

    raw = bytearray(hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    text = raw.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def is_record_id(value: str, kind: str | None = None) -> bool:
    if not _RECORD_ID_RE.fullmatch(value):
        return False
    return kind is None or value.startswith(f"{kind}_")


def is_request_id(value: str) -> bool:
    return bool(_UUID4_RE.fullmatch(value))


def kind_of(record_id: str) -> str:
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"malformed record id: {record_id!r}")
    return record_id.split("_", 1)[0]
