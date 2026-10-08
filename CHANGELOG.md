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
- Remote discovery from the cwd (hive worktree or `origin`), then, for
  commands taking an id, the one hive holding `wt/<id>`; on-demand hive
  creation, `--base` defaulting to the remote HEAD.
- Deletes are leased: `ws drop` follows the current tip; `ws gc` deletes a
  branch only when its tip is the base or the last tip this host pushed or
  saw, never removes a worktree with unpushed or uncommitted work, and
  reports the rest as `conflicted`. The hive keeps reflogs for 90 days.
  Broken records and meta commits are reported as `invalid`, not fatal.
- Adapters: plain git; Forgejo (repository-scoped PAT per workspace,
  `repositories: [{owner, name}]` as Forgejo 16 expects, revoked on drop,
  retried and counted when revocation fails); GitHub (App installation
  token).
- Event sinks: JSONL file and webhook, sent once; `events tail --since`
  replays from git.
- SSH connection multiplexing per hive (ControlMaster, 60 s persist) and
  batched record reads: create, publish and drop in 4 round trips (create
  5 with the default base, drop 7 when the branch moved), reads in 1–2,
  gc 1 plus 3 per expired workspace.
- `scripts/bench.py` measuring SSH connections and latency per command
  against a budget.

### Security

- Every remote URL (flag, env, discovery, MCP, `hive.toml`) passes an
  allowlist: `ssh`, `git+ssh`, `https`, `http`, `file`, scp-style or
  absolute paths; no leading `-`, control characters or `x::` transports.
  git gets `--` before every URL and every user-supplied ref.
- Credentials (userinfo) in http(s) URLs are refused — use a credential
  helper; ssh login names are fine. URLs in errors and logs are redacted
  (`***@`) and truncated.
- Records read from the meta branch are untrusted: ids, branch, token id,
  oids, timestamps, ttl and text are validated; failures are
  `InvalidState`/`NotFound`, never a traceback or a write elsewhere.
- Credentials are read from files, held with `repr=False`, and never
  logged; errors carry the operation and status only.
- `hive.toml` is escaped on write and validated on read; hive creation is
  atomic (temp dir, locked rename).
- The GitHub App credential file must hold an RSA private key; anything
  else is refused before use.
- CI removes its clone token from `.git/config` before the test suite runs
  pull-request code.
- Authorization boundary: a workspace is not bound to its creator; the
  remote's push ACL on `refs/heads/gitswarm/*` is the boundary. See
  [SECURITY.md](https://github.com/KeiaiLab/gitswarm/blob/stable/SECURITY.md).
- Transfers stall-bounded (HTTP low-speed limit, SSH keepalive); `ls-remote`
  probes time out after 60 s. `git://` is refused: it has neither bound.
- Repository-locating variables inherited from hooks or CI (`GIT_DIR`,
  `GIT_WORK_TREE`, …) are removed before every git call.
- Error details echo at most 120 characters of remote text; git stderr in
  `RemoteError` is capped at 2 KiB; records over 64 KiB are `invalid`.

### Fixed before release

Found by the pre-release review and fixed before tagging:

- `ws gc` removed a worktree holding unpushed commits, and the bare hive
  kept no reflog. gc now reports such a workspace (HEAD ahead of the last
  seen tip, or dirty) under `conflicted`; `ws drop` still removes it but
  first records the local tips in the reflog of `refs/gitswarm/trash`
  (`drop <id>`), so `git -C <hive>/repo.git reflog show refs/gitswarm/trash`
  and `git -C <hive>/repo.git branch <name> <oid>` recover the commit for
  90 days.
- An inherited `GIT_DIR` redirected hive commands into the caller's
  repository (and rewrote its `.git/config`).
- One foreign or corrupt commit on the meta branch made `events tail` and
  `stats` fail for everyone. `events tail` now lists it under
  `invalid: [{oid, detail}]` and `stats` counts it in `invalid`. Event kinds
  are checked against the closed set of five, and payloads are the
  validated record (unknown keys dropped).
- Remote text reached callers unbounded (1 MB error details, injected
  event kinds). Details are capped at 120 characters, git stderr at 2 KiB,
  records at 64 KiB.
- A malformed `config.toml` or `--base refs/tags/…` produced a Python
  traceback instead of one JSON line; both are now errors in the contract
  (`InvalidState`, `NotFound`).
- `ws drop` of an already dropped record skipped cleanup, leaving a branch
  pushed after the drop, or a worktree on another host, with no reclaim
  path. Re-drop now deletes the branch, worktree and refs again.
- `ws gc` on a host other than the creator reported untouched expired
  workspaces as `conflicted`; a tip still at `base_oid` is now reclaimed.
- A failed `ws create --checkout` left an open record and a remote branch
  and hid the id; the worktree step is now compensated and the error names
  the id.
- `ws read` on a directory and `ws tree` on a file returned `RemoteError`;
  they are `NotFound`. An unknown `events tail --since` oid is `NotFound`.
- `ws publish` racing a drop suggested a rebase onto a deleted branch; it
  now says the branch is gone from the remote.
- `ws publish <id>` outside the worktree with only `GITSWARM_HOME` set
  found no remote; the id now names its hive.
- Stale `.tmp-*`/`.lock-*` residue and worktree metadata of hand-deleted
  worktrees were never reclaimed; every command now sweeps them.
- Docs: round-trip counts, the ControlMaster socket path
  (`hives/<id>/repo.git/.ssh-control/`), the `--` claim, the `gc`
  guarantees, and the other-host recipe (push before publish) now match
  the code.

### Known limitations

- No per-agent ownership: anyone who can push `refs/heads/gitswarm/*` can
  publish or drop any workspace. The boundary is the remote's push ACL.
- GitHub installation tokens last one hour, cannot be revoked by id, and
  are not refreshed.
- Event sinks have no retry and no signature.
- `ws publish` records the remote tip without an ancestry check.
- `ws gc` never reclaims published workspaces or open ones another host
  pushed to but nobody published (reported as `conflicted`); they need
  `ws drop`.
- Forgejo repository scoping depends on the server honouring the
  `repositories` field; gitswarm does not verify the issued token's scope.
- CI that runs on every push must ignore `gitswarm/**` branches, or each
  workspace and meta write starts a run.
- `uv.lock` is not committed; dependency versions resolve at install time.
- POSIX only (uses `fcntl`). git >= 2.39, Python >= 3.11.
- Not yet on PyPI; install from git.
- B (Intent) and C (Landing) are not implemented.
- The Forgejo and GitHub adapters are tested by contract (`respx`) only,
  not against live servers.
- MCP server threads sharing one hive are not stress-tested.
- `doctor`'s SSH probe starts a ControlMaster under a temporary directory;
  a 60 s master may outlive its deleted socket path (not reproduced).

[0.1.0]: https://github.com/KeiaiLab/gitswarm/releases/tag/v0.1.0
