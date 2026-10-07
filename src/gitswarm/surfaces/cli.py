"""CLI. 출력은 항상 JSON 한 줄 — 에이전트·CI 가 파싱한다. service 만 부른다."""

from __future__ import annotations

import base64
import json
import sys
from functools import wraps
from typing import Annotated

import typer

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import EXIT_CODES, GitswarmError
from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive, resolve_home

REMOTE_ENV = "GITSWARM_REMOTE"
EXIT_USAGE = 1

app = typer.Typer(no_args_is_help=True, add_completion=False)
hive_app = typer.Typer(no_args_is_help=True)
ws_app = typer.Typer(no_args_is_help=True)
events_app = typer.Typer(no_args_is_help=True)
app.add_typer(hive_app, name="hive")
app.add_typer(ws_app, name="ws")
app.add_typer(events_app, name="events")

RemoteOpt = Annotated[
    str | None, typer.Option("--remote", envvar=REMOTE_ENV, help="git remote URL")
]


def _emit(payload: dict) -> None:
    typer.echo(json.dumps({"ok": True, **payload}, ensure_ascii=False))


def _remote(remote: str | None) -> str:
    if remote:
        return remote
    detail = f"--remote or ${REMOTE_ENV} required"
    typer.echo(json.dumps({"ok": False, "error": {"kind": "Usage", "detail": detail}}))
    raise typer.Exit(EXIT_USAGE)


def guarded(fn):
    """GitswarmError → JSON + 종료코드. 그 밖의 예외는 그대로(버그는 숨기지 않는다)."""

    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GitswarmError as e:
            typer.echo(json.dumps(e.to_payload(), ensure_ascii=False))
            raise typer.Exit(EXIT_CODES[e.kind]) from None

    return inner


def _svc(remote: str | None):
    return open_service(_remote(remote), resolve_home())


# ── hive ─────────────────────────────────────────────────────
@hive_app.command("init")
@guarded
def hive_init(url: str) -> None:
    hive = Hive.init(url, resolve_home())
    _emit({"url": hive.url, "path": str(hive.path)})


# ── ws ───────────────────────────────────────────────────────
@ws_app.command("create")
@guarded
def ws_create(
    remote: RemoteOpt = None,
    base: Annotated[str, typer.Option("--base")] = "main",
    agent: Annotated[str, typer.Option("--agent")] = "",
    run: Annotated[str, typer.Option("--run")] = "",
    ttl: Annotated[int, typer.Option("--ttl")] = DEFAULT_TTL_S,
    from_ws: Annotated[str | None, typer.Option("--from-ws")] = None,
    checkout: Annotated[bool, typer.Option("--checkout")] = False,
    label: Annotated[list[str] | None, typer.Option("--label", help="k=v")] = None,
) -> None:
    agent_info = {k: v for k, v in (("name", agent), ("run", run)) if v}
    labels = dict(kv.split("=", 1) for kv in (label or []))
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    r = _svc(remote).create(base, agent_info, ttl, from_ws, mode, labels)
    _emit(r.to_dict())


@ws_app.command("get")
@guarded
def ws_get(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit(_svc(remote).get(ws_id).to_dict())


@ws_app.command("list")
@guarded
def ws_list(
    remote: RemoteOpt = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
) -> None:
    flt = WsState(state) if state else None
    _emit({"workspaces": [w.to_dict() for w in _svc(remote).list(flt)]})


@ws_app.command("read")
@guarded
def ws_read(ws_id: str, path: str, remote: RemoteOpt = None) -> None:
    data = _svc(remote).read(ws_id, path)
    try:
        _emit({"path": path, "content": data.decode("utf-8")})
    except UnicodeDecodeError:
        _emit({"path": path, "content_b64": base64.b64encode(data).decode()})


@ws_app.command("tree")
@guarded
def ws_tree(ws_id: str, path: str = "", remote: RemoteOpt = None) -> None:
    _emit({"path": path, "entries": _svc(remote).tree(ws_id, path)})


@ws_app.command("publish")
@guarded
def ws_publish(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit({"id": ws_id, "oid": _svc(remote).publish(ws_id)})


@ws_app.command("drop")
@guarded
def ws_drop(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit(_svc(remote).drop(ws_id).to_dict())


@ws_app.command("gc")
@guarded
def ws_gc(remote: RemoteOpt = None) -> None:
    _emit({"expired": _svc(remote).gc()})


# ── mcp ──────────────────────────────────────────────────────
@app.command("mcp")
def mcp_serve() -> None:
    """stdio MCP 서버를 띄운다(Task 9)."""
    from gitswarm.surfaces.mcp import serve

    serve()


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
