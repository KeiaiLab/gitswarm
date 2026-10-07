"""MCP 표면. 도구 하나 = CLI 명령 하나. 오류는 페이로드로 돌려준다(에이전트가 분기한다)."""

from __future__ import annotations

import base64
from functools import wraps

from fastmcp import FastMCP

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import GitswarmError
from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive, resolve_home

mcp = FastMCP("gitswarm")


def payload(fn):
    """GitswarmError → {ok:false,...} 반환. 그 밖의 예외는 그대로(버그는 숨기지 않는다)."""

    @wraps(fn)
    def inner(*args, **kwargs) -> dict:
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except GitswarmError as e:
            return e.to_payload()

    return inner


def _svc(remote: str):
    return open_service(remote, resolve_home())


@mcp.tool
@payload
def hive_init(url: str) -> dict:
    """원격 하나에 대한 로컬 hive(bare 미러)를 만든다. 멱등."""
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
    """base 에서 격리 workspace 브랜치를 만든다. checkout=True 면 로컬 worktree 경로도 준다."""
    agent = {k: v for k, v in (("name", agent_name), ("run", agent_run)) if v}
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    return _svc(remote).create(base_ref, agent, ttl_s, from_ws, mode, labels or {}).to_dict()


@mcp.tool
@payload
def workspace_get(remote: str, ws_id: str) -> dict:
    """workspace 메타를 읽는다."""
    return _svc(remote).get(ws_id).to_dict()


@mcp.tool
@payload
def workspace_list(remote: str, state: str | None = None) -> dict:
    """workspace 목록. state = open|published|dropped."""
    flt = WsState(state) if state else None
    return {"workspaces": [w.to_dict() for w in _svc(remote).list(flt)]}


@mcp.tool
@payload
def workspace_read_file(remote: str, ws_id: str, path: str) -> dict:
    """체크아웃 없이 발행된 파일을 읽는다. 텍스트는 content, 이진은 content_b64."""
    data = _svc(remote).read(ws_id, path)
    try:
        return {"path": path, "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "content_b64": base64.b64encode(data).decode()}


@mcp.tool
@payload
def workspace_tree(remote: str, ws_id: str, path: str = "") -> dict:
    """디렉터리 한 단계를 나열한다."""
    return {"path": path, "entries": _svc(remote).tree(ws_id, path)}


@mcp.tool
@payload
def workspace_publish(remote: str, ws_id: str) -> dict:
    """로컬 worktree 의 커밋을 원격 workspace 브랜치로 push 한다."""
    return {"id": ws_id, "oid": _svc(remote).publish(ws_id)}


@mcp.tool
@payload
def workspace_drop(remote: str, ws_id: str) -> dict:
    """브랜치·worktree·토큰을 거두고 dropped 로 표시한다. 멱등."""
    return _svc(remote).drop(ws_id).to_dict()


@mcp.tool
@payload
def workspace_gc(remote: str) -> dict:
    """ttl 이 지난 open workspace 를 drop 한다."""
    return {"expired": _svc(remote).gc()}


def serve() -> None:
    mcp.run()
