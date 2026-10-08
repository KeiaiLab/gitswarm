"""MCP 표면. 도구 하나 = CLI 명령 하나. 오류는 페이로드로 돌려준다(에이전트가 분기한다).

remote 는 선택이다 — 없으면 서버 프로세스의 cwd 로 찾는다(CLI 와 같은 규칙). 성공 결과의
`remote` 가 실제로 쓴 원격이다.
"""

from __future__ import annotations

from functools import wraps

from fastmcp import FastMCP

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import GitswarmError
from gitswarm.service.stats import summarize
from gitswarm.service.workspace import Checkout
from gitswarm.store.hive import Hive, resolve_home
from gitswarm.surfaces.common import (
    UsageError,
    agent_dict,
    doctor_report,
    parse_state,
    read_payload,
    resolve_base,
    service,
    usage_payload,
    with_remote,
)

mcp = FastMCP("gitswarm")


def payload(fn):
    """GitswarmError → {ok:false,...} 반환. 그 밖의 예외는 그대로(버그는 숨기지 않는다)."""

    @wraps(fn)
    def inner(*args, **kwargs) -> dict:
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except GitswarmError as e:
            return e.to_payload()
        except UsageError as e:
            return usage_payload(str(e))

    return inner


@mcp.tool
@payload
def hive_init(url: str) -> dict:
    """Create the local hive (bare mirror) for a remote. Idempotent. Returns {ok, url, path}. On failure returns {ok: false, error: {kind, detail}}."""
    hive = Hive.init(url, resolve_home())
    return {"url": hive.url, "path": str(hive.path)}


@mcp.tool
@payload
def workspace_create(
    remote: str | None = None,
    base_ref: str | None = None,
    agent_name: str = "",
    agent_run: str = "",
    ttl_s: int = DEFAULT_TTL_S,
    from_ws: str | None = None,
    checkout: bool = False,
    labels: dict[str, str] | None = None,
) -> dict:
    """Create an isolated workspace branch from a base. Creates the hive if missing.

    base_ref defaults to the remote HEAD. checkout=True also adds a local worktree and
    returns its path. ttl_s: seconds until gc may drop it (0 = never). from_ws forks from
    that workspace's branch tip. Returns {ok, id, branch, branch_name, base_oid, path, token,
    clone, remote} (path is null unless checkout=True; token is null without a token adapter). On failure returns {ok: false, error: {kind, detail}}.
    """
    agent = agent_dict(agent_name, agent_run)
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    svc = service(remote)
    base = resolve_base(svc, base_ref, from_ws)
    r = svc.create(base, agent, ttl_s, from_ws, mode, labels or {})
    return with_remote(svc, r.to_dict())


@mcp.tool
@payload
def workspace_get(ws_id: str, remote: str | None = None) -> dict:
    """Show a workspace record. Returns {ok, id, branch, state, agent, parent, labels, ..., remote}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote, ws_id)
    return with_remote(svc, svc.get(ws_id).to_dict())


@mcp.tool
@payload
def workspace_list(remote: str | None = None, state: str | None = None) -> dict:
    """List workspaces. state = open|published|dropped. Returns {ok, workspaces: [record, ...], invalid: [{id, detail}, ...], remote} (invalid = unreadable records; reclaim with workspace_drop). On failure returns {ok: false, error: {kind, detail}}."""
    flt = parse_state(state)
    svc = service(remote)
    good, invalid = svc.list_report(flt)
    return with_remote(svc, {"workspaces": [w.to_dict() for w in good], "invalid": invalid})


@mcp.tool
@payload
def workspace_read_file(ws_id: str, path: str, remote: str | None = None) -> dict:
    """Read a file at the workspace's remote branch tip, without a checkout.

    Returns {ok, path, content, remote} (UTF-8 text) or {ok, path, content_b64, remote} (binary). On failure returns {ok: false, error: {kind, detail}}.
    """
    svc = service(remote, ws_id)
    return with_remote(svc, read_payload(path, svc.read(ws_id, path)))


@mcp.tool
@payload
def workspace_tree(ws_id: str, path: str = "", remote: str | None = None) -> dict:
    """List one directory level at the workspace's remote branch tip (path "" = root). Returns {ok, path, entries: [{name, kind, oid}], remote}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote, ws_id)
    return with_remote(svc, {"path": path, "entries": svc.tree(ws_id, path)})


@mcp.tool
@payload
def workspace_publish(ws_id: str, remote: str | None = None) -> dict:
    """Record the branch tip as published. Pushes the local worktree first if there is one; otherwise records the tip another host pushed (InvalidState if it is still at base). Returns {ok, id, oid, remote}; oid = the recorded tip. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote, ws_id)
    return with_remote(svc, {"id": ws_id, "oid": svc.publish(ws_id)})


@mcp.tool
@payload
def workspace_drop(ws_id: str, remote: str | None = None) -> dict:
    """Delete the branch, worktree and token; mark dropped. Idempotent. Returns {ok, id, state, ..., remote}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote, ws_id)
    return with_remote(svc, svc.drop(ws_id).to_dict())


@mcp.tool
@payload
def workspace_gc(remote: str | None = None) -> dict:
    """Drop open workspaces past their ttl. Returns {ok, expired: [ws id, ...], invalid: [{id, detail}, ...], conflicted: [ws id, ...], remote} (invalid = skipped unreadable records; conflicted = skipped because the branch tip is neither the base nor the last tip this host pushed or saw, or the worktree holds unpushed or uncommitted work; reclaim both with workspace_drop). On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, svc.gc())


@mcp.tool
@payload
def events_tail(remote: str | None = None, since: str | None = None) -> dict:
    """Events from the meta log, newest first. since = only events after this meta oid. Returns {ok, events: [{kind, id, oid, at, payload}], invalid: [{oid, detail}], remote}; invalid lists meta commits that could not be read as events (skipped, not fatal). On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    events, invalid = svc.events_report(since)
    return with_remote(svc, {"events": [e.to_dict() for e in events], "invalid": invalid})


@mcp.tool
@payload
def doctor(remote: str | None = None) -> dict:
    """Check git, home, config, remote, hive, unrevoked tokens and ssh multiplexing. Returns {ok, checks: [{name, ok, detail}], remote?}; ok=false means a check failed, see its detail. Remote checks are skipped when no remote is given or found."""
    return doctor_report(remote)


@mcp.tool
@payload
def stats(remote: str | None = None) -> dict:
    """Counts by event kind and state, age of the oldest open workspace. Returns {ok, by_kind, by_state, open_oldest_age_s, total_events, invalid, unrevoked_tokens, remote} (open_oldest_age_s is null with no open workspace). On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, summarize(svc))


def serve() -> None:
    mcp.run()
