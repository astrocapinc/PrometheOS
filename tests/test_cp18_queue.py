"""CP18.3 the agent-visible queue (A05; C18-QUEUE; decision 0035).

The queue lists open work items (deadline, reason, description size) and unacknowledged events
(type, subject, reason) in one list, ordered by due time and then age, each with a stable
reference. Reading it, or the event list, changes nothing: the whole store's logical content is
the same before and after, through the service, the CLI, local HTTP and MCP. The MCP server
runs one detector pass when it starts (never later, never inside a read), and an agent's
acknowledgement is an approval request.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create
from hub_support import running_hub
from mcp import Client, StdioServerParameters
from test_cp1_cli import Cli
from test_cp18_events import iso, store_digest
from test_cp18_queue_text import QUEUE_TOOLS, tool_description
from test_cp18_work_items import DESCRIPTION, add_work_item, adopt, work_body

from prometheos.domain.ids import new_id
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def owner_item(env: Env, **overrides: Any) -> str:
    """A work item the owner adds in the hub: owner scope, so its own events are recorded
    acknowledged (decision 0035, item 4)."""
    action_id = new_id("action")
    env.apply_protected([create("action", work_body(**overrides), action_id), adopt(action_id)],
                        caller=OWNER)  # fmt: skip
    return action_id


def seeded(env: Env) -> dict[str, str]:
    """Items due in 1, 6 and 13 days, one without a deadline, a suggestion, and the detector
    pass that turns the deadlines into events."""
    now = datetime.now(UTC)

    def due(days: int) -> str:
        return iso(now + timedelta(days=days))

    items = {
        "one": owner_item(env, title="Group-meeting slides", due_at=due(1)),
        "six": owner_item(env, title="Referee report", due_at=due(6)),
        "thirteen": owner_item(env, title="Submit paper P1 to journal X", due_at=due(13)),
        "none": owner_item(env, title="Tidy the figures", due_at=None, description=None),
    }
    suggestion = new_id("action")
    env.apply([create("action", work_body(title="Agent idea", due_at=None), suggestion)],
              caller=AGENT)  # fmt: skip
    items["suggested"] = suggestion
    env.svc.events_detect(env.project_id)
    return items


def lazy_writes_pending(env: Env) -> None:
    """State a read must not tidy (review s2-a F2): a pending approval past its expiry and a
    running run whose worker is gone. Listing approvals would expire the first and reading a
    run would recover the second; the queue and the event list must do neither."""
    from test_cp18_events import _requested, sql

    approval_id = _requested(env)
    sql(env.store_dir, "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", approval_id))  # fmt: skip
    view = env.svc.import_plan(
        env.project_id, AGENT, mode="none", root_alias="research", paths=["baseline.md"]
    )
    call = json.dumps({"provider": "test", "external_ref": None, "request_sha256": None,
                       "started_at": "2000-01-01T00:00:00.000000Z"})  # fmt: skip
    sql(env.store_dir, "UPDATE operation_runs SET state = 'running', lease_owner = 'gone', "
        "lease_expires_at = ?, external_effect = 'started', external_call = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000000Z", call, view["run_id"]))  # fmt: skip


def test_queue_lists_items_and_events_by_due_time_then_age(env: Env) -> None:
    items = seeded(env)
    queue = env.svc.work_queue(env.project_id, AGENT, proposed="list")
    assert queue["read_only"] is True and queue["timezone"] == "Europe/London"
    entries = queue["entries"]
    kinds = [(e["entry"], e.get("type")) for e in entries]
    refs = [e["ref"] for e in entries]
    # Each deadline: its item, then its notice (the item is older); undated entries last.
    assert refs[:6] == [
        items["one"], refs[1], items["six"], refs[3], items["thirteen"], refs[5]
    ]  # fmt: skip
    assert kinds[1] == kinds[3] == kinds[5] == ("event", "deadline.approaching")
    notices = {e["subject_ref"]["id"]: e["data"]["days_before"] for e in entries if e.get("type")
               == "deadline.approaching"}  # fmt: skip
    assert notices == {items["one"]: 2, items["six"]: 7, items["thirteen"]: 14}
    titles = {e["subject_ref"]["id"]: e["subject_title"] for e in entries if e["entry"] == "event"}
    assert titles[items["one"]] == "Group-meeting slides"  # joined when read, never stored
    undated = [e for e in entries if e["due_at"] is None]
    assert {e["ref"] for e in undated if e["entry"] == "work_item"} == {
        items["none"], items["suggested"]
    }  # fmt: skip
    assert queue["counts"] == {"work_items": 5, "proposed": 1, "events": len(entries) - 5}
    one = next(e for e in entries if e["ref"] == items["one"])
    assert one["reason"] == "Ready; due within a day."
    assert one["description_chars"] == len(DESCRIPTION) and one["labels"] == [
        "paper P1",
        "submission",
    ]
    assert "description" not in one  # its size, never the text
    suggested = next(e for e in entries if e["ref"] == items["suggested"])
    assert suggested["status"] == "proposed" and suggested["reason"].startswith("Suggested")
    # The agent's suggestion raised an open work_item.created event (the owner's own did not).
    created = [e for e in entries if e.get("type") == "work_item.created"]
    assert [e["subject_ref"]["id"] for e in created] == [items["suggested"]]
    # Every event reference is stable: the event list returns the same IDs.
    listed = {e["event_id"] for e in env.svc.event_list(env.project_id, AGENT)["items"]}
    assert {e["ref"] for e in entries if e["entry"] == "event"} == listed


def test_queue_hides_what_the_caller_may_not_see_and_closed_items(env: Env) -> None:
    hidden = new_id("action")
    env.apply([create("action", work_body(title="Private matter"), hidden,
                      sensitivity="restricted")], caller=OWNER)  # fmt: skip
    done = add_work_item(env, title="Finished")
    env.apply(
        [{"op": "transition", "id": done, "base_rev": 2, "to": "cancelled", "reason": None,
          "evidence": []}], caller=AGENT,
    )  # fmt: skip
    agent = [
        e["ref"] for e in env.svc.work_queue(env.project_id, AGENT, proposed="list")["entries"]
    ]
    owner = [
        e["ref"] for e in env.svc.work_queue(env.project_id, OWNER, proposed="list")["entries"]
    ]
    assert hidden not in agent and hidden in owner
    assert done not in agent and done not in owner


def test_reading_the_queue_and_events_changes_nothing_on_service_cli_and_http(
    env: Env, tmp_path: Path
) -> None:
    seeded(env)
    lazy_writes_pending(env)
    before = store_digest(env.store_dir)
    for caller in (OWNER, AGENT):
        env.svc.work_queue(env.project_id, caller)
        env.svc.event_list(env.project_id, caller, state="all")
    cli = Cli(tmp_path)
    for args in (("queue",), ("events", "list"), ("events", "list", "--state", "all")):
        code, document, _ = cli(*args, "--project", env.project_id)
        assert code == 0, document
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        token = hub.gate.new_agent_token()
        for path in ("/api/v1/queue", "/api/v1/events?state=all"):
            assert hub.request("GET", path).status == 200
            agent = hub.request(
                "GET", path, cookie=None, csrf=None, origin=None,
                headers={"Authorization": f"Bearer {token}"},
            )  # fmt: skip
            assert agent.status == 200, agent.body
    assert store_digest(env.store_dir) == before
    # Control: a read that tidies lazily (the approval listing expires the stale request) does
    # change the digest, so the seeded state would expose an impure queue or event list.
    env.svc.approval_list(env.project_id, OWNER)
    assert store_digest(env.store_dir) != before


def test_reading_through_mcp_changes_nothing_and_descriptions_match(env: Env) -> None:
    seeded(env)
    lazy_writes_pending(env)

    async def main() -> tuple[str, str, Any, Any, dict[str, str]]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            tools = {t.name: t.description or "" for t in (await client.list_tools()).tools}
            before = store_digest(env.store_dir)
            queue = await client.call_tool("work_queue", {})
            listed = await client.call_tool("event_list", {"state": "all"})
            return before, store_digest(env.store_dir), queue, listed, tools

    before, after, queue, listed, tools = anyio.run(main)
    assert before == after
    assert not queue.is_error and queue.structured_content["read_only"] is True
    assert not listed.is_error and listed.structured_content["items"]
    for name in QUEUE_TOOLS:
        assert inspect.cleandoc(tools[name]) == tool_description(name), name


def test_an_agent_acknowledgement_is_an_approval_request_everywhere(
    env: Env, tmp_path: Path
) -> None:
    seeded(env)
    (event, *_) = env.svc.event_list(env.project_id, AGENT)["items"]

    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await client.call_tool("event_acknowledge", {"event_ids": [event["event_id"]]})

    asked = anyio.run(main)
    assert asked.is_error and '"approval_required"' in asked.content[0].text  # type: ignore[union-attr]
    code, document, _ = Cli(tmp_path)(
        "events", "ack", event["event_id"], "--project", env.project_id
    )  # no terminal: an approval request
    assert code == 4 and document["error"]["code"] == "approval_required"
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        token = hub.gate.new_agent_token()
        body = {"event_ids": [event["event_id"]]}
        agent = hub.request("POST", "/api/v1/events/acknowledge", body, cookie=None,
                            csrf=None, origin=None,
                            headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        assert agent.status != 200 and agent.json()["error"]["code"] == "approval_required"
        open_ids = {e["event_id"] for e in env.svc.event_list(env.project_id, OWNER)["items"]}
        assert event["event_id"] in open_ids  # nothing acknowledged by any request
        owner = hub.request("POST", "/api/v1/events/acknowledge", body | {"note": "Seen."})
        assert owner.status == 200, owner.body
        (acked,) = owner.json()["acknowledged"]
        assert acked["acknowledged_by"] == "hub-owner" and acked["acknowledged_note"] == "Seen."


def test_the_mcp_server_runs_one_detector_pass_when_it_starts(env: Env) -> None:
    """Opening a session starts the server (the coding host launches it); its start runs the
    detectors once, so a deadline crossed since the last check is in the queue."""
    item = add_work_item(env, due_at=iso(datetime.now(UTC) + timedelta(days=3)))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "prometheos", "mcp", "serve", "--stdio", "--project", env.project_id],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
    )

    async def main() -> Any:
        async with Client(params) as client:
            first = await client.call_tool("work_queue", {})
            second = await client.call_tool("work_queue", {})
            return first.structured_content, second.structured_content

    first, second = anyio.run(main)
    notices = [e for e in first["entries"] if e.get("type") == "deadline.approaching"]
    assert [n["subject_ref"]["id"] for n in notices] == [item]
    assert first["entries"] == second["entries"]  # no detection between reads
    assert json.dumps(first).count("deadline.approaching") == 1


# ----- decision 0078: suggested items fold into groups by default --------------------------


def suggest(env: Env, title: str, labels: list[str], due_at: str | None = None) -> str:
    action_id = new_id("action")
    env.apply(
        [
            create(
                "action",
                work_body(title=title, due_at=due_at, labels=labels, description=None),
                action_id,
            )
        ],
        caller=AGENT,
    )
    return action_id


def test_the_queue_folds_suggested_items_by_label_and_lists_them_on_request(env: Env) -> None:
    """The KERS case (P20): an adoption plan files dozens of suggestions. Folded, the agent sees
    the adopted work and the events one by one and each label's suggestions as one group with a
    count, its first deadline and three titles; ``proposed="list"`` gives every entry as before,
    and the counts are the same either way."""
    adopted = owner_item(env, title="Draft P3", due_at=None)
    plan = [
        suggest(env, f"Adoption: Decide the current file of f{i}", ["adoption", "resolve-conflict"])
        for i in range(5)
    ]
    soon = iso(datetime.now(UTC) + timedelta(days=3))
    dated = suggest(env, "Meeting follow-up", ["meeting"], due_at=soon)
    bare = suggest(env, "An idea", [])

    folded = env.svc.work_queue(env.project_id, AGENT)
    listed = env.svc.work_queue(env.project_id, AGENT, proposed="list")
    assert folded["proposed"] == "fold" and listed["proposed"] == "list"
    assert folded["counts"] == listed["counts"]
    assert folded["counts"]["work_items"] == 8 and folded["counts"]["proposed"] == 7
    # Each agent suggestion raised a "work item was added" event; it folds with its group.
    assert {e.get("subject_ref", {}).get("id") for e in folded["entries"]} & {
        *plan,
        dated,
        bare,
    } == set()
    assert [g["events"] for g in folded["proposed_groups"]] == [1, 5, 1]
    refs = {e["ref"] for e in folded["entries"]}
    assert adopted in refs and not refs & {*plan, dated, bare}
    assert {*plan, dated, bare, adopted} <= {e["ref"] for e in listed["entries"]}
    assert listed["proposed_groups"] == []

    groups = folded["proposed_groups"]
    # A group with a deadline first, then by size; examples in queue order, at most three.
    assert [(g["label"], g["count"]) for g in groups] == [
        ("meeting", 1),
        ("adoption", 5),
        (None, 1),
    ]
    assert groups[0]["next_due_at"] == soon and groups[1]["next_due_at"] is None
    assert groups[1]["examples"] == [f"Adoption: Decide the current file of f{i}" for i in range(3)]
    assert all(g["oldest_since"] for g in groups)

    with pytest.raises(Exception, match="proposed must be fold or list"):
        env.svc.work_queue(env.project_id, AGENT, proposed="all")


def test_a_folded_queue_is_smaller_on_every_transport(env: Env) -> None:
    """MCP takes the same option, folds by default and still reads nothing it may not."""
    for i in range(30):
        suggest(env, f"Adoption: step {i}", ["adoption"])

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            folded = (await client.call_tool("work_queue", {})).structured_content
            listed = (await client.call_tool("work_queue", {"proposed": "list"})).structured_content
            return folded, listed

    folded, listed = anyio.run(main)
    # The 30 suggestions and their 30 "work item was added" events become one group.
    assert folded["entries"] == []
    assert (folded["proposed_groups"][0]["count"], folded["proposed_groups"][0]["events"]) == (
        30,
        30,
    )
    assert folded["counts"] == listed["counts"]
    assert len([e for e in listed["entries"] if e["entry"] == "work_item"]) == 30
    assert len(json.dumps(folded)) * 5 < len(json.dumps(listed))


def test_folding_never_shows_what_the_caller_may_not_see(env: Env) -> None:
    """A restricted suggestion is neither counted nor quoted for an agent, folded or not."""
    action_id = new_id("action")
    env.apply(
        [create("action", work_body(title="ZQXSECRET plan", due_at=None, labels=["adoption"],
                                    description=None), action_id, sensitivity="restricted")],
        caller=OWNER,
    )  # fmt: skip
    for _ in range(3):
        suggest(env, "Adoption: visible step", ["adoption"])
    agent = env.svc.work_queue(env.project_id, AGENT)
    owner = env.svc.work_queue(env.project_id, OWNER)
    assert "ZQXSECRET" not in json.dumps(agent) and action_id not in json.dumps(agent)
    assert agent["counts"]["proposed"] == 3 and agent["proposed_groups"][0]["count"] == 3
    assert (
        owner["counts"]["proposed"] == 4
        and "ZQXSECRET plan" in owner["proposed_groups"][0]["examples"]
    )


def test_a_deadline_event_about_a_suggestion_stays_listed_and_limit_holds(env: Env) -> None:
    """Only a suggestion's own added/changed events fold; a deadline is a fact the agent reports.
    ``limit`` and ``truncated`` apply to the listed entries; the groups are always complete."""
    soon = iso(datetime.now(UTC) + timedelta(days=1))
    dated = suggest(env, "Reply to the reviewer", ["review"], due_at=soon)
    owner_item(env, title="Draft P3", due_at=None)
    owner_item(env, title="Check figure 4", due_at=None)
    env.svc.events_detect(env.project_id)
    folded = env.svc.work_queue(env.project_id, AGENT)
    kept = [
        e for e in folded["entries"] if e["entry"] == "event" and e["subject_ref"]["id"] == dated
    ]
    assert [e["type"] for e in kept] == ["deadline.approaching"]
    assert folded["proposed_groups"][0]["events"] == 1  # its work_item.created
    first = env.svc.work_queue(env.project_id, AGENT, limit=1)
    assert len(first["entries"]) == 1 and first["truncated"] is True
    assert first["proposed_groups"] == folded["proposed_groups"]
