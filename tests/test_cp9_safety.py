"""C09-SAFETY (S22), compile half: hostile LaTeX against the real pinned engine, the
runner's limits, the read audit and the offline guarantee. The Git half is in
test_cp9_sync.py; engine install verification is in test_cp9_engine.py."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env
from cp9_support import LatexEnv

from prometheos.app import texengine

pytestmark = pytest.mark.tectonic

SECRET = "TOPSECRETCANARY4242"


@pytest.fixture
def ws(tmp_path: Path, tectonic_engine: Path) -> LatexEnv:
    return LatexEnv(Env(tmp_path))


def _set_main(ws: LatexEnv, transform) -> None:  # type: ignore[no-untyped-def]
    loaded = ws.read("main.tex")
    ws.write("main.tex", transform(loaded["text"]), loaded["sha256"])


def _store_has(ws: LatexEnv, needle: str) -> bool:
    data = needle.encode()
    return any(p.is_file() and data in p.read_bytes() for p in ws.env.store_dir.rglob("*"))


def test_shell_escape_is_refused_before_the_engine_runs(ws: LatexEnv, tmp_path: Path) -> None:
    marker = tmp_path / "pwned.txt"
    command = f"\\immediate\\write18{{echo pwned > {marker.as_posix()}}}"
    _set_main(ws, lambda t: t.replace("\\maketitle", "\\maketitle\n" + command))
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "refused" and result["outputs"] == {}
    finding = result["diagnostics"][0]
    assert finding["file"] == "main.tex" and finding["context"] == "shell_escape"
    lines = ws.path("main.tex").read_text("utf-8").split("\n")
    assert finding["line"] == next(n for n, t in enumerate(lines, 1) if "write18" in t)
    assert not marker.exists()


def test_engine_disables_shell_escape_that_hides_from_the_scan(
    ws: LatexEnv, tmp_path: Path
) -> None:
    marker = tmp_path / "pwned2.txt"
    # \csname write\endcsname is \write, so this is \write18 without the literal text.
    hidden = "\\immediate\\csname write\\endcsname18{echo pwned > " + marker.as_posix() + "}"
    _set_main(ws, lambda t: t.replace("\\maketitle", "\\maketitle\n" + hidden))
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "succeeded"
    assert not marker.exists()
    assert any("disabled" in d["message"] for d in result["diagnostics"])


def test_literal_paths_outside_the_workspace_are_refused(ws: LatexEnv, tmp_path: Path) -> None:
    secret = tmp_path / "secret.tex"
    secret.write_text(SECRET + "\n", "utf-8")
    for command in (
        f"\\input{{{secret.as_posix()}}}",
        "\\input{../../secret}",
        "\\include{~/secret}",
        "\\input ../secret ",
        "\\includegraphics{C:/Windows/System32/x.png}",
    ):
        _set_main(ws, lambda t, c=command: t.replace("\\input{sections/intro}", c))
        result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
        assert result["status"] == "refused", command
        assert result["diagnostics"][0]["context"] == "path_escape"
        assert result["diagnostics"][0]["file"] == "main.tex"
        _set_main(ws, lambda t, c=command: t.replace(c, "\\input{sections/intro}"))
    assert not _store_has(ws, SECRET)


def test_macro_built_absolute_read_discards_every_output(ws: LatexEnv, tmp_path: Path) -> None:
    secret = tmp_path / "secret.tex"
    secret.write_text(SECRET + "\n", "utf-8")
    path = secret.as_posix()
    drive, rest = path[:2], path[2:]
    built = f"\\def\\d{{{drive}}}\\def\\r{{{rest}}}\\input{{\\d\\r}}"
    _set_main(ws, lambda t: t.replace("\\input{sections/intro}", built))
    status_before = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "refused" and result["outputs"] == {}
    assert result["diagnostics"][0]["context"] == "read_outside"
    assert "secret.tex" in result["diagnostics"][0]["message"]
    assert not _store_has(ws, SECRET)
    after = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    # The inputs were snapshotted (sequence moved), but no compile output was registered.
    assert after["last_compile"] == status_before["last_compile"] is None
    assert after["pdf"] is None and after["history"] == []


def test_macro_built_parent_read_discards_every_output(ws: LatexEnv) -> None:
    planted = ws.env.store_dir / "staging" / "planted.tex"
    planted.write_text(SECRET + "\n", "utf-8")
    built = "\\def\\up{..}\\input{\\up/\\up/planted}"
    _set_main(ws, lambda t: t.replace("\\input{sections/intro}", built))
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    planted.unlink()
    assert result["status"] == "refused" and result["outputs"] == {}
    assert "planted.tex" in result["diagnostics"][0]["message"]
    assert not _store_has(ws, SECRET)


def test_endless_loop_stops_at_the_time_limit_and_keeps_the_prior_pdf(ws: LatexEnv) -> None:
    good = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert good["status"] == "succeeded"
    _set_main(ws, lambda t: t.replace("\\maketitle", "\\maketitle\n\\def\\x{\\x}\\x"))
    limits = texengine.RunLimits(timeout_seconds=5)
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id, limits=limits)
    assert result["status"] == "timeout"
    assert result["seconds"] < 30
    assert set(result["outputs"]) == {"log"}
    assert result["diagnostics"][0]["message"].startswith("compile stopped after 5 s")
    status = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    assert status["last_compile"]["status"] == "timeout"
    assert status["pdf"]["version_id"] == good["outputs"]["pdf"]


def test_runaway_output_is_bounded(ws: LatexEnv) -> None:
    bomb = "\\loop x\\newpage\\iftrue\\repeat"
    _set_main(ws, lambda t: t.replace("\\maketitle", "\\maketitle\n" + bomb))
    limits = texengine.RunLimits(timeout_seconds=8, max_memory_bytes=768 * 1024 * 1024)
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id, limits=limits)
    assert result["status"] in ("timeout", "memory_limit", "output_limit", "failed")
    assert "pdf" not in result["outputs"]
    assert result["seconds"] < 40


class Recorder:
    """A local TCP listener that counts connection attempts (a would-be proxy)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.count = 0
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _serve(self) -> None:
        while not self.stop:
            try:
                client, _ = self.sock.accept()
            except OSError:
                continue
            self.count += 1
            client.close()

    def close(self) -> None:
        self.stop = True
        self.thread.join(timeout=2)
        self.sock.close()


def _age_bundle_checks(engine_dir: Path, seconds: int) -> list[Path]:
    locks = sorted((texengine.cache_dir(engine_dir) / "bundles" / "hashes").glob("*.lock"))
    for lock in locks:
        lock.write_text(str(int(time.time()) - seconds), encoding="ascii")
    return locks


def test_offline_compile_from_a_populated_cache_makes_no_request(
    ws: LatexEnv, tectonic_engine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tectonic 0.17.0 re-checks a bundle whose last check is over a week old, even with
    # --only-cached (found when the staged cache passed that age): start from such a cache.
    assert _age_bundle_checks(tectonic_engine, 8 * 86400), "the populated cache has no bundle lock"
    recorder = Recorder()
    try:
        # Every proxy variable of an offline run points at the recorder.
        monkeypatch.setattr(texengine, "DEAD_PROXY", recorder.url)
        result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
        assert result["status"] == "succeeded" and result["fetch"] is False
        # A package that is not in the cache fails offline instead of being fetched.
        _set_main(
            ws,
            lambda t: t.replace(
                "\\usepackage{graphicx}", "\\usepackage{graphicx}\n\\usepackage{fontawesome5}"
            ),
        )
        missing = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
        assert missing["status"] == "failed"
    finally:
        recorder.close()
    assert recorder.count == 0


def test_bundle_check_renewal_touches_only_integer_locks(tmp_path: Path) -> None:
    hashes = tmp_path / "bundles" / "hashes"
    hashes.mkdir(parents=True)
    (hashes / "a.lock").write_text("1000", encoding="ascii")
    (hashes / "b.lock").write_text("not a time", encoding="ascii")
    (hashes / "a.tar").write_text("digest", encoding="ascii")
    before = int(time.time())
    assert texengine.mark_bundle_checked(tmp_path) == 1
    assert int((hashes / "a.lock").read_text(encoding="ascii")) >= before
    assert (hashes / "b.lock").read_text(encoding="ascii") == "not a time"
    assert (hashes / "a.tar").read_text(encoding="ascii") == "digest"
    assert sorted(p.name for p in hashes.iterdir()) == ["a.lock", "a.tar", "b.lock"]
    assert texengine.mark_bundle_checked(tmp_path / "missing") == 0


def test_compile_log_holds_no_local_path(ws: LatexEnv) -> None:
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    log = ws.svc.version_text(ws.pid, OWNER, result["outputs"]["log"])["text"]
    assert str(ws.env.store_dir) not in log and "staging" not in log
    assert str(ws.env.store_dir).replace("\\", "/") not in log
    sync_bytes = ws.svc.version_text(ws.pid, OWNER, result["outputs"]["synctex"])
    assert sync_bytes["text"] is None or str(ws.env.store_dir) not in sync_bytes["text"]


# ----- canary file outside the workspace (primary's answer to Q2; decision 0012 item 11a) -----

CANARY = "CANARYTEXT7Q2Z5"


def _leaks(ws: LatexEnv, result: dict) -> list[str]:  # type: ignore[type-arg]
    """Places the canary must not reach: the store (records, payloads, search index,
    media), the compile response, the compile status, agent context and search."""
    hits = []
    if _store_has(ws, CANARY):
        hits.append("store")
    if CANARY in json.dumps(result):
        hits.append("compile response")
    if CANARY in json.dumps(ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)):
        hits.append("compile status")
    for purpose in ("orient", "notebook_draft"):
        if CANARY in json.dumps(ws.svc.context(ws.pid, AGENT, purpose=purpose)):
            hits.append(f"context {purpose}")
    if ws.svc.search(ws.pid, OWNER, CANARY)["hits"]:  # the reply echoes the query itself
        hits.append("search")
    return hits


def _plant(ws: LatexEnv) -> Path:
    """The canary sits in the store's staging folder: two levels above the engine's
    working directory, reachable with ../../ (no absolute-path warning, no log line)."""
    planted = ws.env.store_dir / "staging" / "planted.tex"
    planted.write_text(CANARY + "\n", "utf-8")
    return planted


READ_CANARY = (
    "\\newread\\canary\\def\\up{..}\\openin\\canary=\\up/\\up/planted.tex "
    "\\read\\canary to\\leak\\closein\\canary"
)


def _with(extra: str):  # type: ignore[no-untyped-def]
    return lambda t: t.replace("\\maketitle", "\\maketitle\n" + READ_CANARY + extra)


def test_failed_compile_with_a_macro_built_outside_read_stores_no_canary(ws: LatexEnv) -> None:
    good = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert good["status"] == "succeeded"
    planted = _plant(ws)
    # The canary becomes an error message attributed to a workspace line.
    _set_main(ws, _with("\\errmessage{\\leak}"))
    try:
        result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    finally:
        planted.unlink()
    assert result["status"] == "failed"
    assert set(result["outputs"]) == {"log"}
    lines = ws.path("main.tex").read_text("utf-8").split("\n")
    line = next(n for n, t in enumerate(lines, 1) if "errmessage" in t)
    error = next(d for d in result["diagnostics"] if d["severity"] == "error")
    assert error["file"] == "main.tex" and error["line"] == line
    assert error["message"].startswith("details withheld")
    log = ws.svc.version_text(ws.pid, OWNER, result["outputs"]["log"])["text"]
    assert "are not kept" in log
    assert _leaks(ws, result) == []
    # Control: the same document without the guard would have carried the canary.
    status = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    assert status["pdf"]["version_id"] == good["outputs"]["pdf"]


def test_failed_compile_via_mcp_returns_no_canary(ws: LatexEnv) -> None:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    planted = _plant(ws)
    _set_main(ws, _with("\\errmessage{\\leak}"))

    async def main() -> str:
        async with Client(build_server(ws.svc, ws.pid)) as client:
            # An operation run (decision 0017): wait for its result in the same call.
            reply = await client.call_tool(
                "paper_compile", {"paper_id": ws.paper_id, "wait_seconds": 20}
            )
            status = await client.call_tool("paper_compile_status", {"paper_id": ws.paper_id})
            return str(reply.content[0].text) + str(status.content[0].text)  # type: ignore[union-attr]

    try:
        text = anyio.run(main)
    finally:
        planted.unlink()
    assert '"status":"failed"' in text.replace(" ", "")
    assert CANARY not in text
    assert not _store_has(ws, CANARY)


@pytest.mark.parametrize("use", ["\\typeout{\\leak}", "\\leak"])
def test_successful_compile_with_an_outside_read_stores_nothing(ws: LatexEnv, use: str) -> None:
    planted = _plant(ws)
    _set_main(ws, _with(use))
    try:
        result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    finally:
        planted.unlink()
    assert result["status"] == "refused" and result["outputs"] == {}
    assert result["diagnostics"][0]["context"] == "read_outside"
    assert _leaks(ws, result) == []


def test_timed_out_compile_after_an_outside_read_stores_no_canary(ws: LatexEnv) -> None:
    planted = _plant(ws)
    _set_main(ws, _with("\\typeout{\\leak}\\def\\x{\\x}\\x"))
    try:
        result = ws.svc.paper_compile(
            ws.pid, OWNER, ws.paper_id, limits=texengine.RunLimits(timeout_seconds=5)
        )
    finally:
        planted.unlink()
    assert result["status"] == "timeout"
    assert _leaks(ws, result) == []


def test_proven_successful_compile_keeps_its_full_log(ws: LatexEnv) -> None:
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    log = ws.svc.version_text(ws.pid, OWNER, result["outputs"]["log"])["text"]
    assert "Output written on main.xdv" in log and "are not kept" not in log


def test_a_success_without_its_dependency_list_is_not_trusted(
    ws: LatexEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: should the engine ever finish without writing its dependency list,
    nothing it produced is kept (its reads cannot be proven)."""
    original = texengine.run

    def run_and_lose_the_list(argv, **kwargs):  # type: ignore[no-untyped-def]
        result = original(argv, **kwargs)
        Path(argv[argv.index("--makefile-rules") + 1]).unlink(missing_ok=True)
        return result

    monkeypatch.setattr(texengine, "run", run_and_lose_the_list)
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "refused" and result["outputs"] == {}
    assert result["diagnostics"][0]["context"] == "reads_unverified"
    assert ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)["pdf"] is None
