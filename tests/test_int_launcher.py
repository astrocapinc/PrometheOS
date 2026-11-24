"""The integration launcher targets an explicit worktree (decision 0051).

The session-10 launcher put relative run folders into the main checkout, appended a long
``--basetemp`` and selected a worktree's code through PYTHONPATH that no record showed (review
s10f-a F7). These tests pin the replacement's behaviour with a fake target, no real suite.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import env_report  # noqa: E402
import int_launcher  # noqa: E402
import run_checkpoint  # noqa: E402

CONFIG = {
    "uv": "C:/tools/uv/uv.exe",
    "node_dir": "C:/tools/node",
    "git_cmd_dir": "C:/tools/git/cmd",
    "uv_cache_dir": "C:/cache/uv",
    "playwright_browsers_path": "C:/cache/ms-playwright",
    "tectonic_dir": "C:/staged/tectonic",
    "models_dir": "C:/staged/models",
}

STUB_CHECKPOINT = """
import json, pathlib, sys
cp, run = sys.argv[1], sys.argv[2]
log = pathlib.Path("scratch/calls.jsonl"); log.parent.mkdir(exist_ok=True)
with log.open("a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
folder = pathlib.Path("development/receipts") / cp / run
folder.mkdir(parents=True)
(folder / "marker.txt").write_text(cp)
if cp == "CP2":
    pathlib.Path("src/prometheos/changed.py").write_text("x = 1\\n")
print(f"summary line for {cp}")
sys.exit(3 if cp == "CP1" else 0)
"""


def fake_target(tmp_path: Path, name: str = "target") -> Path:
    target = tmp_path / name
    (target / "src" / "prometheos").mkdir(parents=True)
    (target / "src" / "prometheos" / "__init__.py").write_text("", encoding="utf-8")
    (target / "tools").mkdir()
    for tool in ("run_check.py", "tree_manifest.py"):
        shutil.copy(ROOT / "tools" / tool, target / "tools" / tool)
    (target / "tools" / "run_checkpoint.py").write_text(STUB_CHECKPOINT, encoding="utf-8")
    venv_python = target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    return target


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake target whose 'venv' is this interpreter, with preflight stubbed (it needs uv)."""
    target = fake_target(tmp_path)
    config = tmp_path / "launcher.json"
    config.write_text(json.dumps(CONFIG | {"temp_root": str(tmp_path / "t")}), encoding="utf-8")
    real_paths = int_launcher.target_paths

    def paths(target_root: Path) -> dict[str, Path]:
        found = real_paths(target_root)
        return found | {"python": Path(sys.executable)}

    monkeypatch.setattr(int_launcher, "target_paths", paths)
    monkeypatch.setattr(int_launcher, "preflight", lambda *a: {"stubbed": True})
    # pytest's own temporary folders are longer than the host limit; one test restores it.
    monkeypatch.setattr(int_launcher, "BASETEMP_PREFIX_LIMIT", 10_000)
    return {"target": target, "config": config}


def test_a_relative_run_folder_belongs_to_the_target_not_the_launchers_checkout(
    tmp_path: Path,
) -> None:
    target = fake_target(tmp_path)
    assert int_launcher.resolve_run_dir(target, "scratch/x") == (target / "scratch/x").resolve()
    absolute = (tmp_path / "elsewhere").resolve()
    assert int_launcher.resolve_run_dir(target, absolute) == absolute


def test_the_environment_selects_the_targets_venv_and_sets_no_pythonpath_or_basetemp(
    tmp_path: Path,
) -> None:
    target = fake_target(tmp_path)
    paths = int_launcher.target_paths(target)
    env = int_launcher.environment(paths, CONFIG, tmp_path / "plabc")
    assert "PYTHONPATH" not in env
    assert "PYTEST_ADDOPTS" not in env
    assert env["UV_PROJECT_ENVIRONMENT"] == str(target.resolve() / ".venv")
    assert env["PATH"].split(os.pathsep)[0] == str(paths["python"].parent)
    assert env["TEMP"] == env["TMP"] == str(tmp_path / "plabc" / "t")
    assert env["PROMETHEOS_REQUIRE_TECTONIC"] == env["PROMETHEOS_REQUIRE_REAL_MODEL"] == "1"
    command = int_launcher.check_command(paths, ["pytest", "-q", "tests/x.py"])
    assert command == [str(paths["python"]), "-m", "pytest", "-q", "tests/x.py"]


def test_the_synthetic_folder_is_short_and_only_old_launcher_folders_are_removed(
    tmp_path: Path,
) -> None:
    temp = tmp_path / "temp"
    old = temp / "plold"
    old.mkdir(parents=True)
    (old / int_launcher.ENV_MARKER).write_text("x", encoding="utf-8")
    stale = time.time() - 2 * int_launcher.ENV_KEEP_SECONDS
    os.utime(old / int_launcher.ENV_MARKER, (stale, stale))
    foreign = temp / "plfoo"  # same pattern, no marker: not the launcher's
    foreign.mkdir()
    os.utime(foreign, (stale, stale))
    made = int_launcher.synthetic_root(temp)
    assert made.parent == temp and len(made.name) == 5 and made.name.startswith("pl")
    assert (made / int_launcher.ENV_MARKER).is_file()
    assert not old.exists()
    assert foreign.is_dir()


def test_the_configuration_must_name_every_host_path_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    with pytest.raises(int_launcher.LauncherError, match="missing"):
        int_launcher.load_config(path)
    path.write_text(json.dumps({k: v for k, v in CONFIG.items() if k != "uv"}), encoding="utf-8")
    with pytest.raises(int_launcher.LauncherError, match="lacks uv"):
        int_launcher.load_config(path)
    path.write_text(json.dumps(CONFIG | {"pythonpath": "x"}), encoding="utf-8")
    with pytest.raises(int_launcher.LauncherError, match="unknown keys"):
        int_launcher.load_config(path)


def test_a_target_without_its_own_venv_or_source_is_refused(tmp_path: Path) -> None:
    target = fake_target(tmp_path)
    shutil.rmtree(target / ".venv")
    with pytest.raises(int_launcher.LauncherError, match=r"no .venv of its own"):
        int_launcher.target_paths(target)
    with pytest.raises(int_launcher.LauncherError, match="not a PrometheOS checkout"):
        int_launcher.target_paths(tmp_path)


def test_preflight_refuses_a_venv_that_imports_another_checkouts_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fake_target(tmp_path)
    other = fake_target(tmp_path, "main-checkout")
    paths = int_launcher.target_paths(target)

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(int_launcher.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(int_launcher, "imported_package", lambda *a: other / "src" / "prometheos")
    with pytest.raises(int_launcher.LauncherError, match="not from its own src"):
        int_launcher.preflight(paths, CONFIG, {}, ["audio"])
    monkeypatch.setattr(int_launcher, "imported_package", lambda *a: target / "src" / "prometheos")
    checks = int_launcher.preflight(paths, CONFIG, {}, ["audio"])
    assert checks["extras"] == ["audio"]

    class Stale(Done):
        returncode = 1
        stderr = "would install 3 packages"

    monkeypatch.setattr(int_launcher.subprocess, "run", lambda *a, **k: Stale())
    with pytest.raises(int_launcher.LauncherError, match="not exactly the locked environment"):
        int_launcher.preflight(paths, CONFIG, {}, [])


def test_the_environment_report_states_the_code_under_test_without_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(candidate / "src"), str(tmp_path)]))
    tested = env_report.code_under_test(candidate)
    # This interpreter's prometheos is outside the temporary candidate.
    assert tested["package_in_candidate"] is False
    assert tested["prometheos_package"] == "<outside the candidate>/prometheos"
    assert tested["pythonpath"] == ["src", f"<outside the candidate>/{tmp_path.name}"]
    assert str(tmp_path) not in json.dumps(tested)
    monkeypatch.delenv("PYTHONPATH")
    here = env_report.code_under_test(ROOT)
    assert here["prometheos_package"] == "src/prometheos" and here["package_in_candidate"]


GOOD = {
    "prometheos_package": "src/prometheos",
    "package_in_candidate": True,
    "environment": ".venv",
    "pythonpath": [],
}


@pytest.mark.parametrize(
    ("tested", "reason"),
    [
        (None, "does not state"),
        ("src/prometheos", "not an object"),
        (
            GOOD | {"prometheos_package": "<outside>/prometheos", "package_in_candidate": False},
            "not src",
        ),
        (GOOD | {"pythonpath": ["src"]}, "PYTHONPATH is set"),
        (GOOD | {"environment": "<outside the candidate>/.venv"}, "not the candidate's"),
    ],
)
def test_a_checkpoint_run_stops_before_its_suite_unless_it_tests_the_candidates_src(
    tmp_path: Path, tested: Any, reason: str
) -> None:
    report: dict[str, Any] = {"os": "x"}
    if tested is not None:
        report["code_under_test"] = tested
    (tmp_path / "env.stdout.txt").write_text(json.dumps(report), encoding="utf-8")
    assert reason in (run_checkpoint.code_under_test(tmp_path) or "")
    report["code_under_test"] = GOOD
    (tmp_path / "env.stdout.txt").write_text(json.dumps(report), encoding="utf-8")
    assert run_checkpoint.code_under_test(tmp_path) is None


def test_an_integration_run_drives_the_targets_checkpoints_in_order_and_records_the_target(
    launch: dict[str, Any],
) -> None:
    target: Path = launch["target"]
    code = int_launcher.main(
        [
            "integration",
            "--target",
            str(target),
            "--config",
            str(launch["config"]),
            "--suffix",
            "t1",
            "--only",
            "CP18,CP0",
            "--reuse-from",
            "development/receipts/CP9/a",
        ]
    )
    assert code == 0
    calls = [
        json.loads(line)["argv"]
        for line in (target / "scratch" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert calls == [
        ["CP18", "cp18-t1", "--reuse-from", "development/receipts/CP9/a"],
        [
            "CP0",
            "cp0-t1",
            "--reuse-from",
            "development/receipts/CP9/a",
            "development/receipts/CP18/cp18-t1",
        ],
    ]
    driver = target / "scratch" / "int" / "t1-driver"  # under the target, not this checkout
    log = (driver / "driver.log").read_text(encoding="utf-8")
    assert "TREE STABLE" in log and log.rstrip().endswith("END all exit 0")
    assert "  summary line for CP18\nexit 0 CP18" in log
    record = json.loads((driver / "launcher.json").read_text(encoding="utf-8"))
    assert record["target"]["root"] == str(target.resolve())
    assert record["environment"]["UV_PROJECT_ENVIRONMENT"] == str(target.resolve() / ".venv")
    assert record["results"] == {"CP18": 0, "CP0": 0} and record["tree_stable"]
    assert record["tree_sha256_after"] == record["target"]["tree_sha256_before"]
    for cp in ("cp18", "cp0"):
        assert json.loads((driver / f"run-{cp}.cmd.json").read_text())["exit_code"] == 0
    # A second run with the same suffix would mix records: refused.
    again = ["integration", "--target", str(target), "--config", str(launch["config"])]
    assert int_launcher.main([*again, "--suffix", "t1", "--only", "CP0"]) == 2


def test_an_integration_run_fails_on_a_failed_stage_or_a_tree_change_and_says_which(
    launch: dict[str, Any],
) -> None:
    target: Path = launch["target"]
    base = ["integration", "--target", str(target), "--config", str(launch["config"])]
    assert int_launcher.main([*base, "--suffix", "f1", "--only", "CP1,CP0"]) == 1
    log = (target / "scratch/int/f1-driver/driver.log").read_text(encoding="utf-8")
    assert "exit 3 CP1" in log and log.rstrip().endswith("END FAILED CP1")
    assert int_launcher.main([*base, "--suffix", "f2", "--only", "CP2"]) == 1
    log = (target / "scratch/int/f2-driver/driver.log").read_text(encoding="utf-8")
    assert "TREE CHANGED DURING RUN" in log


def test_a_single_check_records_the_target_beside_its_command_record(
    launch: dict[str, Any],
) -> None:
    target: Path = launch["target"]
    code = int_launcher.main(
        [
            "check",
            "--target",
            str(target),
            "--config",
            str(launch["config"]),
            "--run-dir",
            "scratch/one",
            "--name",
            "hello",
            "--",
            "python",
            "-c",
            "print('hi')",
        ]
    )
    assert code == 0
    run_dir = target / "scratch" / "one"
    assert (run_dir / "hello.stdout.txt").read_text().strip() == "hi"
    record = json.loads((run_dir / "hello.launcher.json").read_text(encoding="utf-8"))
    assert record["exit_code"] == 0 and record["target"]["root"] == str(target.resolve())
    assert "PYTHONPATH" not in record["environment"]


def test_bad_suffixes_folders_under_development_and_long_temp_roots_are_refused(
    launch: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target: Path = launch["target"]
    base = ["--target", str(target), "--config", str(launch["config"])]
    assert int_launcher.main(["integration", *base, "--suffix", "../x"]) == 2
    check = ["check", *base, "--name", "n", "--run-dir"]
    assert int_launcher.main([*check, "development/receipts/CP0/x", "--", "python", "-V"]) == 2
    assert not (target / "development").exists()
    monkeypatch.setattr(int_launcher, "BASETEMP_PREFIX_LIMIT", 70)
    deep = tmp_path / ("d" * 60)
    config = json.loads(launch["config"].read_text(encoding="utf-8")) | {"temp_root": str(deep)}
    launch["config"].write_text(json.dumps(config), encoding="utf-8")
    assert int_launcher.main([*check, "scratch/x", "--", "python", "-V"]) == 2
    assert not list(deep.glob("pl*"))  # the refused synthetic folder is not left behind


def test_the_synthetic_folder_goes_after_a_pass_and_stays_after_a_failure(
    launch: dict[str, Any], tmp_path: Path
) -> None:
    target: Path = launch["target"]
    base = ["check", "--target", str(target), "--config", str(launch["config"]), "--name", "n"]
    assert int_launcher.main([*base, "--run-dir", "scratch/a", "--", "python", "-c", "0"]) == 0
    assert not list((tmp_path / "t").glob("pl*"))
    code = int_launcher.main(
        [*base, "--run-dir", "scratch/b", "--", "python", "-c", "raise SystemExit(4)"]
    )
    assert code == 4
    (kept,) = (tmp_path / "t").glob("pl*")
    assert (kept / "t").is_dir()
    record = json.loads((target / "scratch/b/n.launcher.json").read_text(encoding="utf-8"))
    assert record["preflight"]["basetemp_prefix_max"] <= int_launcher.BASETEMP_PREFIX_LIMIT


def test_the_driver_log_keeps_each_stages_summary_and_always_ends(
    launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target: Path = launch["target"]
    real = int_launcher.tree_digest
    calls = {"n": 0}

    def digest(root: Path) -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise int_launcher.LauncherError("disk gone")
        return real(root)

    monkeypatch.setattr(int_launcher, "tree_digest", digest)
    base = ["integration", "--target", str(target), "--config", str(launch["config"])]
    assert int_launcher.main([*base, "--suffix", "e1", "--only", "CP0"]) == 1
    log = (target / "scratch/int/e1-driver/driver.log").read_text(encoding="utf-8")
    assert "tree after: unavailable (disk gone)" in log
    assert log.rstrip().endswith("END FAILED tree")


def test_the_worst_case_basetemp_counts_a_seven_digit_pid_and_a_two_digit_worker() -> None:
    temp = Path("C:/t")
    expected = len(str(temp)) + len("/ptb/") + 7 + len("/popen-gw99")
    assert int_launcher.basetemp_prefix(temp) == expected


def test_a_driver_folder_under_development_is_refused(launch: dict[str, Any]) -> None:
    target: Path = launch["target"]
    code = int_launcher.main(
        [
            "integration",
            "--target",
            str(target),
            "--config",
            str(launch["config"]),
            "--suffix",
            "d1",
            "--driver-dir",
            "development/receipts/x",
        ]
    )
    assert code == 2 and not (target / "development").exists()
