"""Research events keep their insertion order (decision 0058).

``occurred_at`` has second resolution. Events of one second (an approval requested and decided in
the same second, int9b's C19-WALK failure) were listed by ``ORDER BY occurred_at, event_id``, so
their order was that of random UUIDs, and a keyset cursor could skip a later event of the same
second whose UUID sorted lower. Events now carry ``seq`` (migration 12): their order of writing.

Every test here writes events whose UUIDs sort in the OPPOSITE order of their writing, so the old
order fails each of them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from cp1_support import OWNER, Env
from cp7_support import create

from prometheos.app import outbox
from prometheos.store.migrations import migrate
from prometheos.store.schema import MIGRATIONS

SECOND = "2026-10-16T05:43:44Z"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def descending_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Event IDs whose UUIDs sort in reverse order of issue."""
    ids = (f"evt_{n:08x}-0000-4000-8000-000000000000" for n in range(0xFFFFFF00, 0, -1))
    monkeypatch.setattr(outbox, "new_operational_id", lambda prefix: next(ids))
    return ids


def write(env: Env, count: int, occurred_at: str = SECOND) -> list[str]:
    """``count`` project events in ``count`` transactions, all in one second; their IDs."""
    written = []
    for n in range(count):
        with closing(sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)) as c:
            c.execute("BEGIN IMMEDIATE")
            event = outbox.emit(
                c,
                "backup.coverage_gap",
                {"kind": "project", "id": env.project_id},
                {"uncovered_versions": n + 1},
                occurred_at=occurred_at,
                project_seq=0,
            )
            c.execute("COMMIT")
        written.append(event.event_id)
    return written


def listed(env: Env, **kwargs: Any) -> dict[str, Any]:
    page: dict[str, Any] = env.svc.event_list(env.project_id, OWNER, state="all", **kwargs)
    return page


def test_events_of_one_second_are_listed_in_the_order_they_were_written(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    descending_ids(monkeypatch)
    written = write(env, 4)
    assert written != sorted(written)  # their UUIDs alone would list them backwards
    assert [e["event_id"] for e in listed(env)["items"]] == written
    queue = env.svc.work_queue(env.project_id, OWNER)
    assert [e["ref"] for e in queue["entries"] if e["entry"] == "event"] == written


def test_paging_through_one_second_neither_skips_nor_repeats(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    descending_ids(monkeypatch)
    written = write(env, 5)
    seen, cursor = [], None
    while True:
        page = listed(env, limit=2, cursor=cursor)
        seen += [e["event_id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == written


def test_an_event_written_later_in_the_same_second_is_not_skipped_by_a_cursor(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keyset cursor names the last event returned. A later event of the same second (its
    UUID lower than the cursor's) comes after it, on the next page; ordered by UUID it fell
    before the cursor and was never listed."""
    descending_ids(monkeypatch)
    first = write(env, 3)
    page = listed(env, limit=2)
    assert [e["event_id"] for e in page["items"]] == first[:2]
    later = write(env, 1)  # same second, a lower UUID than every earlier event
    rest = listed(env, limit=10, cursor=page["next_cursor"])
    assert [e["event_id"] for e in rest["items"]] == [first[2], *later]


def test_a_cursor_whose_event_is_gone_repeats_its_second_rather_than_skipping(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    descending_ids(monkeypatch)
    written = write(env, 3)
    page = listed(env, limit=2)
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        conn.execute("DELETE FROM events WHERE event_id = ?", (written[1],))  # as a purge would
        conn.commit()
    rest = listed(env, limit=10, cursor=page["next_cursor"])
    assert [e["event_id"] for e in rest["items"]] == [written[0], written[2]]


def test_the_order_survives_vacuum(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A purge runs VACUUM, which may renumber rowids; ``seq`` is a column and stays."""
    descending_ids(monkeypatch)
    written = write(env, 4)
    with closing(sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)) as c:
        c.execute("VACUUM")
    assert [e["event_id"] for e in listed(env)["items"]] == written


def test_seq_is_required_unique_and_never_changes(env: Env) -> None:
    write(env, 2)
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        row = conn.execute("SELECT * FROM events ORDER BY seq LIMIT 1").fetchone()
        columns = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        values = dict(zip(columns, row, strict=True))
        values["event_id"] = "evt_00000000-0000-4000-8000-0000000000aa"
        names = ", ".join(c for c in columns if c != "seq")
        marks = ", ".join("?" for c in columns if c != "seq")
        with pytest.raises(sqlite3.IntegrityError, match="seq is required"):
            conn.execute(
                f"INSERT INTO events ({names}) VALUES ({marks})",  # noqa: S608
                [values[c] for c in columns if c != "seq"],
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # noqa: S608, E501
                [values[c] for c in columns],
            )
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            conn.execute("UPDATE events SET seq = seq + 100")


def test_migration_twelve_numbers_an_older_stores_events_in_their_order(tmp_path: Path) -> None:
    """A store at schema 11 holds events of one second in rowid (writing) order; migration 12
    numbers them in (occurred_at, rowid) order, so their UUIDs never decide."""
    assert [m.version for m in MIGRATIONS][11] == 12
    conn = sqlite3.connect(tmp_path / "v11.sqlite", isolation_level=None)
    try:
        assert migrate(conn, MIGRATIONS[:11]) == list(range(1, 12))
        rows = [
            ("evt_ffffffff-0000-4000-8000-000000000000", "2026-10-16T05:43:45Z"),
            ("evt_cccccccc-0000-4000-8000-000000000000", SECOND),
            ("evt_bbbbbbbb-0000-4000-8000-000000000000", SECOND),
            ("evt_aaaaaaaa-0000-4000-8000-000000000000", SECOND),
        ]
        for event_id, at in rows:
            conn.execute(
                "INSERT INTO events (event_id, type, subject_ref, occurred_at, project_seq, data) "
                "VALUES (?, 'backup.coverage_gap', '{}', ?, 0, '{}')",
                (event_id, at),
            )
        # Through the harness, in its transaction (later migrations are tested on their own).
        assert migrate(conn, MIGRATIONS[:12]) == [12]
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"events_seq", "events_order", "events_open_order"} <= indexes
        assert "events_open" not in indexes
        ordered = [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY seq")]
        assert ordered == [rows[1][0], rows[2][0], rows[3][0], rows[0][0]]
    finally:
        conn.close()


def work_titles(env: Env) -> list[str]:
    queue = env.svc.work_queue(env.project_id, OWNER)
    return [e["title"] for e in queue["entries"] if e["entry"] == "work_item"]


def test_work_items_created_in_one_commit_keep_their_order_when_one_is_revised(env: Env) -> None:
    """Review s13-event-order-a F1: items created together share the commit (created_seq) and
    the age; their place in the commit (op_index) decides, so revising one never reorders them.
    Ordered by created_seq alone they followed the scan order, which follows the last update."""
    from test_cp18_work_items import adopt, body, revise, work_body

    from prometheos.domain.ids import new_id

    first, second = new_id("action"), new_id("action")
    env.apply_protected(
        [
            create("action", work_body(title="A first"), first),
            adopt(first),
            create("action", work_body(title="B second"), second),
            adopt(second),
        ]
    )
    assert work_titles(env) == ["A first", "B second"]
    revise(env, first, 2, {**body(env, first), "labels": ["revised"]})
    assert work_titles(env) == ["A first", "B second"]
    revise(env, second, 2, {**body(env, second), "labels": ["revised too"]})
    assert work_titles(env) == ["A first", "B second"]


def test_a_work_item_comes_before_an_event_of_the_same_due_time_and_age(env: Env) -> None:
    """The queue's causal tie-break puts work items before events (0035 item 7 as amended by
    0058). Equal ages need an event stamped at the item's creation time, written here directly."""
    from test_cp18_work_items import add_work_item, work_body

    item = add_work_item(env, title="The item")
    with closing(sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)) as c:
        created = c.execute(
            "SELECT recorded_at FROM revisions WHERE record_id = ? AND rev = 1", (item,)
        ).fetchone()[0]
        c.execute("BEGIN IMMEDIATE")
        outbox.emit(
            c,
            "deadline.approaching",
            {"kind": "action", "id": item},
            {"due_at": work_body()["due_at"], "days_before": 14},
            occurred_at=created,
            project_seq=0,
        )
        c.execute("COMMIT")
    entries = env.svc.work_queue(env.project_id, OWNER)["entries"]
    tied = [e for e in entries if e["since"] == created]
    # The item's own creation and adoption events share its commit's time (a real tie): the
    # item comes first, then every event in writing order, the one written here last.
    assert tied[0]["entry"] == "work_item" and tied[0]["ref"] == item
    assert [e["entry"] for e in tied[1:]] == ["event"] * (len(tied) - 1) and len(tied) >= 3
    assert tied[-1]["type"] == "deadline.approaching"
