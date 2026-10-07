"""MCP 표면. 도구 하나 = CLI 명령 하나. 오류는 페이로드로 돌려준다(에이전트가 분기한다)."""

from __future__ import annotations

from functools import wraps

from fastmcp import FastMCP

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import GitswarmError
from gitswarm.service.workspace import Checkout, open_service
from gitswarm.store.hive import Hive, resolve_home
from gitswarm.surfaces.common import (
    UsageError,
    agent_dict,
    parse_state,
    read_payload,
    usage_payload,
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


def _svc(remote: str):
    return open_service(remote, resolve_home())


@mcp.tool
@payload
def hive_init(url: str) -> dict:
    """원격 하나에 대한 로컬 hive(bare 미러)를 만든다. 멱등. Returns {ok, url, path}. On failure returns {ok: false, error: {kind, detail}}."""
    hive = Hive.init(url, resolve_home())
    return {"url": hive.url, "path": str(hive.path)}


@mcp.tool
@payload
def workspace_create(
    remote: str,
    base_ref: str = "main",
    agent_name: str = "",
    agent_run: str = "",
    ttl_s: int = DEFAULT_TTL_S,
    from_ws: str | None = None,
    checkout: bool = False,
    labels: dict[str, str] | None = None,
) -> dict:
    """base 에서 격리 workspace 브랜치를 만든다. checkout=True 면 로컬 worktree 경로도 준다.

    ttl_s 는 초 단위(0 = 만료 없음). Returns {ok, id, branch, base_oid, path, token}
    (path 은 checkout=True 일 때만). On failure returns {ok: false, error: {kind, detail}}.
    """
    agent = agent_dict(agent_name, agent_run)
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    return _svc(remote).create(base_ref, agent, ttl_s, from_ws, mode, labels or {}).to_dict()


@mcp.tool
@payload
def workspace_get(remote: str, ws_id: str) -> dict:
    """workspace 메타를 읽는다. Returns {ok, id, branch, state, agent, parent, labels, ...}. On failure returns {ok: false, error: {kind, detail}}."""
    return _svc(remote).get(ws_id).to_dict()


@mcp.tool
@payload
def workspace_list(remote: str, state: str | None = None) -> dict:
    """workspace 목록. state = open|published|dropped. Returns {ok, workspaces: [meta, ...], invalid: [{id, detail}, ...]} (invalid = 읽을 수 없는 레코드, workspace_drop 으로 거둔다). On failure returns {ok: false, error: {kind, detail}}."""
    flt = parse_state(state)
    good, invalid = _svc(remote).list_report(flt)
    return {"workspaces": [w.to_dict() for w in good], "invalid": invalid}


@mcp.tool
@payload
def workspace_read_file(remote: str, ws_id: str, path: str) -> dict:
    """체크아웃 없이 발행된 파일을 읽는다.

    Returns {ok, path, content} (UTF-8 텍스트) 또는 {ok, path, content_b64} (이진). On failure returns {ok: false, error: {kind, detail}}.
    """
    data = _svc(remote).read(ws_id, path)
    return read_payload(path, data)


@mcp.tool
@payload
def workspace_tree(remote: str, ws_id: str, path: str = "") -> dict:
    """디렉터리 한 단계를 나열한다. Returns {ok, path, entries}. On failure returns {ok: false, error: {kind, detail}}."""
    return {"path": path, "entries": _svc(remote).tree(ws_id, path)}


@mcp.tool
@payload
def workspace_publish(remote: str, ws_id: str) -> dict:
    """로컬 worktree 의 커밋을 원격 workspace 브랜치로 push 한다. Returns {ok, id, oid}. On failure returns {ok: false, error: {kind, detail}}."""
    return {"id": ws_id, "oid": _svc(remote).publish(ws_id)}


@mcp.tool
@payload
def workspace_drop(remote: str, ws_id: str) -> dict:
    """브랜치·worktree·토큰을 거두고 dropped 로 표시한다. 멱등. Returns {ok, id, state, ...}. On failure returns {ok: false, error: {kind, detail}}."""
    return _svc(remote).drop(ws_id).to_dict()


@mcp.tool
@payload
def workspace_gc(remote: str) -> dict:
    """ttl 이 지난 open workspace 를 drop 한다. Returns {ok, expired: [ws id, ...], invalid: [{id, detail}, ...]} (invalid 는 건너뛴 망가진 레코드). On failure returns {ok: false, error: {kind, detail}}."""
    return _svc(remote).gc()


@mcp.tool
@payload
def events_tail(remote: str, since: str | None = None) -> dict:
    """meta 로그를 이벤트로 돌려준다(최신순). since = 마지막으로 본 oid. Returns {events: [...]}. On failure returns {ok: false, error: {kind, detail}}."""
    return {"events": [e.to_dict() for e in _svc(remote).events(since)]}


def serve() -> None:
    mcp.run()
