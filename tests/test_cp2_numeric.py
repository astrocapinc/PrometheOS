"""C02-NUMERIC: every structured quantity reproduces retained bytes exactly (S06)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env, create_entry

from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    (e.research / "values.txt").write_text(
        "Run summary\nEnergy recovered: 2.40 kJ in event 3.\nSmall shift: 0.5 mm\nratio 7\n",
        encoding="utf-8",
    )
    (e.research / "result.json").write_text(
        json.dumps({"energy": {"value": "2.40 kJ"}, "raw": 2.4, "count": "7"}), encoding="utf-8"
    )
    return e


def binding(
    item: dict[str, Any], line: int, start: int, end: int, decimal: str, unit: str
) -> dict[str, Any]:
    return {
        "decimal": decimal,
        "unit": unit,
        "source_ref": {
            "source_id": item["source_id"],
            "version_id": item["version_id"],
            "locator": {"type": "text", "start_line": line, "end_line": line},
            "quote_sha256": None,
        },
        "source_pointer": f"text:{line}:{start}:{end}",
    }


def evidence_of(q: dict[str, Any]) -> list[dict[str, Any]]:
    return [q["source_ref"]]


def test_exact_text_bindings_accepted_and_alterations_rejected(env: Env) -> None:
    item = env.import_files(["values.txt"])["plan"]["items"][0]
    line = "Energy recovered: 2.40 kJ in event 3."
    s = line.index("2.40 kJ")
    good = binding(item, 2, s, s + 7, "2.40", "kJ")
    env.apply([create_entry(quantities=[good], evidence=evidence_of(good))])
    small_line = "Small shift: 0.5 mm"
    t = small_line.index("0.5 mm")
    env.apply([create_entry(quantities=[binding(item, 3, t, t + 6, "0.5", "mm")])])
    unitless = binding(item, 4, 6, 7, "7", "")
    env.apply([create_entry(quantities=[unitless])])
    bad_cases = [
        dict(good, decimal="2.41"),  # altered value
        dict(good, decimal="2.4"),  # reformatted without a declared transformation
        dict(good, decimal="2.400"),
        dict(good, unit="J"),  # wrong unit
        binding(item, 3, t, t + 6, "0.6", "mm"),  # small numbers are checked too
        binding(item, 3, t, t + 6, "0.50", "mm"),
        dict(good, source_pointer=f"text:2:{s + 1}:{s + 8}"),  # shifted span
        dict(good, source_pointer="text:3:0:7"),  # outside the locator line
        dict(good, source_pointer="line 2"),  # malformed pointer
    ]
    for case in bad_cases:
        with pytest.raises(PrometheosError) as err:
            env.apply([create_entry(quantities=[case])])
        assert err.value.code == "validation_error", case


def test_stale_locator_after_file_changes(env: Env) -> None:
    first = env.import_files(["values.txt"])["plan"]["items"][0]
    (env.research / "values.txt").write_text(
        "Run summary (revised)\n\nEnergy recovered: 2.40 kJ in event 3.\n", encoding="utf-8"
    )
    second = env.import_files(["values.txt"])["plan"]["items"][0]
    s = "Energy recovered: 2.40 kJ in event 3.".index("2.40 kJ")
    # The old line number on the new version addresses an empty line: rejected.
    stale = binding(second, 2, s, s + 7, "2.40", "kJ")
    with pytest.raises(PrometheosError):
        env.apply([create_entry(quantities=[stale])])
    # The historical binding against the first version stays valid.
    env.apply([create_entry(quantities=[binding(first, 2, s, s + 7, "2.40", "kJ")])])
    env.apply([create_entry(quantities=[binding(second, 3, s, s + 7, "2.40", "kJ")])])


def test_json_bindings_need_string_values(env: Env) -> None:
    item = env.import_files(["result.json"])["plan"]["items"][0]
    ref = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "json", "pointer": "/energy/value"},
        "quote_sha256": None,
    }
    good = {
        "decimal": "2.40",
        "unit": "kJ",
        "source_ref": ref,
        "source_pointer": "json:/energy/value",
    }
    env.apply([create_entry(quantities=[good])])
    number = dict(
        good,
        decimal="2.4",
        unit="",
        source_pointer="json:/raw",
        source_ref=dict(ref, locator={"type": "json", "pointer": "/raw"}),
    )
    with pytest.raises(PrometheosError):
        env.apply([create_entry(quantities=[number])])


def test_reference_only_sources_cannot_back_quantities(env: Env) -> None:
    item = env.import_files(["values.txt"], retention="reference")["plan"]["items"][0]
    s = "Energy recovered: 2.40 kJ in event 3.".index("2.40 kJ")
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(quantities=[binding(item, 2, s, s + 7, "2.40", "kJ")])])
    assert err.value.code == "source_unavailable"


def test_generated_text_numbers_must_be_bound(env: Env) -> None:
    item = env.import_files(["values.txt"])["plan"]["items"][0]
    s = "Energy recovered: 2.40 kJ in event 3.".index("2.40 kJ")
    q = binding(item, 2, s, s + 7, "2.40", "kJ")
    env.apply(
        [create_entry(origin="generated", text="Recovered 2.40 kJ on 2026-09-28.", quantities=[q])]
    )
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [
                create_entry(
                    origin="generated", text="Recovered 2.40 kJ, about 12% more.", quantities=[q]
                )
            ]
        )
    assert err.value.code == "validation_error" and "12" in str(err.value.details)
    # Typed researcher prose is not machine-checked for numbers (documented limitation).
    env.apply([create_entry(origin="typed", text="Roughly 12% better in this event.")])


def test_spans_must_be_whole_tokens(env: Env) -> None:
    (env.research / "edges.txt").write_text(
        "drift -0.5 mm here\nvalue 2.40 exact\ntotal 12.40 kJ\nrate 2.40 kJ/s\nunit 3 kJ\n",
        encoding="utf-8",
    )
    item = env.import_files(["edges.txt"])["plan"]["items"][0]

    def bad(line: int, text: str, needle: str, decimal: str, unit: str) -> None:
        start = text.index(needle)
        q = binding(item, line, start, start + len(needle), decimal, unit)
        with pytest.raises(PrometheosError) as err:
            env.apply([create_entry(quantities=[q])])
        assert err.value.code == "validation_error", (needle, decimal, unit)

    bad(1, "drift -0.5 mm here", "0.5 mm", "0.5", "mm")  # inside -0.5
    bad(2, "value 2.40 exact", "2.4", "2.4", "")  # prefix of 2.40
    bad(3, "total 12.40 kJ", "2.40 kJ", "2.40", "kJ")  # inside 12.40
    bad(4, "rate 2.40 kJ/s", "2.40 kJ", "2.40", "kJ")  # unit continues
    bad(5, "unit 3 kJ", "3 k", "3", "k")  # k inside kJ
    start = "rate 2.40 kJ/s".index("2.40 kJ/s")
    env.apply([create_entry(quantities=[binding(item, 4, start, start + 9, "2.40", "kJ/s")])])


@pytest.mark.parametrize("text", ["Recovered 2.41kJ.", "About 3x better.", "Recovered 2.40 MJ."])
def test_generated_numbers_must_use_bound_written_forms(env: Env, text: str) -> None:
    item = env.import_files(["values.txt"])["plan"]["items"][0]
    s = "Energy recovered: 2.40 kJ in event 3.".index("2.40 kJ")
    q = binding(item, 2, s, s + 7, "2.40", "kJ")
    with pytest.raises(PrometheosError):
        env.apply([create_entry(origin="generated", text=text, quantities=[q])])
    env.apply(
        [create_entry(origin="generated", text="Recovered 2.40 kJ (CP2, S06).", quantities=[q])]
    )
