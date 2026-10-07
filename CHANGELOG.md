# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-10-07

First release: sub-project A, Workspace.

### Added

- Workspaces over any git remote: `ws create`, `get`, `list`, `read`,
  `tree`, `publish`, `drop`, `gc`, plus `hive init`, `events tail`,
  `doctor`, `stats`. One JSON line per command.
- MCP server (`gitswarm mcp`, stdio) with one tool per command and the
  same arguments and results.
- State in the remote only: `refs/heads/gitswarm/meta` (one JSON record
  per workspace; its commit history is the event log) and
  `refs/heads/gitswarm/ws/<id>`. No server, daemon, lock file or database.
- Meta writes as compare-and-swap via `push --force-with-lease`, re-reading
  and re-validating the record on each retry (16 retries, jittered
  exponential backoff capped at 2 s).
- `ws create --checkout` adds a local worktree; without it, other hosts use
  the returned `clone` command and plain git.
- `ws publish` pushes the worktree with a private lease baseline, or, with
  no worktree, records the tip another host pushed (`published_oid`).
  After `git pull --rebase` it pushes when HEAD contains the remote tip.
- Remote discovery from the cwd (hive worktree or `origin`), on-demand hive
  creation, `--base` defaulting to the remote HEAD.
- Deletes are leased: `ws drop` follows the current tip; `ws gc` deletes
  only commits this host has seen and reports the rest as `conflicted`.
  Broken records are reported as `invalid`, not fatal.
- Adapters: plain git; Forgejo (repository-scoped PAT per workspace,
  revoked on drop, retried and counted when revocation fails); GitHub
  (App installation token).
- Event sinks: JSONL file and webhook, sent once; `events tail --since`
  replays from git.
- SSH connection multiplexing per hive (ControlMaster, 60 s persist) and
  batched record reads: create/publish/drop in 4 round trips, reads in 1–2.
- `scripts/bench.py` measuring SSH connections and latency per command
  against a budget.

### Security

- Every remote URL (flag, env, discovery, MCP, `hive.toml`) passes an
  allowlist: `ssh`, `git+ssh`, `https`, `http`, `git`, `file`, scp-style or
  absolute paths; no leading `-`, control characters or `x::` transports.
  git gets `--` before URLs and refs.
- Credentials (userinfo) in http(s)/git URLs are refused — use a credential
  helper; ssh login names are fine. URLs in errors and logs are redacted
  (`***@`) and truncated.
- Records read from the meta branch are untrusted: ids, branch, token id,
  oids, timestamps, ttl and text are validated; failures are
  `InvalidState`/`NotFound`, never a traceback or a write elsewhere.
- Credentials are read from files, held with `repr=False`, and never
  logged; errors carry the operation and status only.
- `hive.toml` is escaped on write and validated on read; hive creation is
  atomic (temp dir, locked rename).
- Transfers stall-bounded (HTTP low-speed limit, SSH keepalive); `ls-remote`
  probes time out after 60 s.

### Known limitations

- No per-agent ownership: anyone who can push `refs/heads/gitswarm/*` can
  publish or drop any workspace. The boundary is the remote's push ACL.
- GitHub installation tokens last one hour, cannot be revoked by id, and
  are not refreshed.
- Event sinks have no retry and no signature.
- `ws publish` records the remote tip without an ancestry check.
- POSIX only (uses `fcntl`). git >= 2.39, Python >= 3.11.
- Not yet on PyPI; install from git.
- B (Intent) and C (Landing) are not implemented.

[0.1.0]: https://github.com/KeiaiLab/gitswarm/releases/tag/v0.1.0
