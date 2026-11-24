"""The intelligence layer (A07, CP22; D16: models propose, code verifies).

One module, used only through service operations. It is not an agent: no tools, no loop, no
file access beyond the bundle it is given, and no effect on the record other than storing a
validated proposal.

- ``wire``: the schema sent to a provider, derived from each task's strict output model.
- ``tasks``: the typed task registry (input bundle, strict output with an abstain branch,
  template digest, validator, agreement, declared effect).
- ``validate``: the deterministic acceptance checks of A07.3.
- ``gateway``: the OpenRouter profile over the CP6.3 transport (request defaults, per-model
  profiles, fallback inside the evaluated pairs, the served model and provider recorded).
- ``spend``: the home's spend ledger (monthly and per-run caps, reservations under a home lock).
- ``policy``: the owner's standing policy per task kind.
- ``results``: the rebuildable ``ai_results`` cache and offline replay from cassettes.
- ``evaluation``: golden sets, targets fixed in advance and trust on a lower confidence bound.
"""
