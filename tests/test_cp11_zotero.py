"""CP11 S11.5: the read-only Zotero adapter (decisions 0069 section 2.1 and 0070).

Behaviour tests for the Zotero half of C11-IMPORT and the Zotero clauses of C11-DEDUP, against
``tools/zotero_stub.py`` (a loopback stub of the documented local API with the S29/S30 fixtures)
and never against a real Zotero:

- ``zotero status`` is a probe that never raises: available, not_running, local_api_disabled,
  unexpected;
- the adapter is loopback only (refused before any connection, and again against the address
  actually connected to), GET only, with no key, no account, no ``Origin`` and
  ``Zotero-API-Version: 3``; oversized, malformed and non-Zotero answers are refused;
- a plan lists the items; a commit retains each item's JSON as an ``adapter`` source version and
  maps it to a work through the bibliography importer's identifiers, dedup and precedence, with
  provenance (basis ``zotero``) naming the item key and a JSON pointer that resolves;
- one DOI from BibTeX, a PDF and Zotero is one work holding every provenance record; a preprint
  is related to its published version, not merged; a re-import creates nothing; a newer item
  version revises the work; injected text in an abstract stays inert.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import synthetic_bib as bibs
import synthetic_pdf as pdfs
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from zotero_stub import ZoteroStub

from prometheos.app.apply import load_source_json, resolve_pointer
from prometheos.domain.models import PrometheosError
from prometheos.library import zotero

DRAG_DOI = "10.5555/synthetic.2026.001"


class Z:
    """A project, a Zotero stub and the settings file that points the adapter at it."""

    def __init__(self, tmp: Path, **stub: Any) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.stub = ZoteroStub(**stub)
        self.port = self.stub.start()
        self.point_at(self.port)

    def point_at(self, port: int, host: str | None = None) -> None:
        settings: dict[str, Any] = {"port": port}
        if host is not None:
            settings["host"] = host
        (self.env.home / "zotero.json").write_text(json.dumps(settings), encoding="utf-8")

    def plan(self, caller: Any = OWNER, **kw: Any) -> dict[str, Any]:
        return dict(self.svc.zotero_import_plan(self.pid, caller, **kw))

    def commit(self, plan: dict[str, Any], caller: Any = OWNER, **kw: Any) -> dict[str, Any]:
        """The owner commits (an owner act: ``authority``); an agent asks, see ``ask``."""
        return dict(
            self.svc.zotero_import_commit(
                self.pid, plan["plan_id"], caller, authority=OWNER_ACT, **kw
            )
        )

    def ask(self, plan: dict[str, Any], **kw: Any) -> dict[str, Any]:
        """An agent's request to commit: approval_required, with the request's details."""
        params = {"plan_id": plan["plan_id"], **kw}
        with pytest.raises(PrometheosError) as asked:
            self.svc.approval_request(self.pid, AGENT, "zotero_import_commit", params)
        assert asked.value.code == "approval_required"
        return dict(asked.value.details)

    def grant(self, approval_id: str) -> None:
        shown = self.svc.approval_owner_view(self.pid, approval_id)
        self.svc.approval_decide(
            self.pid, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
        )

    def imported(self, **kw: Any) -> dict[str, Any]:
        return self.commit(self.plan(**kw))

    def works(self, caller: Any = OWNER) -> list[dict[str, Any]]:
        return list(self.svc.library_works(self.pid, caller, limit=200)["works"])

    def work(self, title_start: str) -> dict[str, Any]:
        return next(w for w in self.works() if w["title"].lower().startswith(title_start.lower()))

    def body(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None)["body"])

    def rev(self, record_id: str) -> int:
        return int(self.svc.get(self.pid, OWNER, record_id, None)["rev"])

    def retained(self, version_id: str) -> bytes:
        sha = self.body(version_id)["sha256"]
        with closing(sqlite3.connect(self.env.store_dir / "record.sqlite")) as conn:
            return bytes(
                conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (sha,)).fetchone()[0]
            )

    def seq(self) -> int:
        return int(self.env.seq())

    def get_requests(self) -> list[dict[str, Any]]:
        return list(self.stub.requests)

    def close(self) -> None:
        self.stub.stop()


@pytest.fixture
def z(tmp_path: Path) -> Iterator[Z]:
    lib = Z(tmp_path)
    yield lib
    lib.close()


def keys(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["key"]: i for i in plan["items"]}


def write_library_folder(z: Z, *, pdfs_too: bool = False) -> None:
    folder = z.env.research / "library"
    if pdfs_too:
        pdfs.write_folder(
            folder, {"drag.pdf": pdfs.paper(pdfs.DRAG), "kers.pdf": pdfs.paper(pdfs.KERS)}
        )
    bibs.write_folder(folder, {"refs.bib": bibs.REFS_BIB.encode("utf-8")})


def import_library_folder(z: Z) -> dict[str, Any]:
    plan = z.svc.library_import_plan(z.pid, OWNER, root_alias="research", path="library")
    return dict(z.svc.library_import_commit(z.pid, plan["plan_id"], OWNER))


# ----- status: the capability probe -------------------------------------------------------------


def test_status_available_names_the_state_and_lists_collections(z: Z) -> None:
    out = z.svc.zotero_status(z.pid, OWNER, collections=True)
    assert out["state"] == "available" and out["api_version"] == "3"
    assert out["version"] == "unknown"  # the stub sends no version header: reported as unknown
    assert out["library_version"] == 21 and out["endpoint"] == f"http://127.0.0.1:{z.port}/api/"
    assert [c["name"] for c in out["collections"]] == ["Flywheels", "Misc"]
    assert "running" in out["text"]


def test_status_reports_the_version_zotero_sends(tmp_path: Path) -> None:
    lib = Z(tmp_path, version_header="10.0.6")
    try:
        assert lib.svc.zotero_status(lib.pid, OWNER)["version"] == "10.0.6"
    finally:
        lib.close()


def test_status_not_running_is_a_state_not_an_error(z: Z) -> None:
    z.stub.stop()
    out = z.svc.zotero_status(z.pid, AGENT)
    assert out["state"] == "not_running" and "Start Zotero" in out["text"]
    # The library keeps working whatever Zotero does.
    assert z.svc.library_works(z.pid, OWNER)["total"] == 0


def test_status_local_api_disabled(tmp_path: Path) -> None:
    lib = Z(tmp_path, mode="disabled")
    try:
        out = lib.svc.zotero_status(lib.pid, OWNER)
        assert out["state"] == "local_api_disabled" and "Settings" in out["text"]
    finally:
        lib.close()


@pytest.mark.parametrize("fault", ["not_zotero", "malformed", "duplicate_keys"])
def test_status_unexpected_answers(tmp_path: Path, fault: str) -> None:
    lib = Z(tmp_path, fault=fault)
    try:
        out = lib.svc.zotero_status(lib.pid, OWNER)
        assert out["state"] == "unexpected" and out["detail"], out
    finally:
        lib.close()


def test_status_never_raises_for_a_broken_settings_file_or_a_foreign_host(z: Z) -> None:
    (z.env.home / "zotero.json").write_text("{not json", encoding="utf-8")
    out = z.svc.zotero_status(z.pid, OWNER)
    assert out["state"] == "unexpected" and "zotero.json" in out["detail"]
    (z.env.home / "zotero.json").write_text('{"port": 70000}', encoding="utf-8")
    assert z.svc.zotero_status(z.pid, OWNER)["state"] == "unexpected"
    (z.env.home / "zotero.json").write_text('{"proxy": "x"}', encoding="utf-8")
    assert z.svc.zotero_status(z.pid, OWNER)["state"] == "unexpected"
    z.point_at(z.port, "example.com")
    out = z.svc.zotero_status(z.pid, OWNER)
    assert out["state"] == "unexpected" and "127.0.0.1" in out["detail"]


# ----- the adapter only reads, on loopback ------------------------------------------------------


def test_a_host_that_is_not_loopback_is_refused_before_any_connection(
    z: Z, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_connect(self: http.client.HTTPConnection) -> None:
        raise AssertionError("the adapter connected to a non-loopback host")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_connect)
    before = len(z.get_requests())
    for host in ("192.0.2.1", "example.com", "0.0.0.0", "10.0.0.5", "zotero.local"):  # noqa: S104
        z.point_at(z.port, host)
        with pytest.raises(PrometheosError) as exc:
            z.plan()
        assert exc.value.code == "policy_denied", host
        assert z.svc.zotero_status(z.pid, OWNER)["state"] == "unexpected"
    assert z.svc.run_list(z.pid, OWNER)["runs"] == []  # refused at once: no run was created
    assert len(z.get_requests()) == before
    for host in ("127.0.0.1", "localhost", "[::1]", "::1"):
        assert zotero.is_loopback(host), host


def test_the_connected_address_must_be_loopback_too(z: Z, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name that resolves elsewhere (a hosts file, DNS) does not make a remote host local."""

    class Sock:
        def getpeername(self) -> tuple[str, int]:
            return ("203.0.113.9", 23119)

        def close(self) -> None:
            pass

    def fake_connect(self: http.client.HTTPConnection) -> None:
        self.sock = Sock()  # type: ignore[assignment]

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fake_connect)
    with pytest.raises(PrometheosError) as exc:
        zotero.request(zotero.Endpoint("localhost", z.port), "GET", "/api/users/0/items/top")
    assert exc.value.code == "policy_denied" and "loopback" in exc.value.message


def test_only_get_is_ever_sent_and_other_methods_are_refused_before_a_socket(z: Z) -> None:
    ep = zotero.endpoint(z.env.home)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "get"):
        with pytest.raises(PrometheosError) as exc:
            zotero.request(ep, method, "/api/users/0/items")
        assert exc.value.code == "policy_denied"
    with pytest.raises(PrometheosError):
        zotero.get(ep, "/connector/ping")  # only the local API (/api/) is read
    assert z.get_requests() == []
    z.imported()
    z.stub.update("DRAG0001", title="Aerodynamic Drag of a Flywheel Housing (second edition)")
    z.imported()
    z.svc.zotero_status(z.pid, OWNER, collections=True)
    seen = z.get_requests()
    assert seen and {r["method"] for r in seen} == {"GET"}
    # No key, no account, no Origin; the API version is declared; Host is the loopback name.
    assert all(r["origin"] is None and r["api_version"] == "3" for r in seen)
    assert not any(r["has_authorization"] or r["has_zotero_api_key"] for r in seen)
    assert all(r["host"] == f"127.0.0.1:{z.port}" for r in seen)
    assert all(str(r["user_agent"]).startswith("PrometheOS") for r in seen)
    assert z.stub.dropped == []


def test_the_stub_enforces_the_hardening_the_adapter_must_respect(z: Z) -> None:
    def raw(headers: dict[str, str], method: str = "GET") -> tuple[int | None, str]:
        conn = http.client.HTTPConnection("127.0.0.1", z.port, timeout=3)
        try:
            conn.putrequest(method, "/api/users/0/items/top?format=json", skip_host=True)
            for name, value in {"Host": f"127.0.0.1:{z.port}", **headers}.items():
                conn.putheader(name, value)
            conn.endheaders()
            reply = conn.getresponse()
            return reply.status, reply.read().decode("utf-8", "replace")
        except (http.client.RemoteDisconnected, ConnectionError):
            return None, ""
        finally:
            conn.close()

    assert raw({})[0] == 200
    assert raw({"Host": "evil.example"})[0] == 400  # the Host header must be loopback
    assert raw({"Origin": "http://evil.example"})[0] is None  # an Origin request is dropped
    assert raw({"User-Agent": "Mozilla/5.0"})[0] is None  # so is a browser's
    assert raw({}, "POST")[0] == 405  # the stub never writes
    assert len(z.stub.dropped) == 2


def test_oversized_malformed_and_foreign_answers_are_refused_with_a_reason(tmp_path: Path) -> None:
    expected = {
        "oversize": ("budget_exceeded", "larger"),
        "malformed": ("validation_error", "not valid JSON"),
        "duplicate_keys": ("validation_error", "not valid JSON"),
        "not_zotero": ("source_unavailable", "answered"),
    }
    for fault, (code, text) in expected.items():
        lib = Z(tmp_path / fault, fault=fault)
        try:
            with pytest.raises(PrometheosError) as exc:
                lib.plan()
            assert exc.value.code == code and text in exc.value.message, (fault, exc.value.message)
            assert lib.works() == []
        finally:
            lib.close()


def test_import_needs_a_running_zotero_with_its_local_api_on(tmp_path: Path) -> None:
    lib = Z(tmp_path / "off", mode="disabled")
    try:
        with pytest.raises(PrometheosError) as exc:
            lib.plan()
        assert exc.value.code == "source_unavailable"
        assert exc.value.details == {
            "state": "local_api_disabled",
            "run_id": exc.value.details["run_id"],
        }
    finally:
        lib.close()
    gone = Z(tmp_path / "gone")
    gone.stub.stop()
    with pytest.raises(PrometheosError) as exc:
        gone.plan()
    assert exc.value.code == "source_unavailable" and exc.value.details["state"] == "not_running"


def test_scope_and_keys_are_checked(z: Z) -> None:
    for bad in (
        {"collection": "short"},
        {"item_keys": ["lowercase1"]},
        {"collection": "COLL0001", "item_keys": ["DRAG0001"]},
        {"item_keys": ["a"]},
    ):
        with pytest.raises(PrometheosError) as exc:
            z.plan(**bad)
        assert exc.value.code == "validation_error", bad
    assert z.svc.run_list(z.pid, OWNER)["runs"] == []


# ----- the plan ---------------------------------------------------------------------------------


def test_the_plan_lists_items_and_counts_what_it_will_not_import(z: Z) -> None:
    plan = z.plan()
    rows = keys(plan)
    assert set(rows) == {"BOOK0005", "DRAG0001", "EXTR0008", "INJT0004", "PREP0002", "PUBL0003"}
    drag = rows["DRAG0001"]
    assert drag["version"] == 12 and drag["item_type"] == "journalArticle"
    assert drag["title"].startswith("Aerodynamic Drag") and drag["action"] == "create"
    assert drag["identifiers"] == {"doi": DRAG_DOI} and drag["has_abstract"]
    assert rows["PREP0002"]["related"] == {"version_doi": "10.5555/synthetic.2026.040"}
    assert rows["EXTR0008"]["identifiers"] == {"pmcid": "PMC7654321", "pmid": "12345678"}
    assert rows["BOOK0005"]["has_abstract"] is False
    assert plan["counts"]["create"] == 6 and plan["counts"]["skipped_note"] == 1
    assert plan["kind"] == "zotero" and plan["content_trust"] == "untrusted_data"
    assert plan["scope"] == {"kind": "library"} and plan["total"] == 7
    assert z.works() == [] and z.seq() == z.seq()  # planning wrote no work


def test_a_plan_reads_a_collection_or_named_keys_and_counts_attachments(z: Z) -> None:
    assert set(keys(z.plan(collection="COLL0001"))) == {"DRAG0001", "PREP0002", "PUBL0003"}
    named = z.plan(item_keys=["DRAG0001", "ATTC0007", "NOTE0006"])
    assert set(keys(named)) == {"DRAG0001"}
    assert named["skipped"] == {"annotation": 0, "attachment": 1, "note": 1}
    with pytest.raises(PrometheosError) as exc:
        z.plan(collection="ZZZZ9999")
    assert exc.value.code == "not_found"


def test_a_malformed_item_is_refused_with_its_reason_and_the_rest_is_planned(z: Z) -> None:
    z.stub.add({"key": "BAD00001", "version": 3, "data": {"title": "no item type"}})
    z.stub.add({"key": "lowercase", "version": 3, "data": {"itemType": "book"}})
    plan = z.plan()
    assert len(plan["items"]) == 6 and len(plan["refused"]) == 2
    assert {r["reason"] for r in plan["refused"]} == {"malformed_item"}
    assert plan["refused"][0]["key"] == "BAD00001" and plan["refused"][1]["key"] is None
    result = z.commit(plan)
    assert result["counts"]["committed"] == 6 and result["counts"]["refused"] == 2


# ----- the commit -------------------------------------------------------------------------------


def test_items_become_works_whose_provenance_names_the_item_and_a_pointer_that_resolves(
    z: Z,
) -> None:
    result = z.imported()
    assert result["counts"] == {
        "committed": 6, "skipped": 0, "unchanged": 0, "not_selected": 0, "refused": 0
    }  # fmt: skip
    drag = z.work("Aerodynamic Drag")
    assert drag["access_state"] == "abstract_only" and drag["identifiers"] == {"doi": DRAG_DOI}
    body = z.body(drag["work_id"])
    assert body["year"] == 2026 and body["venue"] == "Journal of Flywheel Studies"
    assert body["entry_type"] == "article"
    assert [c["name"] for c in body["contributors"]] == ["Synthetic, A.", "Example, B."]
    records = body["field_provenance"]
    assert {r["basis"] for r in records} == {"zotero"} and {r["entry"] for r in records} == {
        "DRAG0001"
    }
    assert {r["field"] for r in records} >= {
        "title",
        "contributors",
        "year",
        "venue",
        "abstract_ref",
    }
    version_id = records[0]["version_ref"]["id"]
    assert {r["version_ref"]["id"] for r in records} == {version_id}
    version = z.body(version_id)
    source = z.body(version["source_id"])
    assert (
        source["origin"] == "adapter" and source["origin_uri"] == "zotero://users/0/items/DRAG0001"
    )
    assert source["retention"] == "copy" and source["root_binding"] is None
    assert version["mime"] == "application/json" and version["availability"] == "retained"
    data = z.retained(version_id)
    assert hashlib.sha256(data).hexdigest() == version["sha256"]
    item = load_source_json(data)
    assert item["key"] == "DRAG0001" and item["version"] == 12  # the Zotero version is retained
    for record in records:
        assert record["locator"]["type"] == "json"
        value = resolve_pointer(item, record["locator"]["pointer"])
        if record["field"] == "title":
            assert value == body["title"]
        if record["field"] == "abstract_ref":
            assert record["value"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
    ref = body["abstract_ref"]
    assert ref["locator"] == {"type": "json", "pointer": "/data/abstractNote"}
    abstract = next(r for r in records if r["field"] == "abstract_ref")
    assert ref["version_id"] == version_id and ref["quote_sha256"] == abstract["value"]
    assert ref["source_id"] == version["source_id"]
    assert z.work("Rotor dynamics")["access_state"] == "metadata_only"
    assert z.work("Rotor dynamics")["identifiers"] == {"isbn": "9783161484100"}
    assert z.work("Rotor safety")["identifiers"]["pmid"] == "12345678"
    # a Better BibTeX citation key in `extra` is a citation key; an item key is not one
    other = z.body(z.work("Rotor safety")["work_id"])["identifiers"]["other"]
    assert {"scheme": "citekey", "value": "sample2024rotor"} in other
    assert not any(
        o["scheme"] == "citekey" for o in z.body(drag["work_id"])["identifiers"]["other"]
    )
    assert z.svc.library_work(z.pid, OWNER, drag["work_id"])["work"]["sensitivity"] == "normal"


def test_attachments_and_notes_create_nothing(z: Z) -> None:
    before = z.seq()
    z.imported(item_keys=["ATTC0007", "NOTE0006"])
    assert z.seq() == before and z.works() == []


# ----- one DOI from BibTeX, a PDF and Zotero ----------------------------------------------------


def test_one_doi_from_bibtex_and_zotero_is_one_work_with_both_provenance_records(z: Z) -> None:
    write_library_folder(z)
    import_library_folder(z)
    assert len([w for w in z.works() if w["identifiers"].get("doi") == DRAG_DOI]) == 1
    z.imported()
    holders = [w for w in z.works() if w["identifiers"].get("doi") == DRAG_DOI]
    assert len(holders) == 1
    body = z.body(holders[0]["work_id"])
    assert {r["basis"] for r in body["field_provenance"]} == {"bibliography", "zotero"}
    entries = {r["entry"] for r in body["field_provenance"]}
    assert entries == {"drag2026", "DRAG0001"}
    assert holders[0]["sources"] == 2
    # the precedence is one rule: the same winner whatever the import order
    z2 = Z(Path(str(z.env.tmp) + "-b"))
    try:
        write_library_folder(z2)
        z2.imported()
        import_library_folder(z2)
        other = next(w for w in z2.works() if w["identifiers"].get("doi") == DRAG_DOI)
        again = z2.body(other["work_id"])
        for name in ("title", "year", "venue", "contributors", "identifiers"):
            assert again[name] == body[name], name
        assert sorted(
            (r["field"], r["basis"], r["entry"]) for r in again["field_provenance"]
        ) == sorted((r["field"], r["basis"], r["entry"]) for r in body["field_provenance"])
    finally:
        z2.close()


def test_a_pdf_joins_the_work_zotero_made_and_zotero_fields_stay(z: Z) -> None:
    z.imported()
    write_library_folder(z, pdfs_too=True)
    import_library_folder(z)
    holders = [w for w in z.works() if w["identifiers"].get("doi") == DRAG_DOI]
    assert len(holders) == 1 and holders[0]["access_state"] == "full_text"
    body = z.body(holders[0]["work_id"])
    bases = {r["basis"] for r in body["field_provenance"]}
    assert "zotero" in bases and "bibliography" in bases and bases & {"pdf_info", "first_page_text"}
    assert body["abstract_ref"]["locator"]["type"] == "json"  # the Zotero abstract is kept


def test_a_preprint_is_related_to_its_published_version_and_never_merged(z: Z) -> None:
    result = z.imported()
    works = {w["title"]: w for w in z.works()}
    prep, publ = works["Bearing drag in rarefied gas"], works["Bearing Drag in Rarefied Gas"]
    assert prep["work_id"] != publ["work_id"]
    assert prep["identifiers"] == {"arxiv": "2604.00077"} and publ["identifiers"] == {
        "doi": "10.5555/synthetic.2026.040"
    }
    links = [lnk for o in result["outcomes"] for lnk in o.get("links", [])]
    assert links == [{"preprint": prep["work_id"], "published": publ["work_id"]}]
    shown = z.svc.library_work(z.pid, OWNER, prep["work_id"])["versions"]
    assert shown == [{"work_id": publ["work_id"], "title": publ["title"], "role": "published"}]
    with closing(sqlite3.connect(z.env.store_dir / "record.sqlite")) as conn:
        rows = conn.execute(
            "SELECT source_id, target_id FROM relations WHERE relation = 'version_of' "
            "AND retired_seq IS NULL"
        ).fetchall()
    assert rows == [(prep["work_id"], publ["work_id"])]


def test_similar_titles_are_only_proposed(z: Z) -> None:
    z.stub.add(
        {
            **z.stub.item("PUBL0003"),
            "key": "SIMI0009",
            "version": 30,
            "data": {**z.stub.item("PUBL0003")["data"], "key": "SIMI0009", "DOI": "",
                     "extra": "", "title": "Bearing drag in rarefied gas: a note"},
        }
    )  # fmt: skip
    z.imported()
    assert len(z.works()) == 7  # nothing merged by similarity


# ----- idempotence, versions ---------------------------------------------------------------------


def test_importing_the_same_library_again_creates_nothing(z: Z) -> None:
    z.imported()
    seq, revs = z.seq(), {w["work_id"]: w["rev"] for w in z.works()}
    again = z.plan()
    assert {i["action"] for i in again["items"]} == {"unchanged"}
    assert again["counts"]["unchanged"] == 6 and again["counts"]["create"] == 0
    result = z.commit(again)
    assert result["outcomes"] == [] and result["counts"]["committed"] == 0
    assert z.seq() == seq and {w["work_id"]: w["rev"] for w in z.works()} == revs


def test_committing_a_plan_twice_replays_it(z: Z) -> None:
    plan = z.plan()
    first = z.commit(plan)
    seq = z.seq()
    second = z.commit(plan)
    assert z.seq() == seq and len(z.works()) == 6
    assert [o["outcome"] for o in second["outcomes"]] == ["committed"] * 6
    assert all(o["replayed"] for o in second["outcomes"])
    assert [o["works"] for o in first["outcomes"]] == [o["works"] for o in second["outcomes"]]


def test_a_newer_version_of_an_item_revises_its_work(z: Z) -> None:
    z.imported()
    drag = z.work("Aerodynamic Drag")
    old_body = z.body(drag["work_id"])
    old_version = old_body["field_provenance"][0]["version_ref"]["id"]
    new = z.stub.update(
        "DRAG0001", title="Aerodynamic Drag of a Flywheel Housing at Low Pressure, revised"
    )
    plan = z.plan(item_keys=["DRAG0001"])
    row = keys(plan)["DRAG0001"]
    assert row["action"] == "update" and row["version"] == new
    z.commit(plan)
    works = [w for w in z.works() if w["identifiers"].get("doi") == DRAG_DOI]
    assert len(works) == 1 and works[0]["work_id"] == drag["work_id"]
    assert works[0]["rev"] == drag["rev"] + 1 and works[0]["title"].endswith("revised")
    body = z.body(drag["work_id"])
    versions = {r["version_ref"]["id"] for r in body["field_provenance"]}
    assert len(versions) == 1 and versions != {old_version}
    new_item = load_source_json(z.retained(next(iter(versions))))
    assert new_item["version"] == new
    assert load_source_json(z.retained(old_version))["version"] == 12  # the old bytes stay
    # one source for the item, two retained versions
    assert z.body(old_version)["source_id"] == z.body(next(iter(versions)))["source_id"]
    # and re-importing that version creates nothing
    seq = z.seq()
    z.imported(item_keys=["DRAG0001"])
    assert z.seq() == seq


def test_a_newer_version_with_no_mapped_change_is_still_kept(z: Z) -> None:
    z.imported()
    z.stub.update("DRAG0001", tags=[{"tag": "flywheel"}, {"tag": "new tag"}])
    plan = z.plan(item_keys=["DRAG0001"])
    assert keys(plan)["DRAG0001"]["action"] == "update"
    z.commit(plan)
    body = z.body(z.work("Aerodynamic Drag")["work_id"])
    item = load_source_json(z.retained(body["field_provenance"][0]["version_ref"]["id"]))
    assert item["data"]["tags"][-1] == {"tag": "new tag"}


# ----- selecting, changed items, closed Zotero ---------------------------------------------------


def test_only_the_selected_items_are_committed(z: Z) -> None:
    plan = z.plan()
    result = z.commit(plan, item_keys=["PREP0002", "BOOK0005"])
    assert sorted(o["key"] for o in result["outcomes"]) == ["BOOK0005", "PREP0002"]
    assert result["counts"]["committed"] == 2 and result["counts"]["not_selected"] == 4
    assert len(z.works()) == 2
    with pytest.raises(PrometheosError) as exc:
        z.commit(plan, item_keys=["NOTE0006"])
    assert exc.value.code == "validation_error" and "NOTE0006" in exc.value.message
    rest = z.commit(plan)  # the rest of the same plan
    assert rest["counts"]["committed"] == 6  # the two replay, the other four are new
    assert len(z.works()) == 6


def test_an_item_that_changed_since_the_plan_is_skipped_not_imported(z: Z) -> None:
    plan = z.plan()
    z.stub.update("BOOK0005", publisher="Another Press")
    result = z.commit(plan)
    skipped = [o for o in result["outcomes"] if o["outcome"] == "skipped"]
    assert [(o["key"], o["reason"]) for o in skipped] == [("BOOK0005", "changed")]
    assert result["counts"]["committed"] == 5 and len(z.works()) == 5


def test_an_item_deleted_from_zotero_since_the_plan_is_skipped_as_gone(z: Z) -> None:
    plan = z.plan()
    z.stub.items[:] = [i for i in z.stub.items if i["key"] != "INJT0004"]
    result = z.commit(plan)
    skipped = [(o["key"], o["reason"]) for o in result["outcomes"] if o["outcome"] == "skipped"]
    assert skipped == [("INJT0004", "gone")] and len(z.works()) == 5


def test_a_zotero_that_stops_during_the_commit_fails_the_run_and_keeps_what_was_committed(
    z: Z,
) -> None:
    plan = z.plan()
    z.stub.stop()
    with pytest.raises(PrometheosError) as exc:
        z.commit(plan)
    assert exc.value.code == "source_unavailable"
    assert z.works() == []  # nothing was half written
    z.stub = ZoteroStub(port=z.port)  # Zotero comes back on the same port
    z.stub.start()
    done = z.commit(plan)  # the same plan is still good (it is an hour old at most)
    assert done["counts"]["committed"] == 6


def test_an_expired_or_unknown_plan_is_refused(z: Z) -> None:
    with pytest.raises(PrometheosError) as exc:
        z.svc.zotero_import_commit(
            z.pid, "00000000-0000-4000-8000-000000000000", OWNER, authority=OWNER_ACT
        )
    assert exc.value.code == "not_found"
    plan = z.plan()
    with closing(sqlite3.connect(z.env.store_dir / "record.sqlite")) as conn:
        conn.execute("UPDATE library_plans SET plan = replace(plan, ?, ?)",
                     (plan["expires_at"], "2020-01-01T00:00:00Z"))  # fmt: skip
        conn.commit()
    with pytest.raises(PrometheosError) as exc:
        z.commit(plan)
    assert exc.value.code == "stale_task"


def test_a_file_plan_is_not_a_zotero_plan(z: Z) -> None:
    write_library_folder(z)
    plan = z.svc.library_import_plan(z.pid, OWNER, root_alias="research", path="library")
    with pytest.raises(PrometheosError) as exc:
        z.svc.zotero_import_commit(z.pid, plan["plan_id"], OWNER, authority=OWNER_ACT)
    assert exc.value.code == "not_found"


# ----- untrusted data ---------------------------------------------------------------------------


def test_injected_instructions_in_an_abstract_stay_inert_text(z: Z) -> None:
    before_approvals = z.svc.approval_list(z.pid, OWNER)["approvals"]
    plan = z.plan(AGENT)
    assert "Ignore all previous" not in json.dumps(plan)  # the plan shows titles, not abstracts
    asked = z.ask(plan)  # the agent asks; the owner grants; the agent resumes
    z.grant(asked["approval_request_id"])
    resumed = z.svc.approval_resume(z.pid, AGENT, asked["approval_request_id"])
    result = resumed["result"]
    assert result["counts"]["committed"] == 6
    assert "evil.example" not in json.dumps(result)
    # nothing was requested or decided beyond the import's own approval
    approvals = z.svc.approval_list(z.pid, OWNER)["approvals"]
    assert [a["operation"] for a in approvals] == ["zotero_import_commit"] + [
        a["operation"] for a in before_approvals
    ]
    assert z.svc.library_dedup(z.pid, OWNER)["decisions"] == []  # nothing was decided
    assert len(z.works()) == 6  # nothing merged
    hostile = z.work("A note on flywheel housing safety")
    body = z.body(hostile["work_id"])
    assert body["access_state"] == "abstract_only"
    item = load_source_json(z.retained(body["abstract_ref"]["version_id"]))
    assert "Ignore all previous instructions" in item["data"]["abstractNote"]  # kept as data
    assert (
        body["abstract_ref"]["quote_sha256"]
        == hashlib.sha256(item["data"]["abstractNote"].encode("utf-8")).hexdigest()
    )
    shown = json.dumps(z.svc.library_work(z.pid, AGENT, hostile["work_id"]))
    assert "untrusted_data" in shown


def test_an_agent_may_plan_but_committing_is_the_owners_decision(z: Z) -> None:
    """Decision 0070 section 7: reading the owner's reference library into the project is of
    the same kind as adding a research folder. The probe and the plan are open to an agent."""
    with pytest.raises(PrometheosError) as exc:
        z.plan(AGENT, sensitivity="private")
    assert exc.value.code == "policy_denied"
    assert z.svc.zotero_status(z.pid, AGENT)["state"] == "available"
    plan = z.plan(AGENT)
    seq = z.seq()
    # Without an owner act the service refuses; an agent's request is an approval request.
    for authority in (None, AGENT, object()):
        with pytest.raises(PrometheosError) as refused:
            z.svc.zotero_import_commit(z.pid, plan["plan_id"], AGENT, authority=authority)
        assert refused.value.code == "policy_denied", authority
    asked = z.ask(plan, item_keys=["DRAG0001", "PUBL0003"])
    assert z.seq() == seq and z.works() == []  # only the request was stored (not a record)
    view = z.svc.approval_owner_view(z.pid, asked["approval_request_id"])
    line = " ".join(view["summary"])
    assert "2 item(s)" in line and "owner's local Zotero" in line and "Aerodynamic" in line
    # a request for what the plan does not hold, or for an expired or unknown plan, is refused
    for params in (
        {"plan_id": plan["plan_id"], "item_keys": ["NOTE0006"]},
        {"plan_id": "00000000-0000-4000-8000-000000000000"},
    ):
        with pytest.raises(PrometheosError) as refused:
            z.svc.approval_request(z.pid, AGENT, "zotero_import_commit", params)
        assert refused.value.code in ("validation_error", "not_found"), params
    # pending: not resumable; granted: resumed once, exactly the named items, and it replays
    with pytest.raises(PrometheosError) as pending:
        z.svc.approval_resume(z.pid, AGENT, asked["approval_request_id"])
    assert pending.value.code == "approval_required"
    z.grant(asked["approval_request_id"])
    done = z.svc.approval_resume(z.pid, AGENT, asked["approval_request_id"])
    assert done["state"] == "used"
    assert sorted(o["key"] for o in done["result"]["outcomes"]) == ["DRAG0001", "PUBL0003"]
    assert len(z.works()) == 2
    again = z.svc.approval_resume(z.pid, AGENT, asked["approval_request_id"])
    assert again["replayed"] is True and len(z.works()) == 2
    record = z.svc.get(z.pid, OWNER, z.work("Aerodynamic Drag")["work_id"], None)
    assert record["sensitivity"] == "normal"  # at the agent's rank, as planned


def test_an_agent_cannot_ask_to_commit_a_plan_the_owner_made(z: Z) -> None:
    plan = z.plan()  # an owner-rank plan
    with pytest.raises(PrometheosError) as exc:
        z.svc.approval_request(z.pid, AGENT, "zotero_import_commit", {"plan_id": plan["plan_id"]})
    assert exc.value.code == "not_found"


def test_an_agent_cannot_ask_to_commit_an_expired_plan(z: Z) -> None:
    plan = z.plan(AGENT)
    with closing(sqlite3.connect(z.env.store_dir / "record.sqlite")) as conn:
        conn.execute(
            "UPDATE library_plans SET plan = replace(plan, ?, ?)",
            (plan["expires_at"], "2020-01-01T00:00:00Z"),
        )
        conn.commit()
    with pytest.raises(PrometheosError) as exc:
        z.svc.approval_request(z.pid, AGENT, "zotero_import_commit", {"plan_id": plan["plan_id"]})
    assert exc.value.code == "stale_task"


def test_a_grant_binds_the_plan_it_was_given_for(z: Z) -> None:
    plan = z.plan(AGENT)
    asked = z.ask(plan)
    z.grant(asked["approval_request_id"])
    with closing(sqlite3.connect(z.env.store_dir / "record.sqlite")) as conn:
        conn.execute(  # the stored plan is no longer the one the owner saw
            "UPDATE library_plans SET plan = replace(plan, ?, ?)",
            (plan["plan_sha256"], "0" * 64),
        )
        conn.commit()
    with pytest.raises(PrometheosError) as changed:
        z.svc.approval_resume(z.pid, AGENT, asked["approval_request_id"])
    assert changed.value.code == "revision_conflict"
    assert z.works() == []


def test_the_item_key_is_not_a_citation_key_but_the_work_exports_with_one(z: Z) -> None:
    z.imported()
    out = z.svc.library_export(z.pid, OWNER, "bibtex", None)
    assert "synthetic2026aerodynamic" in out["content"]  # a deterministic key, not DRAG0001
    assert "sample2024rotor" in out["content"] and "DRAG0001" not in out["content"]


def test_search_finds_zotero_titles_through_knowledge_search(z: Z) -> None:
    z.imported()
    hits = z.svc.search(z.pid, OWNER, "rarefied", 10)["hits"]
    assert any(h["kind"] == "work" and "rarefied" in h["title"].lower() for h in hits)
    assert any(h["kind"] == "source" and h["title"].startswith("Zotero item") for h in hits)


def test_the_stub_is_loopback_only_and_nothing_else_was_contacted(z: Z) -> None:
    ip = socket.gethostbyname("localhost")
    assert ip.startswith("127.")
    z.imported()
    assert {r["host"].split(":")[0] for r in z.get_requests()} == {"127.0.0.1"}


# ----- an answer that is not JSON must never crash a command (S11.5 review round 1, F1) -----------


def test_an_integer_past_the_conversion_limit_is_invalid_json_not_a_crash(tmp_path: Path) -> None:
    lib = Z(tmp_path, fault="bigint")
    try:
        # The adapter's probe, and the service's, answer a state; neither raises.
        out = zotero.status(lib.env.home)
        assert out["state"] == "unexpected" and "not JSON" in out["detail"], out
        assert lib.svc.zotero_status(lib.pid, OWNER, collections=True)["state"] == "unexpected"
        # Planning refuses with a reason, and nothing is stored.
        with pytest.raises(PrometheosError) as exc:
            lib.plan()
        assert exc.value.code == "validation_error" and "not valid JSON" in exc.value.message
        assert lib.works() == []
        with pytest.raises(PrometheosError) as listed:
            zotero.list_collections(zotero.endpoint(lib.env.home))
        assert listed.value.code == "validation_error"
    finally:
        lib.close()


def test_the_same_input_is_refused_when_an_item_is_re_read_for_the_commit(z: Z) -> None:
    plan = z.plan()
    z.stub.fault = "bigint"  # Zotero now answers every request with the hostile document
    with pytest.raises(PrometheosError) as exc:
        z.commit(plan)
    assert exc.value.code == "validation_error" and "not valid JSON" in exc.value.message
    assert z.works() == []


def test_an_item_whose_version_is_not_a_sane_number_is_refused_per_item(z: Z) -> None:
    z.stub.add({"key": "BIGV0001", "version": 10**30, "data": {"itemType": "book", "title": "x"}})
    plan = z.plan()
    assert [r["key"] for r in plan["refused"]] == ["BIGV0001"]
    assert plan["refused"][0]["reason"] == "malformed_item" and len(plan["items"]) == 6


def test_loads_strict_turns_every_invalid_document_into_one_error() -> None:
    from prometheos.domain.canonical import CanonicalJSONError, loads_strict

    for text in ("[" + "9" * 4400 + "]", '{"a": }', '{"a": 1, "a": 2}', "[NaN]", "[1e999]"):
        with pytest.raises(CanonicalJSONError):
            loads_strict(text)


# ----- the command line (S11.5 review round 1, F3) ------------------------------------------------


def parsed(cli: Any, *args: str) -> tuple[int, Any]:
    code, out, _err = cli(*args)
    return int(code), out


def test_the_command_line_probes_plans_and_asks_for_the_commit(z: Z) -> None:
    from test_cp1_cli import Cli

    cli = Cli(z.env.tmp)
    code, out = parsed(cli, "zotero", "status", "--collections")
    assert code == 0 and out["state"] == "available"
    assert [c["name"] for c in out["collections"]] == ["Flywheels", "Misc"]
    code, planned = parsed(cli, "zotero", "import", "--plan", "--collection", "COLL0001")
    assert code == 0
    assert {i["key"] for i in planned["items"]} == {"DRAG0001", "PREP0002", "PUBL0003"}
    code, named = parsed(
        cli, "zotero", "import", "--plan", "--key", "BOOK0005", "--key", "EXTR0008"
    )
    assert code == 0 and {i["key"] for i in named["items"]} == {"BOOK0005", "EXTR0008"}
    # wrong use is refused with exit 2
    assert parsed(cli, "zotero", "import")[0] == 2
    assert parsed(cli, "zotero", "import", "--plan", "--commit", planned["plan_id"])[0] == 2
    assert parsed(cli, "zotero", "import", "--plan", "--collection", "short")[0] == 2
    both = parsed(
        cli, "zotero", "import", "--commit", planned["plan_id"], "--collection", "COLL0001"
    )
    assert both[0] == 2
    # without an interactive terminal the commit is a request (exit 4); nothing is written
    code, asked = parsed(
        cli, "zotero", "import", "--commit", planned["plan_id"], "--key", "DRAG0001"
    )
    assert code == 4 and asked["error"]["code"] == "approval_required", asked
    assert z.works() == []
    approval = asked["error"]["details"]["approval_request_id"]
    z.grant(approval)
    code, done = parsed(cli, "approval", "resume", approval)
    assert code == 0 and done["state"] == "used"
    assert [o["key"] for o in done["result"]["outcomes"]] == ["DRAG0001"]
    assert [w["title"].split()[0] for w in z.works()] == ["Aerodynamic"]
    # a Zotero that is not running is exit 5; the probe itself still answers
    z.stub.stop()
    assert parsed(cli, "zotero", "status")[1]["state"] == "not_running"
    code, gone = parsed(cli, "zotero", "import", "--plan")
    assert code == 5 and gone["error"]["details"]["state"] == "not_running"
    # a host that is not loopback is refused (exit 4) and the probe says so
    z.point_at(z.port, "example.com")
    assert parsed(cli, "zotero", "status")[1]["state"] == "unexpected"
    code, denied = parsed(cli, "zotero", "import", "--plan")
    assert code == 4 and denied["error"]["code"] == "policy_denied"


# ----- the MCP tools (S11.5 review round 1, F3) ---------------------------------------------------


def mcp(z: Z, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    async def run() -> Any:
        async with Client(build_server(z.svc, z.pid)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(run)
    if result.is_error:
        text = result.content[0].text
        return False, json.loads(text[text.index("{") :])["error"]
    return True, result.structured_content


def test_the_mcp_tools_plan_and_the_commit_is_an_approval_request(z: Z) -> None:
    ok, planned = mcp(z, "zotero_import_plan", {"collection": "COLL0001", "wait_seconds": 20})
    assert ok and planned["state"] == "completed"
    plan = planned["result"]
    assert {i["key"] for i in plan["items"]} == {"DRAG0001", "PREP0002", "PUBL0003"}
    ok, status = mcp(z, "library", {"request": {"op": "zotero_status", "collections": True}})
    assert ok and status["result"]["state"] == "available"
    ok, _bad = mcp(z, "zotero_import_plan", {"collection": "COLL0001", "item_keys": ["DRAG0001"]})
    assert not ok  # collection and keys together: refused before anything is read
    seq = z.seq()
    ok, asked = mcp(
        z, "zotero_import_commit", {"plan_id": plan["plan_id"], "item_keys": ["PUBL0003"]}
    )
    assert not ok and asked["code"] == "approval_required", asked
    assert z.works() == [] and z.seq() == seq
    approval = asked["details"]["approval_request_id"]
    ok, early = mcp(z, "approval_resume", {"approval_id": approval})
    assert not ok and early["code"] == "approval_required"  # not granted yet
    z.grant(approval)
    ok, resumed = mcp(z, "approval_resume", {"approval_id": approval})
    assert ok and resumed["state"] == "used"
    assert [o["key"] for o in resumed["result"]["outcomes"]] == ["PUBL0003"]
    assert [w["title"] for w in z.works()] == ["Bearing Drag in Rarefied Gas"]


def test_the_mcp_status_of_a_stopped_zotero_is_a_state_not_an_error(z: Z) -> None:
    z.stub.stop()
    ok, status = mcp(z, "library", {"request": {"op": "zotero_status"}})
    assert ok and status["result"]["state"] == "not_running"
    ok, run = mcp(z, "zotero_import_plan", {"wait_seconds": 20})
    assert ok and run["state"] == "failed"  # the run ended with the reason
    assert run["error"]["code"] == "source_unavailable"
    assert run["error"]["details"]["state"] == "not_running"


# ----- the agent's HTTP routes --


def test_the_http_routes_agent_asks_owner_commits(z: Z) -> None:
    from hub_support import running_hub

    with running_hub(z.svc, z.pid) as hub:
        hub.pair()
        agent = {"Authorization": f"Bearer {hub.gate.new_agent_token()}"}

        def call(method: str, path: str, body: Any = None, *, as_agent: bool) -> Any:
            if as_agent:
                return hub.request(
                    method, path, body, cookie=None, csrf=None, origin=None, headers=agent
                )
            return hub.request(method, path, body)

        planned = call("POST", "/api/v1/zotero/plans", {"wait_seconds": 20}, as_agent=True)
        assert planned.status == 200 and planned.json()["state"] == "completed"
        plan_id = planned.json()["result"]["plan_id"]
        assert call("GET", "/api/v1/zotero/status", as_agent=True).json()["state"] == "available"
        path = f"/api/v1/zotero/plans/{plan_id}/commit"
        asked = call("POST", path, {"item_keys": ["BOOK0005"]}, as_agent=True)
        assert asked.status == 403 and asked.json()["error"]["code"] == "approval_required"
        assert z.works() == []
        body = {"item_keys": ["BOOK0005"], "wait_seconds": 20}
        direct = call("POST", path, body, as_agent=False)
        assert direct.status == 200 and direct.json()["state"] == "completed"
        assert [w["title"] for w in z.works()] == ["Rotor dynamics: a synthetic primer"]
