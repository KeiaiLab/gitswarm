# gitswarm architecture

Scope: sub-project A (Workspace). Binding design:
`docs/superpowers/specs/2026-10-06-gitswarm-workspace-design.md`.
Names below are the names in the code.

## 1. What it is

gitswarm gives each LLM agent run an isolated git workspace and keeps the
registry of workspaces in the same git remote.

- A coordination layer over any git remote. State lives in refs on that
  remote; the service is stateless.
- Not a server: no daemon, no lock file, no database.
- Not a VCS or a storage backend: it drives the `git` binary.
- One core, two surfaces. Every CLI command has one MCP tool with the same
  arguments and result (`ws create` / `workspace_create`).

## 2. Layers

```
  agent ──CLI──▶ ┌──────────────────────────────────────────┐
  agent ──MCP──▶ │ surfaces/   cli.py (typer) · mcp.py      │
                 │             common.py (shared shapes)    │
                 ├──────────────────────────────────────────┤
                 │ service/    workspace.py                 │──▶ adapters/
                 ├──────────────────────────────────────────┤    remote.py (Protocol)
                 │ store/      meta.py (CAS) · hive.py      │    plain.py
                 ├──────────────────────────────────────────┤    forgejo.py
                 │ driver/     git.py                       │    github.py
                 └───────────────────┬──────────────────────┘
                                     ▼
                              git subprocess ──▶ any git remote
```

Rules:

- A layer calls only its direct neighbour below.
- `subprocess` appears only in `driver/git.py`. Upper layers handle oids,
  refs and bytes.
- Names and limits live in `constants.py`. Layers do not repeat literals.
- Surfaces catch `GitswarmError` only. Other exceptions are bugs and are
  not hidden.

## 3. Ref layout

Remote:

```
<remote>
  refs/heads/gitswarm/meta          orphan branch. tree: ws/<id>.json
                                    one state change = one commit
  refs/heads/gitswarm/ws/<id>       workspace branch, forked from base_oid
```

Local hive (one per remote, `GITSWARM_HOME` or `~/.gitswarm`):

```
~/.gitswarm/
  config.toml                       adapters, sinks
  hives/<sha256(url)[:16]>/         url normalised: scheme lowercased,
    hive.toml                       trailing "/" and ".git" removed
    repo.git/                       bare repo, remote "origin"
      refs/remotes/origin/gitswarm/meta     cache written by fetch
      refs/remotes/origin/gitswarm/ws/<id>  cache written by fetch
      refs/heads/gitswarm/ws/<id>           local branch (worktree target)
      refs/gitswarm/lease/gitswarm/ws/<id>  private publish/drop baseline
      refs/gitswarm/peek/gitswarm/ws/<id>   read-only lookups
    wt/<id>/                        worktree (create --checkout)
    .ssh-control/mux                SSH ControlMaster socket (section 5)
```

- `refs/heads/` is the one namespace every host accepts pushes to
  (`REF_PREFIX = "gitswarm/"`, `META_REF`).
- There is no local `refs/heads/gitswarm/meta`. New meta commits are pushed
  by oid.
- `lease/*` and `peek/*` mirror the full ref name under their prefix
  (`LEASE`, `PEEK`).

## 4. The workspace record

`ws/<id>.json` (sorted keys, one JSON object):

| field | type | note |
|---|---|---|
| `id` | ULID, 26 chars | `^[0-9A-HJKMNP-TV-Z]{26}$`, must equal the path |
| `state` | `open` \| `published` \| `dropped` | `WsState` |
| `base_ref`, `base_oid` | str | base fixed at create |
| `branch` | str | must equal `refs/heads/gitswarm/ws/<id>` |
| `agent` | object | `{name, run}` |
| `parent` | ULID or null | set by `--from-ws` |
| `created_at` | ISO-8601 with timezone | |
| `ttl_s` | int | `0` = forever, else `<= MAX_TTL_S` (1 year); default `DEFAULT_TTL_S = 7200` |
| `labels` | object | free-form |
| `token_id` | digits or null | adapter token id |
| `published_oid` | 40-hex or null | remote tip recorded by `publish` |

State machine (`TRANSITIONS`):

```
open ──publish──▶ published ──drop──▶ dropped
  └──────────────────drop──────────────────▲
```

Same-state re-transitions are allowed (`publish` on `published`, `drop` on
`dropped`). Anything else is `InvalidState`. `dropped` is terminal.

### Validation on read

The meta branch is writable by anyone with push rights to the remote. A
record is therefore untrusted input. `Workspace.from_json` fails closed with
`InvalidState` on:

- bad JSON, unknown `state`, wrong types (a numeric `id` is not a string)
- `id` not a ULID, or different from the path it was read from (`_parse_record`)
- `branch` different from the value recomputed from `id` (stops a record
  from pointing a delete at another ref)
- `parent` not a ULID; `token_id` not digits (it goes into a URL path);
  `published_oid` not 40 lowercase hex
- `ttl_s` not an int in range (bool rejected); `created_at` unparsable or
  without timezone

`get` raises on a bad record. `list` and `gc` skip it and report it under
`invalid: [{id, detail}]`, so one bad record does not stop everyone. A bad
record is removed with `ws drop <id>` (force-drop, section 6).

## 5. The meta CAS

Every state change is `MetaStore.apply(Change(path, transform, subject))`.
`transform: bytes | None -> bytes` receives the current content and returns
the new one. It is not a value to overwrite with: on each retry it runs again
on the new tip, so a transition is re-judged against what others wrote.

```
apply(change):
 for attempt in 0 .. META_CAS_RETRIES-1:          # 16
   sleep backoff_s(attempt-1)  if attempt > 0
   old  = fetch origin meta          ──▶ refs/remotes/origin/gitswarm/meta
   prev = old:<path>                 (None if absent)
   new_content = transform(prev)     ──▶ may raise InvalidState / NotFound
   tree = old.tree + (path ↦ blob)
   new  = commit-tree(tree, parent=old, "<kind> <id>")
   push new ─▶ refs/heads/gitswarm/meta, lease = old
        ok       ──▶ return new
        rejected ──▶ next attempt
 raise Conflict
```

```
host A                 remote meta                  host B
  fetch ─────────────▶ tip=T0 ◀───────────────────── fetch
  commit A1 (parent T0)                      commit B1 (parent T0)
  push lease=T0 ─────▶ T0 → A1  ok
                       push lease=T0 ◀─────────────── rejected
                                                    backoff (jitter)
                       tip=A1 ◀───────────────────── fetch
                                                    transform(prev from A1)
                                                    commit B2 (parent A1)
                       A1 → B2 ◀──────────────────── push lease=A1  ok
```

Backoff (`backoff.py`): `min(CAS_BACKOFF_MAX_S, CAS_BACKOFF_BASE_S * 2**n)`
times U(0.5, 1.5). Constants: `META_CAS_RETRIES = 16`,
`CAS_BACKOFF_BASE_S = 0.05`, `CAS_BACKOFF_MAX_S = 2.0`. Jitter spreads
writers that were rejected in the same beat. 8 attempts with a 1.0 s cap
were exhausted in 1 of 3 runs with 4 concurrent writers.

`Conflict` means: sixteen attempts all lost the race (meta), or a branch moved
under a lease (publish, drop, gc), or a ULID already has a record. It never
means a hook or permission failure.

`create` compensates. The remote branch is pushed first (lease expecting the
ref to be absent). Any later failure, including meta `Conflict`, runs
`_undo_create`: delete remote branch, local ref, lease ref, revoke token.
Each step is attempted even if the previous failed; the original exception
is re-raised. No orphan branch without a record remains.

### Git traps and countermeasures

CAS rests on `git push --force-with-lease=<ref>:<expected> <oid>:<ref>`.
Four measured behaviours break the naive use (git 2.55).

**1. Same-oid push skips the lease.** When the remote already holds the
pushed oid, git prints `Everything up-to-date` and exits 0 without checking
the lease. A same-oid push is not a CAS.
Countermeasure: no `ls-remote` pre-check. `Git.push` runs once, without
`-q`, and reads the result. rc 0 with a whole-line `Everything up-to-date`
(a server hook may print the same words as `remote: ...`, so substring
matching is wrong) means the remote already held `oid` and the lease was
skipped. `push` returns `expected == oid`: success only if the caller
expected that oid, otherwise `False` (a lost race). Nothing is pushed in
either case, so there is no window to close.

Every git call runs with `LC_ALL=C`. The `Everything up-to-date`,
`couldn't find remote ref` and rejection markers are English text; a
translated git would break the classification.

**2. Rejections have several wordings.** `REJECTED_MARKERS`:

```
[rejected]                                  (stale info)
[remote rejected] ... incorrect old value provided
... reference already exists                (create race)
cannot lock ref ...                         (server-side lock contention)
```

`is_lease_rejection(stderr)` matches any marker; `push` then returns
`False` and the loop retries. Any other failure, in particular
`pre-receive hook declined`, raises `RemoteError`. Hook declines never
match and never retry. `delete_remote` applies the same classification.

**3. A read can move the lease base.** A plain `git fetch` of a branch
moves `refs/remotes/origin/*`. A lease based on the tracking ref is defeated
by any read, including one in a worktree. Countermeasures:

- publish and drop take their baseline from the private
  `refs/gitswarm/lease/<ref>` (`_seen`). Only gitswarm's own successful
  pushes (create, publish) write it. The tracking ref is the fallback for
  branches this host did not create.
- Read-only lookups (`read`, `tree`, `create --from-ws`, `create` base)
  use `Git.peek`: it fetches by URL into `refs/gitswarm/peek/<ref>`, because
  fetching through the named remote would also update the tracking ref.

**4. Siblings race on the local ref directory.** Several processes sharing
one hive create and delete directories under `refs/remotes/origin/` while
fetching: `cannot lock ref ... unable to create directory`. `fetch` and
`peek` run through `_lock_retry`: up to `FETCH_LOCK_RETRIES = 5` attempts,
jittered backoff, retrying only when the error contains `cannot lock ref`.
Other errors raise at once.

**Absent refs are read from the error.** `fetch` and `peek` do one
`git fetch` and treat `couldn't find remote ref` (or `remote ref does not
exist`) as "absent": the local tracking or peek ref is deleted and `None`
is returned. Other failures raise `RemoteError`.

**SSH is multiplexed per hive.** Network commands (`fetch`, `push`,
`ls-remote`) get `GIT_SSH_COMMAND` with `ControlMaster=auto`,
`ControlPath=<repo>/.ssh-control/mux` and `ControlPersist` (60 s), so the
calls of one command, and the next commands within the window, share one
handshake. Multiplexing is skipped (plain git behaviour) when:

- the caller set `GIT_SSH_COMMAND` or `GIT_SSH`, or git config has
  `core.sshCommand` (the env var would override it);
- the socket path is too long for `sun_path` (104 bytes, minus the
  ssh temp suffix) or contains `"`.

The socket name is fixed (`mux`, not `%C`) to stay under the path limit.
That is safe because a hive holds exactly one remote.

### Round trips

Network calls per operation (one `fetch`, `push` or delete each):

| operation | calls |
|---|---|
| `create`, `publish`, `drop` | 4 |
| `read`, `tree` | 2 |
| `get`, `list`, `events tail`, `gc` | 1 |

`scripts/bench.py <remote-url>` measures this against a real remote: it
counts ssh invocations and new handshakes per command over repeated
create-to-drop cycles and exits 1 when a command exceeds its budget
(`BUDGETS`: call count; 2.5 s median for `create` and `publish`).

## 6. publish, drop, gc

**publish** (`open|published -> published`)

1. Re-judge the transition.
2. With a local worktree: read its `HEAD` and push it with lease =
   `_seen(branch)` (the private lease ref). If rejected, `peek` the remote
   tip. If it is an ancestor of `HEAD` (`merge-base --is-ancestor`), the
   agent rebased onto it and no commit is lost, so push once more with
   lease = that tip. Otherwise `Conflict` ("run `git pull --rebase`").
   Move the lease ref to `HEAD`. The recorded oid is `HEAD`.
3. Without a worktree (another host pushed the branch): `peek` the remote
   tip and record it. Tip equal to `base_oid` is `InvalidState` ("nothing
   published"); a missing branch is `NotFound`. No push, no ancestry check.
4. Record `ws.published` via CAS, with `published_oid` = the oid from 2 or
   3. A repeated publish overwrites it.

**drop** (`-> dropped`, idempotent) deletes the remote branch, local refs
(branch, peek, lease), the worktree, revokes the token, then records
`ws.dropped`. The remote delete is leased on the last seen oid
(`_delete_branch`):

| mode | used by | when the lease is rejected or no oid was seen |
|---|---|---|
| `LeaseMode.FOLLOW` | explicit `drop` | peek the current tip, delete once more with that lease. Dropping means "discard this workspace", others' pushes included |
| `LeaseMode.STRICT` | `gc` | peek the current tip; stop with `Conflict` if it exists |

A branch already gone counts as deleted, in both modes. A `drop` whose meta
CAS lost after the branch was deleted leaves an `open` record without a
branch; the next `gc` records it as `ws.expired`.

**gc** drops `open` workspaces with `created_at + ttl_s < now` (`ttl_s = 0`
never expires). Result: `{expired, invalid, conflicted}`.

- `expired`: ids dropped, recorded as `ws.expired`.
- `invalid`: malformed records, skipped.
- `conflicted`: branch moved after this host last saw it, skipped.

Rule: gc never deletes a commit this host has not seen. Both skipped
classes are removed by an explicit `ws drop <id>`.

**force-drop**: `drop` on a malformed record (`InvalidState` from `get`)
does not parse it. It recomputes the branch from the id (refusing if a
stored `branch` disagrees), deletes it unconditionally, revokes a
well-formed `token_id`, and writes a salvaged record (`_salvage`): valid
fields kept, others defaulted, `created_at` set to now, `ttl_s = 0`.

## 7. Events

The event log is the meta branch history. Commit subject: `<kind> <id>`.
Kinds: `ws.created`, `ws.published`, `ws.dropped`, `ws.expired`
(force-drop emits `ws.dropped`).

- `gitswarm events tail [--since <oid>]` lists `{kind, id, oid, at, payload}`
  for commits in `<since>..meta`. `since` must match `^[0-9a-f]{40}$`.
  `payload` is the record at that commit; `at` is the commit time.
- Sinks (`[[sink]]` in `config.toml`, `kind = "jsonl" | "webhook"`) fire
  once, after the CAS push succeeded. No retry, no queue. A failing sink
  prints one line to stderr and never raises: the record is already on the
  remote, and an error would make the caller retry and duplicate.
- A consumer that missed events replays from its last oid with `--since`.
  Nothing is lost because the log is in git.
- Sink `at` is the emitting host's clock; `tail` uses the committer time.

## 8. Adapters

```python
class RemoteAdapter(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...   # {TOKEN}
    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token: ...
    def revoke_token(self, token_id: str) -> None: ...
```

Selection (`adapters/select.py`): the remote URL host is looked up in
`config.toml` `[remote."<host>"]`; `adapter = "forgejo"` selects Forgejo, `"github"` selects GitHub,
anything else (or no entry) selects plain. Both `https://` and scp-style
`git@host:org/repo` URLs are parsed.

- **plain**: no capabilities. `issue_token` and `revoke_token` raise
  `Unsupported`. `create` skips issuing when `TOKEN` is absent and returns
  `token: null`. Not an error.
- **forgejo**: `POST /api/v1/users/<user>/tokens` with name
  `gitswarm-<ws_id>`, scope `read:repository` or `write:repository`, and
  `repositories: [<org/repo>]`, so the PAT reaches one repository. Basic
  auth `(user, credential)` on every request. `credential` is read from
  `credential_file` and kept with `field(repr=False)`. Error messages carry
  the operation, HTTP status or exception class name, never URL or
  credential. `revoke_token` accepts digits only (the id goes into the URL)
  and treats 404 as done. Drop revokes the recorded `token_id` even if the
  adapter config changed since.

- **github**: App installation token. `user` is `<app_id>/<installation_id>`
  (both positive digit strings, at most `MAX_ID_DIGITS`), `credential_file`
  is the App PEM. `from_spec` loads the PEM and rejects anything that is not
  an RSA private key (`Unsupported`). Each `issue_token` signs a fresh RS256
  JWT (`iat` backdated 60 s, `exp` = now + `JWT_EXP_S` = 540 s, under
  GitHub's 600 s cap), `POST /app/installations/<id>/access_tokens` with
  `repositories: [<repo name>]` and `permissions: {contents: read|write}`.
  The token lives one hour and has no name; `Token.id` is its expiry epoch.
  It cannot be revoked by id (GitHub revokes only the token you present, and
  gitswarm does not keep the secret), so `revoke_token` is a documented
  no-op that prints the expiry time to stderr. It does not raise
  `Unsupported`: the service reads that as "adapter has no tokens".
  `Token.secret` and the PEM are `repr=False`; errors carry the operation
  and HTTP status or exception class only.

Other hosts plug in the same way: implement the Protocol, add a name in
`select.py`. Git traffic is unaffected; adapters only add capabilities that
git does not have.

## 9. Errors and contracts

| kind | meaning | CLI exit |
|---|---|---|
| `Usage` | bad arguments (surface only) | 1 |
| `NotFound` | id, ref or path missing; invalid id | 2 |
| `Conflict` | CAS exhausted, branch moved, id collision | 3 |
| `InvalidState` | disallowed transition, malformed record | 4 |
| `Unsupported` | adapter lacks the capability | 5 |
| `RemoteError` | git failed (non-lease), HTTP failure | 6 |

- CLI prints exactly one JSON line. Success: `{"ok": true, ...}`. Failure:
  `{"ok": false, "error": {"kind", "detail"}}` plus the exit code above.
- MCP returns the same dicts; failures are payloads, not exceptions.
- `ws read` returns `content` (UTF-8) or `content_b64` (binary).
- Ambiguity is an error. No path guesses and continues.
- `--remote` or `GITSWARM_REMOTE` selects the remote; `hive init` must run
  first (`NotFound` otherwise).
- `create` returns the PAT once in `token`; it is not stored in meta (only
  `token_id` is).

## 10. Testing

- Real `git` against `file://` bare remotes. No mocked git.
- Races use spawned processes sharing one hive, and forced interleaving
  where timing would be flaky.
- `pre-receive` hooks cover the "hook decline stays a hard error" case.
- CLI: typer `CliRunner` and direct `main()` calls. MCP: in-process fastmcp
  client. Forgejo: `respx`.
- Gates: `pytest --cov` with `fail_under = 100`, ruff, vulture. 253 tests at
  the time of writing.
- Not tested: a real two-host race over a network. It is simulated by
  forcing the interleaving of two writers against one remote; the CAS
  depends only on the remote's ref update being atomic.

## 11. Authorization and roadmap

Sub-project A binds no workspace to the agent that created it. Anyone who
can push `refs/heads/gitswarm/*` can publish or drop any workspace and
rewrite any record. The boundary is the remote's push ACL on that
namespace. Repo-scoped tokens (Forgejo) narrow it per workspace. Per-agent
ownership and approval belong to C.

```
A Workspace  ──▶  B Intent  ──▶  C Landing
lifecycle         why a commit    serial rebase + machine
(this doc)        exists          arbitration; ownership
                  refs/notes/
                  gitswarm/intent
```

B and C are separate designs and are not implemented.

## Spec and code differences

Checked against the spec amended for `published_oid` and the round-trip
sentence.

- spec section 1: `build.yml`; code: `.forgejo/workflows/ci.yml` + `.forgejo/ci/verify.sh`.
- spec section 1 layout omits `adapters/github.py`, `adapters/select.py`; section 10 lists the GitHub adapter as out of scope while section 5 specifies it.
- spec section 3 record: no `token_id`; code stores it.
- spec section 3 gc: `{expired, invalid}`; code adds `conflicted`.
- spec section 4 drop: lease on the last seen oid; code adds `LeaseMode` FOLLOW (explicit drop) vs STRICT (gc).
- spec section 4.1/4.2: lease ref `refs/gitswarm/lease/<id>`, peek ref `<short>`; code uses the full ref name under each prefix.
- spec section 5: `capabilities() -> set`, `issue_token(ws, scope)`, `revoke_token(token)`; code: `frozenset`, `issue_token(repo, ws_id, scope)`, `revoke_token(token_id: str)`.
- spec section 7: no `Usage` kind; code has it (exit 1, surfaces only).
