set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

# Optional BES-1 task aliases; native commands remain supported.
default:
    @just --list

setup:
    python -m pip install -e ".[dev]" build

check:
    ruff check .
    ruff format --check .
    mypy
    python -m pytest --cov --cov-fail-under=90

integration:
    python -m pytest tests/integration -q

smoke:
    blackbox --help

build:
    python -m build

# Local preflight only; the tag-triggered GitHub release workflow is authoritative.
release-check:
    just check
    just integration
    just build
    just smoke
