"""CLI. 출력은 항상 JSON 한 줄 — 에이전트·CI 가 파싱한다. service 만 부른다."""

from __future__ import annotations

import json
import sys
from functools import wraps
from typing import Annotated

import typer

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import EXIT_CODES, GitswarmError
from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive, resolve_home
from gitswarm.surfaces.common import agent_dict, read_payload, usage_payload

# typer>=0.27 vendors click as typer._click; older typer uses the real click.
try:
    from typer._click import exceptions as click_exc
except ImportError:  # pragma: no cover
    from click import exceptions as click_exc

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
    raise click_exc.UsageError(f"--remote or ${REMOTE_ENV} required")


def _parse_labels(items: list[str]) -> dict:
    labels = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise click_exc.UsageError(f"--label expects k=v, got {item!r}")
        labels[key] = value
    return labels


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
    agent_info = agent_dict(agent, run)
    labels = _parse_labels(label or [])
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
    state: Annotated[WsState | None, typer.Option("--state")] = None,
) -> None:
    _emit({"workspaces": [w.to_dict() for w in _svc(remote).list(state)]})


@ws_app.command("read")
@guarded
def ws_read(ws_id: str, path: str, remote: RemoteOpt = None) -> None:
    data = _svc(remote).read(ws_id, path)
    _emit(read_payload(path, data))


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
    try:
        rc = app(standalone_mode=False)
    except click_exc.UsageError as e:
        typer.echo(json.dumps(usage_payload(e.format_message()), ensure_ascii=False))
        sys.exit(EXIT_USAGE)
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except typer.Abort:
        typer.echo(json.dumps(usage_payload("aborted")))
        sys.exit(EXIT_USAGE)

    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    sys.exit(main())
