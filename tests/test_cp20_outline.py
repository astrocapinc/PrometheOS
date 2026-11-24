"""CP20.4 part 6b (A06.1; C20-ADOPT; decision 0106): the adoption outline, proposed by rules with
every item citing its file and line, and confirmed by the owner once, in one transaction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from cp1_support import OWNER_ACT

from prometheos.app.reads import Caller
from prometheos.domain.models import PrometheosError

AGENT = Caller(actor="test", scope="agent")
OWNER = Caller(actor="owner", scope="owner")


def write(root: Path, rel: str, text: str, mtime: float | None = None) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def forking_folder(root: Path) -> None:
    """A small repository that forks by track: one script and its output per track, a paper,
    notes that ask questions, and one output older than its script."""
    write(root, "README.md", "# Flywheel KERS study\n\n"
          "RQ1: Does flywheel storage cut the energy use of a city bus?\n\n"
          "- Which damper keeps the ring stable at full speed?\n\n"
          "This line is not a question.\n")  # fmt: skip
    write(root, "paper/main.tex", "\\documentclass{article}\n\\title{Kinematic limits of a "
          "flywheel KERS}\n\\begin{document}\n\\maketitle\n\\end{document}\n")  # fmt: skip
    write(root, "paper/notes.md", "Q1: What limits the regenerative torque of the clutch?\n")
    old, new = 1_760_000_000, 1_760_100_000
    for track, n, stamp in (("track2", 2, (old, old + 10)), ("track5", 5, (new, old))):
        script_time, output_time = stamp
        write(root, f"{track}/run_sim.m",
              f"T = table([1;{n}], 'VariableNames', {{'kWh'}});\n"
              "writetable(T, 'energy.csv');\n", script_time)  # fmt: skip
        write(root, f"{track}/energy.csv", f"kWh\n1\n{n}\n", output_time)


@pytest.fixture
def adopted(tmp_path: Path) -> dict[str, Any]:
    from prometheos.app.service import Service

    root = tmp_path / "study"
    forking_folder(root)
    svc = Service(home=tmp_path / "home", actor="test")
    pid = svc.init_project(
        title="Outline", objective="Adopt a forking study.", roots={"study": str(root)}
    )["project_id"]
    gen = svc.adoption_scan(pid, AGENT, root_alias="study", mode="wait")["scan_gen"]
    return {"svc": svc, "pid": pid, "gen": gen, "root": root}


def items(outline: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [i for i in outline["items"] if i["kind"] == kind]


def test_the_rules_propose_an_outline_whose_items_cite_file_and_line(
    adopted: dict[str, Any],
) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    outline = svc.adoption_outline(pid, AGENT, gen)
    assert {t["title"] for t in items(outline, "track")} == {"track2", "track5"}
    assert all(c["line"] is None and c["path"] in ("track2", "track5")
               for t in items(outline, "track") for c in t["cites"])  # fmt: skip
    (paper,) = items(outline, "deliverable")
    assert paper["title"] == "Kinematic limits of a flywheel KERS"
    assert paper["cites"][0]["path"] == "paper/main.tex" and paper["cites"][0]["line"] == 2
    questions = {q["title"]: q for q in items(outline, "question")}
    assert set(questions) == {
        "Does flywheel storage cut the energy use of a city bus?",
        "Which damper keeps the ring stable at full speed?",
        "What limits the regenerative torque of the clutch?",
    }
    for q in questions.values():
        (cite,) = q["cites"]
        lines = (adopted["root"] / cite["path"]).read_text("utf-8").splitlines()
        assert cite["quote"] == lines[cite["line"] - 1].strip()  # the exact words, on that line
        assert cite["sha256"] is not None
    # A question beside the paper is placed under it; the others under the objective.
    assert (
        questions["What limits the regenerative torque of the clutch?"]["addresses"]
        == (paper["item_id"])
    )
    assert questions["Which damper keeps the ring stable at full speed?"]["addresses"] == (
        "objective"
    )
    results = {r["path"]: r for r in items(outline, "result")}
    assert set(results) == {"track2/energy.csv", "track5/energy.csv"}
    assert results["track5/energy.csv"]["producer"] == "track5/run_sim.m"
    assert results["track5/energy.csv"]["cites"][0]["line"] == 2
    (stale,) = items(outline, "stale_candidate")
    assert stale["path"] == "track5/energy.csv"
    # The same scan gives the same outline.
    assert svc.adoption_outline(pid, AGENT, gen)["sha256"] == outline["sha256"]


def test_the_owner_confirms_once_and_the_map_has_the_outline(adopted: dict[str, Any]) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    outline = svc.adoption_outline(pid, AGENT, gen)
    questions = {q["title"]: q for q in items(outline, "question")}
    damper = questions["Which damper keeps the ring stable at full speed?"]
    bus = questions["Does flywheel storage cut the energy use of a city bus?"]
    (paper,) = items(outline, "deliverable")
    track5 = next(t for t in items(outline, "track") if t["title"] == "track5")
    edits = {
        "drop": [damper["item_id"]],
        "titles": {track5["item_id"]: "Track 5: urban cycle"},
        "addresses": {bus["item_id"]: paper["item_id"]},
        "draws_on": {bus["item_id"]: [track5["item_id"]]},
    }
    seen = svc.adoption_outline_preview(pid, AGENT, gen, outline["sha256"], edits)
    assert seen["kept"]["question"] == 2 and seen["dropped"] == 1
    assert seen["records_created"] == 5  # two tracks, one paper, two questions
    # An agent can only ask.
    with pytest.raises(PrometheosError) as err:
        svc.adoption_outline_confirm(pid, gen, outline["sha256"], edits, authority=None)
    assert err.value.code == "policy_denied"
    before = sorted(p.name for p in adopted["root"].rglob("*"))
    done = svc.adoption_outline_confirm(
        pid, gen, outline["sha256"], edits, "Outline checked.", authority=OWNER_ACT
    )
    assert done["records_created"] == 5 and done["relations_added"] == 3
    assert sorted(p.name for p in adopted["root"].rglob("*")) == before  # nothing written there
    view = svc.research_map(pid, OWNER)
    (deliverable,) = view["deliverables"]
    assert deliverable["title"] == "Kinematic limits of a flywheel KERS"
    under = {q["title"] for q in deliverable["questions"]}
    assert under == {
        "Does flywheel storage cut the energy use of a city bus?",
        "What limits the regenerative torque of the clutch?",
    }
    assert "Track 5: urban cycle" in {t["title"] for t in view["tracks"]}
    confirmed = svc.adoption_outline(pid, AGENT, gen)["confirmed"]
    assert confirmed["outline_id"] == done["outline_id"]
    assert {i["kind"] for i in confirmed["outline"]["items"]} >= {"result", "stale_candidate"}
    # Confirming the same outline again reuses the records and adds no link twice.
    again = svc.adoption_outline_confirm(pid, gen, outline["sha256"], edits, authority=OWNER_ACT)
    assert (again["records_created"], again["relations_added"]) == (0, 0)


def test_a_changed_outline_must_be_reviewed_again(adopted: dict[str, Any]) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    with pytest.raises(PrometheosError) as err:
        svc.adoption_outline_confirm(pid, gen, "0" * 64, {}, authority=OWNER_ACT)
    assert err.value.code == "revision_conflict"


@pytest.mark.parametrize(
    "bad",
    [
        {"drop": ["oi_" + "0" * 16]},  # not in this outline
        "a question under a track",
        "a title on a result",
    ],
)
def test_edits_that_do_not_fit_are_refused(adopted: dict[str, Any], bad: Any) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    outline = svc.adoption_outline(pid, AGENT, gen)
    question = items(outline, "question")[0]["item_id"]
    track = items(outline, "track")[0]["item_id"]
    result = items(outline, "result")[0]["item_id"]
    if bad == "a question under a track":
        bad = {"addresses": {question: track}}
    elif bad == "a title on a result":
        bad = {"titles": {result: "Energy"}}
    with pytest.raises(PrometheosError) as err:
        svc.adoption_outline_preview(pid, AGENT, gen, outline["sha256"], bad)
    assert err.value.code == "validation_error"


def test_an_agent_reads_the_outline_and_asks_for_its_confirmation_through_mcp(
    adopted: dict[str, Any],
) -> None:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]

    async def main() -> tuple[Any, Any]:
        async with Client(build_server(svc, pid)) as client:
            read = await client.call_tool(
                "adoption_read", {"request": {"op": "outline", "scan_gen": gen}}
            )
            sha = read.structured_content["result"]["sha256"]
            asked = await client.call_tool(
                "adoption_manage",
                {"request": {"op": "outline_confirm", "scan_gen": gen, "proposal_sha256": sha}},
            )
            return read, asked

    read, asked = anyio.run(main)
    assert (
        read.structured_content["result"]["sha256"]
        == svc.adoption_outline(pid, AGENT, gen)["sha256"]
    )
    assert asked.is_error and "approval_required" in str(asked.content)
    assert svc.research_map(pid, OWNER)["deliverables"] == []  # nothing recorded yet
    (row,) = [
        r for r in svc.approval_list(pid, OWNER)["approvals"]
        if r["operation"] == "adoption_outline_confirm"
    ]  # fmt: skip
    approval = row["approval_request_id"]
    shown = svc.approval_owner_view(pid, approval)
    svc.approval_decide(pid, approval, grant=True, plan_sha256=shown["plan_sha256"],
                        owner=OWNER_ACT)  # fmt: skip
    resumed = svc.approval_resume(pid, AGENT, approval)
    assert (
        resumed["result"]["records_created"] == 6
    )  # nothing dropped: 2 tracks, 1 paper, 3 questions
    (deliverable,) = svc.research_map(pid, OWNER)["deliverables"]
    assert deliverable["title"] == "Kinematic limits of a flywheel KERS"
    # The grant was spent: resuming again does not record twice.
    again = svc.approval_resume(pid, AGENT, approval)
    assert again["result"] == resumed["result"]
    assert len(svc.research_map(pid, OWNER)["deliverables"]) == 1
