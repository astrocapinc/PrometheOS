"""PrometheOS: a local research record and companion hub used by coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("prometheos")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"
