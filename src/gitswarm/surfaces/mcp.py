"""MCP 표면. 도구 하나 = CLI 명령 하나. 오류는 페이로드로 돌려준다(에이전트가 분기한다).

remote 는 선택이다 — 없으면 서버 프로세스의 cwd 로 찾는다(CLI 와 같은 규칙). 성공 결과의
`remote` 가 실제로 쓴 원격이다.
"""

from __future__ import annotations

from functools import wraps

from fastmcp import FastMCP

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import GitswarmError
from gitswarm.service.workspace import Checkout
from gitswarm.store.hive import Hive, resolve_home
from gitswarm.surfaces.common import (
    UsageError,
    agent_dict,
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
    """원격 하나에 대한 로컬 hive(bare 미러)를 만든다. 멱등. Returns {ok, url, path}. On failure returns {ok: false, error: {kind, detail}}."""
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
    """base 에서 격리 workspace 브랜치를 만든다. checkout=True 면 로컬 worktree 경로도 준다.

    base_ref 를 생략하면 원격 HEAD 가 가리키는 브랜치다. hive 가 없으면 먼저 만든다(멱등).
    ttl_s 는 초 단위(0 = 만료 없음). Returns {ok, id, branch, base_oid, path, token, remote}
    (path 은 checkout=True 일 때만). On failure returns {ok: false, error: {kind, detail}}.
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
    """workspace 메타를 읽는다. Returns {ok, id, branch, state, agent, parent, labels, ...}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, svc.get(ws_id).to_dict())


@mcp.tool
@payload
def workspace_list(remote: str | None = None, state: str | None = None) -> dict:
    """workspace 목록. state = open|published|dropped. Returns {ok, workspaces: [meta, ...], invalid: [{id, detail}, ...]} (invalid = 읽을 수 없는 레코드, workspace_drop 으로 거둔다). On failure returns {ok: false, error: {kind, detail}}."""
    flt = parse_state(state)
    svc = service(remote)
    good, invalid = svc.list_report(flt)
    return with_remote(svc, {"workspaces": [w.to_dict() for w in good], "invalid": invalid})


@mcp.tool
@payload
def workspace_read_file(ws_id: str, path: str, remote: str | None = None) -> dict:
    """체크아웃 없이 발행된 파일을 읽는다.

    Returns {ok, path, content} (UTF-8 텍스트) 또는 {ok, path, content_b64} (이진). On failure returns {ok: false, error: {kind, detail}}.
    """
    svc = service(remote)
    return with_remote(svc, read_payload(path, svc.read(ws_id, path)))


@mcp.tool
@payload
def workspace_tree(ws_id: str, path: str = "", remote: str | None = None) -> dict:
    """디렉터리 한 단계를 나열한다. Returns {ok, path, entries}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, {"path": path, "entries": svc.tree(ws_id, path)})


@mcp.tool
@payload
def workspace_publish(ws_id: str, remote: str | None = None) -> dict:
    """workspace 의 원격 브랜치 tip 을 발행 결과로 기록한다. 로컬 worktree 가 있으면 그 커밋을 먼저 push 하고, 없으면 다른 호스트가 push 한 tip 을 기록한다(base 그대로면 InvalidState). Returns {ok, id, oid} — oid = 기록한 tip. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, {"id": ws_id, "oid": svc.publish(ws_id)})


@mcp.tool
@payload
def workspace_drop(ws_id: str, remote: str | None = None) -> dict:
    """브랜치·worktree·토큰을 거두고 dropped 로 표시한다. 멱등. Returns {ok, id, state, ...}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, svc.drop(ws_id).to_dict())


@mcp.tool
@payload
def workspace_gc(remote: str | None = None) -> dict:
    """ttl 이 지난 open workspace 를 drop 한다. Returns {ok, expired: [ws id, ...], invalid: [{id, detail}, ...], conflicted: [ws id, ...]} (invalid = 건너뛴 망가진 레코드, conflicted = 본 뒤 남이 push 해 건너뛴 브랜치; 둘 다 workspace_drop 으로 거둔다). On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, svc.gc())


@mcp.tool
@payload
def events_tail(remote: str | None = None, since: str | None = None) -> dict:
    """meta 로그를 이벤트로 돌려준다(최신순). since = 마지막으로 본 oid. Returns {events: [...]}. On failure returns {ok: false, error: {kind, detail}}."""
    svc = service(remote)
    return with_remote(svc, {"events": [e.to_dict() for e in svc.events(since)]})


def serve() -> None:
    mcp.run()
