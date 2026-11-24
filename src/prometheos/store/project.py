"""One project's local store: layout, connections and the shared commit engine.

Layout: ``record.sqlite``, ``media/``, ``staging/``, ``cache/``, ``exports/``.
Every research mutation goes through :meth:`ProjectStore.commit`, which owns
request idempotency, the global ``expected_seq`` check, ordered audit rows,
the project sequence advance and the stored response -- all in one SQLite
transaction. A crash before COMMIT exposes nothing; after COMMIT an identical
retry returns the original response with ``replayed=true``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheos.domain.canonical import dumps_canonical, loads_internal, sha256_hex
from prometheos.domain.ids import is_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError, utc_now
from prometheos.faults import fault_point
from prometheos.store import admission, db, derived
from prometheos.store.migrations import migrate, pending, schema_version
from prometheos.store.schema import MIGRATIONS

DB_NAME = "record.sqlite"
SUBDIRS = ("media", "staging", "cache", "exports")
MAX_RANK = max(SENSITIVITY_ORDER.values())


def rank_bound_sha256(request_sha256: str, max_rank: int) -> str:
    """The stored request key: the request digest bound to the caller's visibility rank."""
    return sha256_hex(f"{request_sha256}|rank={max_rank}".encode())


def request_key_matches(stored: str, request_sha256: str, max_rank: int) -> bool:
    """Whether a stored request key belongs to this request digest at this rank.

    Rows written before rank binding hold the bare digest; their rank is unknown, so only
    the highest rank may match them (fail safe)."""
    if stored == rank_bound_sha256(request_sha256, max_rank):
        return True
    return stored == request_sha256 and max_rank >= MAX_RANK


@dataclass
class RecordState:
    id: str
    kind: str
    rev: int
    sensitivity: str
    lifecycle: str
    body: dict[str, Any]
    body_id: int


def live(state: RecordState | None) -> RecordState | None:
    """``state`` unless it is a purged or deleted record, which reads as absent."""
    if state is None or state.lifecycle == "deleted" or state.body.get("purged") is True:
        return None
    return state


@dataclass
class Txn:
    """Mutation context handed to commit handlers (one SQLite transaction)."""

    conn: sqlite3.Connection
    seq: int
    request_id: str
    actor: str
    recorded_at: str
    op_index: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    # Highest sensitivity rank the caller may see (0 normal .. 2 private). Records above
    # it behave exactly like absent records, so writes cannot probe for hidden data.
    max_rank: int = 2

    def visible(self, sensitivity: str) -> bool:
        return SENSITIVITY_ORDER[sensitivity] <= self.max_rank

    def get(self, record_id: str) -> RecordState | None:
        row = self.conn.execute(
            "SELECT r.id, r.kind, r.current_rev, r.sensitivity, r.lifecycle, v.body_id, b.encoded "
            "FROM records r JOIN revisions v ON v.record_id = r.id AND v.rev = r.current_rev "
            "JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
            (record_id,),
        ).fetchone()
        if row is None or not self.visible(row["sensitivity"]):
            return None
        body = json.loads(row["encoded"]) if row["encoded"] is not None else {}
        return RecordState(
            row["id"],
            row["kind"],
            row["current_rev"],
            row["sensitivity"],
            row["lifecycle"],
            body,
            row["body_id"],
        )

    def rev_exists(self, record_id: str, rev: int) -> bool:
        row = self.conn.execute(
            "SELECT r.sensitivity FROM revisions v JOIN records r ON r.id = v.record_id "
            "WHERE v.record_id = ? AND v.rev = ?",
            (record_id, rev),
        ).fetchone()
        return row is not None and self.visible(row["sensitivity"])

    def insert_body(self, body: dict[str, Any]) -> int:
        encoded = dumps_canonical(body).decode("utf-8")
        cur = self.conn.execute(
            "INSERT INTO bodies (sha256, encoded) VALUES (?, ?)",
            (sha256_hex(encoded.encode("utf-8")), encoded),
        )
        return int(cur.lastrowid or 0)

    def write_revision(
        self,
        record_id: str,
        kind: str,
        rev: int,
        body_id: int,
        sensitivity: str,
        lifecycle: str,
        occurred_at: str | None,
        operation: str,
        *,
        body: dict[str, Any] | None,
    ) -> None:
        """Write one revision. ``body`` (``None`` for purge tombstones) feeds the
        derived reference/search indexes in the same transaction."""
        if rev == 1:
            self.conn.execute(
                "INSERT INTO records (id, kind, current_rev, sensitivity, lifecycle, created_seq, "
                "updated_seq) VALUES (?,?,?,?,?,?,?)",
                (record_id, kind, 1, sensitivity, lifecycle, self.seq, self.seq),
            )
        else:
            self.conn.execute(
                "UPDATE records SET current_rev = ?, sensitivity = ?, lifecycle = ?, "
                "updated_seq = ? WHERE id = ? AND current_rev = ?",
                (rev, sensitivity, lifecycle, self.seq, record_id, rev - 1),
            )
        self.conn.execute(
            "INSERT INTO revisions (record_id, rev, commit_seq, op_index, recorded_at, "
            "occurred_at, body_id, sensitivity, lifecycle) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                rev,
                self.seq,
                self.op_index,
                self.recorded_at,
                occurred_at,
                body_id,
                sensitivity,
                lifecycle,
            ),
        )
        self.audit(operation, record_id, rev)
        derived.index_record(self.conn, record_id, kind, rev, body)
        if operation == "revise" and kind != "extraction":
            # A matrix row's flags clear only when the owner reviews it again (CP11 S11.4,
            # decision 0064); apply clears them then.
            derived.clear_flags(self.conn, record_id, self.seq)

    def audit(self, operation: str, record_id: str | None, rev: int | None) -> None:
        position = self.conn.execute(
            "SELECT COUNT(*) FROM audit WHERE seq = ?", (self.seq,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO audit (seq, op_index, request_id, operation, record_id, rev, actor, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.seq,
                position,
                self.request_id,
                operation,
                record_id,
                rev,
                self.actor,
                self.recorded_at,
            ),
        )

    def result(self, record_id: str | None, rev: int | None) -> None:
        self.results.append({"op_index": self.op_index, "id": record_id, "rev": rev})


Handler = Callable[[Txn], None]


class ProjectStore:
    def __init__(self, directory: Path, *, _scratch_copy: bool = False) -> None:
        self.directory = directory
        self.db_path = directory / DB_NAME
        self._project_id: str | None = None
        self._physical_key: tuple[Any, ...] | None = None
        self._scratch_copy = _scratch_copy

    # ----- lifecycle -------------------------------------------------------------

    @classmethod
    def create(cls, directory: Path, project_id: str) -> ProjectStore:
        if directory.exists() and any(directory.iterdir()):
            raise PrometheosError("validation_error", "store directory is not empty", {})
        directory.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (directory / sub).mkdir(exist_ok=True)
        conn = db.connect(directory / DB_NAME, create=True)
        try:
            migrate(conn, MIGRATIONS)
            with db.write_transaction(conn):
                conn.execute(
                    "INSERT INTO project_state (singleton, project_id, project_seq, created_at) "
                    "VALUES (1, ?, 0, ?)",
                    (project_id, utc_now()),
                )
        finally:
            conn.close()
        return cls(directory)

    def connect(
        self, readonly: bool = False, allow_pending: bool = False, *, wait: bool = True
    ) -> sqlite3.Connection:
        """``wait=False`` makes one attempt at the project's admission gate instead of waiting
        for it (``busy`` at once): for housekeeping that a later call can do instead."""
        from prometheos.app import maintenance

        if self._scratch_copy:
            return db.connect(self.db_path, readonly=readonly)
        wait_ms = admission.TIMEOUT_MS if wait else 0
        with admission.operation(self.db_path, wait_ms=wait_ms) as lease:
            if self._physical_key is not None and self._physical_key != lease.key:
                raise PrometheosError("busy", "the project database identity changed; reopen", {})
            lease.prepare(self.db_path)
            if not lease.live:
                with admission.source_snapshot(self.directory) as snapshot:
                    maintenance.check_openable(
                        self.directory, self._project_id or "unknown", allow_pending=allow_pending
                    )
                    lease.pin = snapshot.pin()
            conn = db.connect(self.db_path, readonly=readonly, factory=admission.Connection)
            assert isinstance(conn, admission.Connection)
            lease.attach(conn)
            self._physical_key = lease.key
            try:
                todo = pending(conn, MIGRATIONS)
                if todo and not allow_pending:
                    version = schema_version(conn)
                    raise PrometheosError(
                        "unsupported",
                        "store schema requires migration before use "
                        "(prometheos maintenance migrate)",
                        {
                            "store_schema_version": version,
                            "supported_schema_version": len(MIGRATIONS),
                        },
                    )
            except BaseException:
                conn.close()
                raise
            return conn

    def upgrade(self, before_upgrade: Callable[[int, int], None]) -> dict[str, Any]:
        """Apply pending migrations after ``before_upgrade`` (a verified backup) succeeds."""
        conn = self.connect(allow_pending=True)
        try:
            old = schema_version(conn)
            applied = migrate(conn, MIGRATIONS, before_upgrade=before_upgrade)
            return {"from_schema": old, "to_schema": schema_version(conn), "applied": applied}
        finally:
            conn.close()

    def identity(self) -> str:
        """The project this store holds, read through an immutable connection (no lock, no
        journal or write-ahead-log handling, so no side file is created; decision 0021 round 2,
        F5). The caller has checked the file, the journal and the schema version first
        (``app.maintenance.check_openable``)."""
        from prometheos.app import maintenance

        with admission.source_snapshot(self.directory) as snapshot:
            maintenance.check_openable(self.directory, self._project_id or "unknown")
            conn = db.connect(self.db_path, readonly=True, immutable=True)
            try:
                row = conn.execute("SELECT project_id FROM project_state").fetchone()
            finally:
                conn.close()
            self._physical_key = snapshot.lease.key
        if row is None:
            raise PrometheosError("io_error", "the store holds no project state", {})
        self._project_id = str(row["project_id"])
        return self._project_id

    @property
    def project_id(self) -> str:
        if self._project_id is None:
            conn = self.connect(readonly=True)
            try:
                row = conn.execute("SELECT project_id FROM project_state").fetchone()
            finally:
                conn.close()
            if row is None:
                raise PrometheosError("io_error", "the store holds no project state", {})
            self._project_id = str(row["project_id"])
        return self._project_id

    def project_seq(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])

    # ----- commit engine ---------------------------------------------------------

    def commit(
        self,
        *,
        request_id: str,
        request_sha256: str,
        expected_seq: int | None,
        actor: str,
        handler: Handler,
        max_rank: int = 2,
    ) -> dict[str, Any]:
        """Apply ``handler`` atomically, or replay an identical committed request."""
        if not is_request_id(request_id):
            # Every transport (CLI, MCP, hub) reaches this engine; one check keeps the
            # stored request keys and result_request_id fields within their schema.
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        # A replay returns the original response, including IDs of records it created, so
        # the stored key also binds the caller's visibility rank: the same body resent at a
        # lower rank conflicts instead of revealing restricted records (review 0009 R2-L4).
        bound_sha256 = rank_bound_sha256(request_sha256, max_rank)
        conn = self.connect()
        try:
            with db.write_transaction(conn):
                prior = conn.execute(
                    "SELECT request_sha256, response FROM requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if prior is not None:
                    if not request_key_matches(prior["request_sha256"], request_sha256, max_rank):
                        raise PrometheosError(
                            "request_key_conflict",
                            "request_id was already used for a different request",
                            {"request_id": request_id},
                        )
                    replay: dict[str, Any] = loads_internal(prior["response"])
                    replay["replayed"] = True
                    return replay
                current = self.project_seq(conn)
                if expected_seq is not None and expected_seq != current:
                    raise PrometheosError(
                        "revision_conflict",
                        f"expected_seq {expected_seq} does not match project sequence {current}",
                        {"expected_seq": expected_seq, "project_seq": current},
                    )
                txn = Txn(
                    conn=conn,
                    seq=current + 1,
                    request_id=request_id,
                    actor=actor,
                    recorded_at=utc_now(),
                    max_rank=max_rank,
                )
                handler(txn)
                conn.execute("UPDATE project_state SET project_seq = ?", (txn.seq,))
                response = {
                    "ok": True,
                    "schema_version": 1,
                    "project_id": self.project_id_in(conn),
                    "request_id": request_id,
                    "project_seq": txn.seq,
                    "results": txn.results,
                    "replayed": False,
                }
                conn.execute(
                    "INSERT INTO requests (request_id, request_sha256, project_seq, response, "
                    "committed_at) VALUES (?,?,?,?,?)",
                    (
                        request_id,
                        bound_sha256,
                        txn.seq,
                        dumps_canonical(response).decode("utf-8"),
                        txn.recorded_at,
                    ),
                )
                fault_point("commit_before")
            fault_point("commit_after")
            return response
        finally:
            conn.close()

    def prior_response(
        self, request_id: str, *, request_sha256: str, max_rank: int
    ) -> dict[str, Any] | None:
        """The stored response of a committed request, marked as a replay (else ``None``).

        For conveniences whose request content depends on the snapshot they were built
        from (e.g. a saved paper check): a retry with the same request ID must replay the
        original commit, not build a new request against a newer sequence. Such a caller
        commits under a fixed operation identity digest (never the snapshot-dependent body
        digest) and passes that digest and its rank here. A stored request that is not
        bound to exactly this digest at this rank raises ``request_key_conflict`` exactly
        like :meth:`commit`: a replay never returns IDs created at a higher rank (review
        0010 R1-I1)."""
        conn = self.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT request_sha256, response FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if not request_key_matches(row["request_sha256"], request_sha256, max_rank):
            raise PrometheosError(
                "request_key_conflict",
                "request_id was already used for a different request",
                {"request_id": request_id},
            )
        replay: dict[str, Any] = loads_internal(row["response"])
        replay["replayed"] = True
        return replay

    def dry_run(self, *, expected_seq: int, handler: Handler, max_rank: int = 2) -> None:
        """Run ``handler`` against the current snapshot and always roll back."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self.project_seq(conn)
                if expected_seq != current:
                    raise PrometheosError(
                        "revision_conflict",
                        f"expected_seq {expected_seq} does not match project sequence {current}",
                        {"expected_seq": expected_seq, "project_seq": current},
                    )
                handler(
                    Txn(
                        conn=conn,
                        seq=current + 1,
                        request_id="dry-run",
                        actor="dry-run",
                        recorded_at=utc_now(),
                        max_rank=max_rank,
                    )
                )
            finally:
                conn.execute("ROLLBACK")
        finally:
            conn.close()

    @staticmethod
    def project_id_in(conn: sqlite3.Connection) -> str:
        return str(conn.execute("SELECT project_id FROM project_state").fetchone()[0])
