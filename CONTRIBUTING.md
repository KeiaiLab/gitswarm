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
uv run vulture src vulture_whitelist.py --min-confidence 60
uv run pytest tests -q --cov --cov-report=term-missing
uv build
uv run --isolated --no-project --with dist/*.whl gitswarm --help >/dev/null
```

- Coverage must be 100% (`fail_under = 100`). Do not add pragmas.
- `vulture_whitelist.py` lists, one name per line with its reason, only
  entry points registered by typer and fastmcp decorators and the console
  script `main`. Do not add names to silence dead code; delete the code.
- `uv build` and the wheel smoke run are part of the gate (last two lines).
- `scripts/bench.py` checks round-trip budgets against a real remote; run it
  after touching `driver/git.py` or the service call paths.

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
