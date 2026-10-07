# Claude Code

## Register

```sh
# from PyPI (pending)
claude mcp add gitswarm -- uvx gitswarm mcp
# until PyPI
claude mcp add gitswarm -- uvx --from git+https://git.keiailab.com/keiailab-oss/gitswarm gitswarm mcp
```

- `-s user` makes it available in every project; the default scope is the
  current project only.
- `-e GITSWARM_HOME=/path` moves the hives and `config.toml`. Keep the path
  short: the SSH control socket must fit in 104 bytes, or multiplexing is
  skipped (`gitswarm doctor` reports it).
- The server runs in the directory Claude Code was started in. Inside a
  clone, tools find the remote from `origin`, so `remote` can be omitted.
  `GITSWARM_REMOTE` applies to the CLI only; pass `remote` to the tool to
  target another repository.

Check it: ask Claude Code to call the `doctor` tool, or run
`gitswarm doctor` in the same directory.

## Tools

`hive_init`, `workspace_create`, `workspace_get`, `workspace_list`,
`workspace_read_file`, `workspace_tree`, `workspace_publish`,
`workspace_drop`, `workspace_gc`, `events_tail`, `doctor`, `stats`.
Arguments and results match the CLI ([README](../../README.md#commands-and-mcp-tools)).
Failures are returned, not raised: `{"ok": false, "error": {"kind", "detail"}}`.
Branch on `kind`; `detail` names the next step.

## Three agents at once

A lead agent gives each sub-agent its own workspace, lets them work in
parallel, reads the results and cleans up. Below are the actual payloads
from one stdio server handling the calls concurrently (captured with a
scripted MCP client against a `file://` remote; `→` call, `←` result).

Each sub-agent creates a workspace with a worktree:

```
→ workspace_create {"agent_name": "tester", "checkout": true}
← {"ok": true, "id": "01M4B3R92QR3ZHPARHDY71R00J", "branch": "refs/heads/gitswarm/ws/01M4B3R92QR3ZHPARHDY71R00J", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": "/tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3R92QR3ZHPARHDY71R00J", "token": null, "clone": "git clone -b gitswarm/ws/01M4B3R92QR3ZHPARHDY71R00J file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3R92QR3ZHPARHDY71R00J", "remote": "file:///tmp/demo/remote.git"}
→ workspace_create {"agent_name": "coder", "checkout": true}
← {"ok": true, "id": "01M4B3R92VEG717J4H6F76ZH1M", "branch": "refs/heads/gitswarm/ws/01M4B3R92VEG717J4H6F76ZH1M", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": "/tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3R92VEG717J4H6F76ZH1M", "token": null, "clone": "git clone -b gitswarm/ws/01M4B3R92VEG717J4H6F76ZH1M file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3R92VEG717J4H6F76ZH1M", "remote": "file:///tmp/demo/remote.git"}
→ workspace_create {"agent_name": "planner", "checkout": true}
← {"ok": true, "id": "01M4B3R92PAAECZVM0BFZ4VJBX", "branch": "refs/heads/gitswarm/ws/01M4B3R92PAAECZVM0BFZ4VJBX", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": "/tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3R92PAAECZVM0BFZ4VJBX", "token": null, "clone": "git clone -b gitswarm/ws/01M4B3R92PAAECZVM0BFZ4VJBX file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3R92PAAECZVM0BFZ4VJBX", "remote": "file:///tmp/demo/remote.git"}
```

Each works in its `path` with plain git (`git add`, `git commit`), then
publishes. All three write the shared meta branch; meta writes are
compare-and-swap with retry, so concurrent writers do not lose updates:

```
→ workspace_publish {"ws_id": "01M4B3R92VEG717J4H6F76ZH1M"}
← {"ok": true, "id": "01M4B3R92VEG717J4H6F76ZH1M", "oid": "4ed21ab52a323a02fdbe9c4d19c69d53dc028e71", "remote": "file:///tmp/demo/remote.git"}
→ workspace_publish {"ws_id": "01M4B3R92PAAECZVM0BFZ4VJBX"}
← {"ok": true, "id": "01M4B3R92PAAECZVM0BFZ4VJBX", "oid": "8166ff33d30ae24a07c48156143453a275b09058", "remote": "file:///tmp/demo/remote.git"}
→ workspace_publish {"ws_id": "01M4B3R92QR3ZHPARHDY71R00J"}
← {"ok": true, "id": "01M4B3R92QR3ZHPARHDY71R00J", "oid": "cf9772a26725a79ee5fccef82f5f069ab7dc106f", "remote": "file:///tmp/demo/remote.git"}
```

The lead reads a result without a checkout:

```
→ workspace_read_file {"ws_id": "01M4B3R92PAAECZVM0BFZ4VJBX", "path": "planner.md"}
← {"ok": true, "path": "planner.md", "content": "planner was here\n", "remote": "file:///tmp/demo/remote.git"}
```

and drops each workspace when it has taken what it needs:

```
→ workspace_drop {"ws_id": "01M4B3R92PAAECZVM0BFZ4VJBX"}
← {"ok": true, "id": "01M4B3R92PAAECZVM0BFZ4VJBX", "state": "dropped", "base_ref": "refs/heads/main", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "branch": "refs/heads/gitswarm/ws/01M4B3R92PAAECZVM0BFZ4VJBX", "agent": {"name": "planner"}, "parent": null, "created_at": "2026-10-07T12:00:02Z", "ttl_s": 7200, "labels": {}, "token_id": null, "published_oid": "8166ff33d30ae24a07c48156143453a275b09058", "remote": "file:///tmp/demo/remote.git"}
```

```
→ stats {}
← {"ok": true, "by_kind": {"ws.dropped": 4, "ws.published": 6, "ws.created": 7}, "by_state": {"dropped": 4, "published": 2, "open": 1}, "open_oldest_age_s": 52, "total_events": 17, "invalid": 0, "unrevoked_tokens": 0, "remote": "file:///tmp/demo/remote.git"}
```

(The counts include workspaces from earlier runs on the same remote.)

## Prompting tips

- Tell each sub-agent to call `workspace_create` with its own
  `agent_name` and `checkout: true`, work only under the returned `path`,
  commit, and call `workspace_publish`.
- On `Conflict` from `workspace_publish`, run the `git pull --rebase …`
  from `detail` in the worktree and publish again.
- On `InvalidState` "nothing published", commit first.
- The lead calls `workspace_drop` (or `workspace_gc` for anything past its
  `ttl_s`).
