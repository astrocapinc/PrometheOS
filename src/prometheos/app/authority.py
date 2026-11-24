"""Owner authority for operations that always need the owner (A02, CP10.2; decision 0016).

Some protected operations act on the PrometheOS home or the computer rather than on one
project's records (installing the TeX engine or a speech model, removing a model, trusting
a skill package, starting a recording, migrating a store). They need an explicit owner act,
not a capability secret a client could hold:

- ``OwnerAct`` is built only by owner channels: an interactive CLI confirmation with a
  fresh code, the paired hub owner session, or ``approval resume`` after the server-held
  grant of an approval request was spent in a committed transaction;
- the MCP adapter and agent-scoped HTTP routes never build one; an agent reaches these
  operations only as approval requests (``approval_required``).

As everywhere in PrometheOS this guards against untrusted model instructions, not against
malicious software running as the same OS user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prometheos.domain.models import PrometheosError

Channel = Literal["cli-interactive", "hub-owner", "approval-grant"]


@dataclass(frozen=True)
class OwnerAct:
    """Proof, inside the process, that the owner decided this action in an owner channel."""

    channel: Channel
    actor: str


def require_owner(authority: object, what: str) -> OwnerAct:
    """``authority`` must be an :class:`OwnerAct`; anything else is ``policy_denied``."""
    if isinstance(authority, OwnerAct):
        return authority
    raise PrometheosError(
        "policy_denied",
        f"{what} is an owner decision: confirm it in an interactive terminal or the paired "
        "hub, or request it (approval_required) and resume after the owner grants it",
        {},
    )
