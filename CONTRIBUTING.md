# Contributing to PrometheOS

Thank you for considering a contribution. PrometheOS is a small project in pre-release, so please open an issue to discuss a change of any size before you spend time on it.

## Ground rules

- **Use synthetic data.** Never attach real research, manuscripts, recordings, credentials or personal data to an issue, a test or a pull request. The `fixtures/` folder shows the kind of synthetic material tests use.
- **Keep the product boundary.** Coding agents execute; PrometheOS supplies context, records, evidence and approvals. Proposals for an agent supervisor, a cloud service or telemetry are out of scope.
- **Test behaviour, not implementation.** Each fixed defect gets a focused regression test. Default tests make no model calls and no network requests.

## Development setup

See [docs/development.md](docs/development.md). In short:

```bash
uv sync --locked
uv run pytest -n auto -m "not serial and not slow and not real_model and not audio_extra"
uv run pytest -p no:xdist -m "serial and not slow and not real_model and not audio_extra"
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python tools/check_contracts.py
```

A new dependency needs an entry in the dependency register (`uv run python tools/dependency_register.py --write` in the private repository) and a licence compatible with Apache-2.0. Say in the pull request why it is needed.

## Developer Certificate of Origin

Contributions are accepted under the [Developer Certificate of Origin 1.1](https://developercertificate.org/). By signing off a commit you certify that you wrote the change or otherwise have the right to submit it under the project's licence. Sign off each commit with your real name:

```bash
git commit -s -m "Describe the change"
```

This adds a line such as `Signed-off-by: Your Name <you@example.org>`. Only you can sign off your own work: maintainers and tools never add a sign-off on a contributor's behalf. The DCO is not a copyright assignment, and contributions stay under the Apache License 2.0 (section 5 of the [licence](LICENSE)).

## Pull requests

- Keep each pull request to one change, with a description of what it does and how you tested it.
- Run the commands above; CI runs them on Windows and Ubuntu.
- Changes to files sealed by the specification (`package-manifest.json`) need a specification amendment; ask in the issue first.
- Changes to the hub's sources need rebuilt assets (`uv run python tools/build_hub.py`).

## Reporting bugs and security issues

Use the issue templates for bugs and feature requests. Report vulnerabilities privately, as [SECURITY.md](SECURITY.md) describes, never in a public issue.
