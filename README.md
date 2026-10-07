# gitswarm

[한국어](README.ko.md)

Canonical repository: git.keiailab.com/keiailab-oss/gitswarm · mirror: github.com/KeiaiLab/gitswarm (issues welcome on either)

gitswarm is a coordination layer over any git remote for many LLM agents
working on the same repository at once. Each agent run gets an isolated
workspace branch; agents read each other's work without a checkout, publish,
and drop. All state lives in the remote (`refs/heads/gitswarm/*`): no server,
no daemon, no database. Every CLI command has one MCP tool with the same
arguments and result.

## Install

```sh
uv tool install gitswarm      # or: uvx gitswarm --help
```

PyPI publication is pending. Until then, install from git:

```sh
uvx --from git+https://github.com/KeiaiLab/gitswarm gitswarm --help
```

Claude Code (details: [docs/recipes/claude-code.md](docs/recipes/claude-code.md)):

```sh
claude mcp add gitswarm -- uvx gitswarm mcp
# until PyPI:
claude mcp add gitswarm -- uvx --from git+https://github.com/KeiaiLab/gitswarm gitswarm mcp
```

Platform: POSIX (Linux, macOS), git >= 2.39, Python >= 3.11.

## 60 seconds

Run it inside a clone. gitswarm finds the remote (`origin`), creates its
local mirror on first use, and branches from the remote HEAD. The outputs
below are real, from a `file://` remote whose default branch is `main`.

```console
$ cd repo
$ gitswarm ws create --agent impl --checkout
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "branch": "refs/heads/gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": "/tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3NX30CF0XQFN7FGKTRSBC", "token": null, "clone": "git clone -b gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "remote": "file:///tmp/demo/remote.git"}
```

Work in `path` with plain git, then publish. Inside the worktree the remote
is found from the hive, so no flag is needed.

```console
$ cd /tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3NX30CF0XQFN7FGKTRSBC
$ git add src/calc.py && git commit -qm "Add calc"
$ gitswarm ws publish 01M4B3NX30CF0XQFN7FGKTRSBC
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "oid": "0212f996898c32b842cdc3068580f9ac019391a0", "remote": "file:///tmp/demo/remote.git"}
```

Anyone can read it without a checkout:

```console
$ gitswarm ws tree 01M4B3NX30CF0XQFN7FGKTRSBC src
{"ok": true, "path": "src", "entries": [{"name": "app.py", "kind": "blob", "oid": "b80e3222ab264bd7cafb376749bd18814fd66776"}, {"name": "calc.py", "kind": "blob", "oid": "4693ad3cf8b0903b98497fb89b8b524fbf1b93f4"}], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm ws read 01M4B3NX30CF0XQFN7FGKTRSBC src/calc.py
{"ok": true, "path": "src/calc.py", "content": "def add(a, b):\n    return a + b\n", "remote": "file:///tmp/demo/remote.git"}
```

Drop it when done. This deletes the remote branch, the worktree and any
token. It is idempotent.

```console
$ gitswarm ws drop 01M4B3NX30CF0XQFN7FGKTRSBC
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "state": "dropped", "base_ref": "refs/heads/main", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "branch": "refs/heads/gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "agent": {"name": "impl"}, "parent": null, "created_at": "2026-10-07T11:58:44Z", "ttl_s": 7200, "labels": {}, "token_id": null, "published_oid": "0212f996898c32b842cdc3068580f9ac019391a0", "remote": "file:///tmp/demo/remote.git"}
```

The remote is chosen in this order: `--remote`, `$GITSWARM_REMOTE`, the cwd
(inside a hive worktree: that hive; inside a git repo: `origin`). The
`remote` key of every success line is the remote actually used. With none
of these:

```console
$ cd /tmp && gitswarm ws list
{"ok": false, "error": {"kind": "Usage", "detail": "no remote: pass --remote (MCP: remote), set $GITSWARM_REMOTE, or run inside a git repo with an origin"}}
```

`--base` defaults to the branch the remote HEAD names (`main` here, `stable`
on some hosts). Pass it to pin the base and save one round trip.

## Agents on other hosts

An agent on another machine needs only git. Run the `clone` field of the
create result, commit, push, then record the result with `ws publish`. A
plain `git push` does not change the workspace state; `ws publish` does.

```console
$ gitswarm ws create --agent reviewer
{"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", "branch": "refs/heads/gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": null, "token": null, "clone": "git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74", "remote": "file:///tmp/demo/remote.git"}

# on the other host
$ git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git work
$ cd work && git add REVIEW.md && git commit -qm "Add review"
$ git push origin HEAD
To file:///tmp/demo/remote.git
   bc876f2..87d3743  HEAD -> gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74
$ gitswarm ws publish 01M4B3P6V3D549KPH4SC5BTJ74
{"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", "oid": "87d3743c1103ca653c4afe8db4d7a9e841649344", "remote": "file:///tmp/demo/remote.git"}
```

Use `branch_name` (`gitswarm/ws/<id>`), not `branch` (`refs/heads/…`), with
`git clone -b`. Publishing before any commit was pushed is refused:

```console
$ gitswarm ws publish 01M4B3PT92MMV3BST3Q0JDNBR6
{"ok": false, "error": {"kind": "InvalidState", "detail": "nothing published: branch is still at base; commit and push to the branch first"}}
```

Whoever coordinates the run drops the workspace (`ws drop`), or `ws gc`
does it once `ttl_s` has passed. More: [docs/recipes/other-host.md](docs/recipes/other-host.md).

## When publish says Conflict

`ws publish` from a worktree pushes with a lease: it overwrites the remote
branch only if the branch is still where gitswarm last saw it. If someone
else pushed in between, nothing is overwritten and you get `Conflict`
(exit 3):

```console
$ gitswarm ws publish 01M4B3PF534F5ZZJDWM0YWK6TM
{"ok": false, "error": {"kind": "Conflict", "detail": "refs/heads/gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM moved on remote; run `git pull --rebase origin gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM` in the worktree"}}
```

Rebase onto their commits and publish again. gitswarm sees that your HEAD
contains the remote tip, so nothing can be lost, and pushes:

```console
$ git pull --rebase origin gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM
$ gitswarm ws publish 01M4B3PF534F5ZZJDWM0YWK6TM
{"ok": true, "id": "01M4B3PF534F5ZZJDWM0YWK6TM", "oid": "efb41f844494ac27c759c925cad13257e1c037f7", "remote": "file:///tmp/demo/remote.git"}
$ gitswarm ws read 01M4B3PF534F5ZZJDWM0YWK6TM NOTES.md
{"ok": true, "path": "NOTES.md", "content": "notes\n", "remote": "file:///tmp/demo/remote.git"}
```

A plain `git fetch` in the worktree does not move the lease baseline
(`refs/gitswarm/lease/…`); only gitswarm's own pushes do. Publishing again
without the rebase returns the same `Conflict`.

## Commands and MCP tools

Every command prints one JSON line (except `--help`). MCP tools return the
same object. `remote` is optional everywhere; MCP uses the server's cwd.

| CLI | MCP tool | returns |
|---|---|---|
| `hive init <url>` | `hive_init` | `url, path` |
| `ws create [--base] [--agent] [--run] [--ttl] [--from-ws] [--checkout] [--label k=v]` | `workspace_create` | `id, branch, branch_name, base_oid, path, token, clone, remote` |
| `ws get <id>` | `workspace_get` | the record: `id, state, base_ref, base_oid, branch, agent, parent, created_at, ttl_s, labels, token_id, published_oid, remote` |
| `ws list [--state]` | `workspace_list` | `workspaces, invalid, remote` |
| `ws read <id> <path>` | `workspace_read_file` | `path, content` (UTF-8) or `content_b64`, `remote` |
| `ws tree <id> [path]` | `workspace_tree` | `path, entries [{name, kind, oid}], remote` |
| `ws publish <id>` | `workspace_publish` | `id, oid, remote` |
| `ws drop <id>` | `workspace_drop` | the record, `state: dropped` |
| `ws gc` | `workspace_gc` | `expired, invalid, conflicted, remote` |
| `events tail [--since <oid>]` | `events_tail` | `events [{kind, id, oid, at, payload}], remote` |
| `doctor` | `doctor` | `ok, checks [{name, ok, detail}], remote` |
| `stats` | `stats` | `by_kind, by_state, open_oldest_age_s, total_events, invalid, unrevoked_tokens, remote` |
| `mcp` | — | runs the MCP server on stdio |

- `invalid` lists records that cannot be read; `conflicted` lists expired
  workspaces whose branch someone pushed after this host last saw it. `gc`
  skips both; reclaim them with `ws drop <id>`.
- `token` is returned once by `create` and never stored (only `token_id`).
  It is `null` unless a token adapter is configured.
- Event kinds: `ws.created`, `ws.published`, `ws.dropped`, `ws.expired`,
  `ws.revoked`.

```console
$ gitswarm ws gc
{"ok": true, "expired": ["01M4B3PT92MMV3BST3Q0JDNBR6"], "invalid": [], "conflicted": [], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm events tail --since 1ab2a0e68ea5680f66af0bc5c6e5662b81f7c49b
{"ok": true, "events": [{"kind": "ws.expired", "id": "01M4B3PT92MMV3BST3Q0JDNBR6", "oid": "d75a472ed8e037e0d4d1daf677112a1cb4873c13", "at": "2026-10-07T21:00:23+09:00", "payload": {"agent": {"name": "idle"}, "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "base_ref": "refs/heads/main", "branch": "refs/heads/gitswarm/ws/01M4B3PT92MMV3BST3Q0JDNBR6", "created_at": "2026-10-07T11:59:14Z", "id": "01M4B3PT92MMV3BST3Q0JDNBR6", "labels": {"task": "demo"}, "parent": null, "published_oid": null, "state": "dropped", "token_id": null, "ttl_s": 60}}], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm stats
{"ok": true, "by_kind": {"ws.expired": 1, "ws.dropped": 4, "ws.published": 6, "ws.created": 7}, "by_state": {"dropped": 5, "published": 2}, "open_oldest_age_s": null, "total_events": 18, "invalid": 0, "unrevoked_tokens": 0, "remote": "file:///tmp/demo/remote.git"}
$ gitswarm doctor
{"ok": true, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/home"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": true, "detail": "reachable; default branch main"}, {"name": "hive", "ok": true, "detail": "/tmp/demo/home/hives/c63648f6fa869444: 1 worktree(s)"}, {"name": "tokens", "ok": true, "detail": "every recorded token revoked"}, {"name": "ssh_mux", "ok": true, "detail": "not an ssh remote"}], "remote": "file:///tmp/demo/remote.git"}
```

`doctor` against an SSH remote also checks connection multiplexing:

```console
$ gitswarm doctor --remote ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git
{"ok": true, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/homeR"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": true, "detail": "reachable; default branch stable"}, {"name": "hive", "ok": true, "detail": "absent; the first ws command creates it"}, {"name": "tokens", "ok": true, "detail": "no hive; nothing recorded"}, {"name": "ssh_mux", "ok": true, "detail": "connections are multiplexed"}], "remote": "ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git"}
```

### Exit codes

Failures print `{"ok": false, "error": {"kind", "detail"}}`. `detail` ends
with the next step when there is one.

| kind | meaning | exit |
|---|---|---|
| `Usage` | bad arguments or no remote found (surfaces only) | 1 |
| `NotFound` | id, ref or path missing; malformed id | 2 |
| `Conflict` | someone else moved the branch or meta; CAS retries exhausted | 3 |
| `InvalidState` | transition not allowed, malformed record, refused URL | 4 |
| `Unsupported` | the adapter lacks the capability | 5 |
| `RemoteError` | git or the hosting API failed | 6 |

`doctor` prints `{"ok": false, "checks": […]}` and exits 1 when any check
fails. A refused `--remote` is still `InvalidState` (exit 4).

```console
$ gitswarm doctor --remote file:///tmp/demo/missing.git
{"ok": false, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/home"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": false, "detail": "file:///tmp/demo/missing.git unreachable: fatal: '/tmp/demo/missing.git' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\nand the repository exists."}, {"name": "hive", "ok": true, "detail": "absent; the first ws command creates it"}, {"name": "tokens", "ok": true, "detail": "no hive; nothing recorded"}, {"name": "ssh_mux", "ok": true, "detail": "not an ssh remote"}], "remote": "file:///tmp/demo/missing.git"}
```

## Configuration

`~/.gitswarm/config.toml` (move the whole home with `GITSWARM_HOME`).
Without it every remote is plain git and no events are sent.

```toml
[remote."git.example.com"]
adapter = "forgejo"                 # issues a repo-scoped token per workspace
api = "https://git.example.com"
user = "gitswarm-bot"
credential_file = "~/.config/gitswarm/forgejo.cred"

[remote."github.com"]
adapter = "github"                  # GitHub App installation token
api = "https://api.github.com"
user = "<app_id>/<installation_id>"
credential_file = "~/.config/gitswarm/github-app.pem"

[[sink]]
kind = "jsonl"                      # or "webhook" with target = "https://…"
target = "~/.gitswarm/events.jsonl"
```

The adapter is chosen by the remote's host; undeclared hosts are plain git.
Sinks receive each event once, with no retry; `events tail --since` replays
from the log.

**Credential scope.** The Forgejo `credential_file` is a secret for the
whole `user` account: Forgejo's token endpoint accepts only account
authentication. Use a dedicated bot account that exists only to mint
tokens, is a member of the target org, and can write only the target
repositories. The GitHub PEM can mint tokens for every repository the App
is installed on; install it on the needed repositories only. GitHub
installation tokens last one hour and cannot be revoked by id; `drop`
prints their expiry to stderr. Setup: [forgejo-tokens.md](docs/recipes/forgejo-tokens.md),
[github-app.md](docs/recipes/github-app.md).

**Credentials in URLs are refused.** `https://user:token@host/…` fails
with `InvalidState`. Use a git credential helper.

## Authorization boundary

A workspace is not bound to the agent that created it. Anyone who can push
`refs/heads/gitswarm/*` can publish or drop any workspace and rewrite any
record; the boundary is the remote's push ACL. Every value read from the
meta branch is validated and fails closed. `ws drop` deletes unpublished
commits on that branch, including other people's; `ws gc` never deletes a
commit this host has not seen. Details: [SECURITY.md](SECURITY.md).

## CI

Workspace branches are ordinary branches. Make your CI ignore
`gitswarm/**`, or every create, publish and meta write starts a run:

```yaml
on:
  push:
    branches-ignore:
      - "gitswarm/**"
```

Forgejo Actions and GitHub Actions use the same syntax.
More: [docs/recipes/ci.md](docs/recipes/ci.md).

## Performance

A command makes 1 to 4 remote round trips. SSH connections are multiplexed
per hive, so a command opens no new handshake while the master lives (60 s).
Measured against Forgejo over SSH (RTT 0.2 s), median of 3 rounds:

| command | ssh connections | new handshakes | seconds (median) | budget |
|---|---|---|---|---|
| hive init | 1 | 1 | 3.49 | - |
| ws create --checkout | 4 | 0 | 1.62 | ≤ 4 conn, ≤ 2.5 s |
| ws get | 1 | 0 | 0.29 | ≤ 1 conn |
| ws list | 1 | 0 | 0.71 | ≤ 1 conn |
| ws read | 2 | 0 | 0.54 | ≤ 2 conn |
| ws tree | 2 | 0 | 0.49 | ≤ 2 conn |
| ws publish | 4 | 0 | 1.68 | ≤ 4 conn, ≤ 2.5 s |
| events tail | 1 | 0 | 1.30 | ≤ 1 conn |
| ws gc | 1 | 0 | 0.64 | ≤ 1 conn |
| ws drop | 4 | 0 | 1.71 | ≤ 4 conn |

Reproduce with `uv run scripts/bench.py <remote-url> --base <branch>`
(exit 1 if over budget). The connection budget is the real gate; the 2.5 s
budget is tight on a shared server, where a push sometimes takes seconds.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The gate, as CI runs it:

```sh
uv sync --dev
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run vulture src vulture_whitelist.py --min-confidence 60
uv run pytest -q --cov          # coverage must be 100%
```

Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/superpowers/specs/](docs/superpowers/specs/). Changes: [CHANGELOG.md](CHANGELOG.md).

## Roadmap

A Workspace (this release) → B Intent (record why each commit exists:
instruction, rationale, checks) → C Landing (merge concurrent results:
serial rebase and machine arbitration; per-agent ownership).

## License

MIT. Copyright KeiaiLab.
