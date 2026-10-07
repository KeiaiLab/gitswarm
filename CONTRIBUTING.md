# Contributing

## Setup

```sh
uv sync --dev
```

Python >= 3.11 and `git` on `PATH`. Tests use real git; no network.

## Gate

The same commands CI runs (`.forgejo/ci/verify.sh`):

```sh
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run vulture src --min-confidence 60 --ignore-names "hive_init,ws_*,workspace_*,events_tail,mcp_serve,main"
uv run pytest tests -q --cov --cov-report=term-missing
```

- Coverage must be 100% (`fail_under = 100`). Do not add pragmas.
- The vulture ignore list covers only entry points registered by typer and
  fastmcp decorators. Do not widen it to silence dead code; delete the code.
- CI also runs `uv build` and a smoke run of the built wheel.

## Rules

- Bug fix: write the failing test first, watch it fail, then fix.
- `subprocess` only in `src/gitswarm/driver/git.py`. Layers call their direct
  neighbour below (`docs/ARCHITECTURE.md`, section 2).
- Constants and names go in `src/gitswarm/constants.py`.
- Tests use real git against `file://` remotes. Prefer that to mocking git.
- Keep changes minimal; do not touch unrelated code.

## Commits

- Subject: imperative, capitalized, at most 50 characters, no period.
- Blank line, then a body wrapped at 72 characters.
- Body explains what and why, not how.

One slice = one branch = one commit. Keep a slice small enough to review
in one sitting.

## CI

A push runs the fleet workflow `.forgejo/workflows/ci.yml`, which calls
`.forgejo/ci/verify.sh`. The checks run in a cluster pod; landing happens
after CI is green.

## Design documents

- Specs: `docs/superpowers/specs/`
- Plans: `docs/superpowers/plans/`
- Architecture: `docs/ARCHITECTURE.md`

Change the spec in the same slice when behaviour diverges from it.

## Language

Commits, issues and pull requests may be in English or Korean.
