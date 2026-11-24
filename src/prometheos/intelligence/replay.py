"""A replay transport for walks (C22-WALK; decision 0098). Test hook only.

When ``PROMETHEOS_TEST_HOOKS=1`` and ``PROMETHEOS_TEST_REPLAY`` names a folder, the service's
provider transport answers from recorded reply bodies instead of the network: a request is
matched by the digest of the bundle it carries (the ``DATA:`` part of its user message) and, when
the recording names one, the model asked for; a request it has no recording for is ``NotSent``,
and nothing ever opens a socket. Results replayed this way are stored as recorded replies
(executor ``cassette``), never as the gateway's (review cp22-a D3). The key's
remaining limit reads as plenty. Replies recorded this way confer no trust (they are not the
gateway's own receipts of a real call)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from prometheos.domain.canonical import canonical_sha256
from prometheos.providers import transport as tp

HOOKS = "PROMETHEOS_TEST_HOOKS"
FOLDER = "PROMETHEOS_TEST_REPLAY"


def from_environment() -> ReplayTransport | None:
    if os.environ.get(HOOKS) != "1" or not os.environ.get(FOLDER):
        return None
    return ReplayTransport(Path(os.environ[FOLDER]))


class ReplayTransport:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _recordings(self) -> dict[tuple[str, str | None], dict[str, object]]:
        found: dict[tuple[str, str | None], dict[str, object]] = {}
        for path in sorted(self.folder.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            model = item.get("model")
            found[(str(item["input_sha256"]), str(model) if model else None)] = item["body"]
        return found

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tp.Reply:
        try:
            request = json.loads(body)
            user = next(m["content"] for m in request["messages"] if m["role"] == "user")
            data = json.loads(str(user).split("DATA:\n", 1)[1])
        except (ValueError, KeyError, IndexError, StopIteration) as exc:
            raise tp.NotSent(f"not a layer request: {exc}") from None
        recordings, digest = self._recordings(), canonical_sha256(data)
        recorded = recordings.get((digest, str(request.get("model")))) or recordings.get(
            (digest, None)
        )
        if recorded is None:
            raise tp.NotSent("no recording for this bundle")
        return tp.Reply(200, json.dumps(recorded).encode("utf-8"))

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tp.Reply:
        return tp.Reply(200, json.dumps({"data": {"limit": 5, "limit_remaining": 5}}).encode())
