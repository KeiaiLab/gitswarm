# Agents on other hosts

A workspace is a branch. An agent that cannot run gitswarm, or runs on a
machine without the hive, works with plain git.

```
coordinator (has gitswarm)            other host (git only)
  ws create ──▶ clone, branch_name ──▶ git clone -b <branch_name> <url>
                                       commit, git push origin HEAD
  ws publish <id> ◀──────────────────── "done"
  ws read / ws tree <id>
  ws drop <id>
```

## Steps

1. Create without `--checkout`. Hand the other host the `clone` field.

   ```console
   $ gitswarm ws create --agent reviewer
   {"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", …, "path": null, "token": null, "clone": "git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74", …}
   ```

2. On the other host: clone, commit, push.

   ```console
   $ git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git work
   $ cd work && git add REVIEW.md && git commit -qm "Add review"
   $ git push origin HEAD
   To file:///tmp/demo/remote.git
      bc876f2..87d3743  HEAD -> gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74
   ```

   `git clone -b` takes `branch_name` (`gitswarm/ws/<id>`), not `branch`
   (`refs/heads/gitswarm/ws/<id>`).

3. Record the result. Any host with gitswarm and push access can do this,
   including the other host itself (inside its clone the remote is found
   from `origin`).

   ```console
   $ gitswarm ws publish 01M4B3P6V3D549KPH4SC5BTJ74
   {"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", "oid": "87d3743c1103ca653c4afe8db4d7a9e841649344", "remote": "file:///tmp/demo/remote.git"}
   ```

## What publish records

- Without a local worktree, `ws publish` reads the remote branch tip and
  stores it as `published_oid`; state becomes `published`; a
  `ws.published` event is written.
- It does not check ancestry. A force-pushed history is recorded as is.
- If the tip still equals `base_oid`, it is refused (`InvalidState`,
  exit 4): "nothing published: branch is still at base; commit and push to
  the branch first".
- Publishing again later records the new tip.
- `git push` alone changes nothing in gitswarm: `ws get` keeps showing
  `open` until someone runs `ws publish`.

## Tokens

With a token adapter ([Forgejo](forgejo-tokens.md), [GitHub](github-app.md))
`ws create` returns `token`, a credential scoped to that repository. Give
it to the other host's git through a credential helper or the
environment, never inside the URL: credentials (userinfo) in http(s)/git
URLs are refused — use a credential helper; ssh login names are fine. A
URL also ends up in shell history and `.git/config`. The token is shown
once and revoked on `ws drop` (GitHub tokens instead expire after one
hour).

## Cleanup

The coordinator owns cleanup: `ws drop <id>` when it has what it needs.
`ws gc` reclaims only open workspaces past `ttl_s` (default 7200 s) whose branch has not
moved since this host last saw it. It never touches published workspaces,
and it reports an expired workspace that another host pushed to under
`conflicted` instead of dropping it. Both need an explicit `ws drop <id>`.
