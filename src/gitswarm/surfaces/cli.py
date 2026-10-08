"""CLI. 출력은 JSON 한 줄(--help 제외) — 에이전트·CI 가 파싱한다. service 만 부른다."""

from __future__ import annotations

import json
import sys
from functools import wraps
from typing import Annotated

import typer

# typer>=0.27 vendors click as typer._click and raises its own UsageError — click's would
# not catch it. Pinned by `typer>=0.27,<1`; the usage-error tests go red if the path moves.
from typer._click.exceptions import UsageError as ClickUsageError

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import EXIT_CODES, GitswarmError
from gitswarm.service.stats import summarize
from gitswarm.service.workspace import Checkout, WsState
from gitswarm.store.hive import Hive, resolve_home
from gitswarm.surfaces.common import (
    UsageError,
    agent_dict,
    doctor_report,
    read_payload,
    resolve_base,
    service,
    usage_payload,
    with_remote,
)

REMOTE_ENV = "GITSWARM_REMOTE"
EXIT_USAGE = 1
EXIT_CHECK_FAILED = 1  # doctor: 검사 실패는 오류 종류가 아니라 보고다

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Isolated workspaces for concurrent agents over any git remote. Prints one JSON line.",
)
hive_app = typer.Typer(no_args_is_help=True)
ws_app = typer.Typer(no_args_is_help=True)
events_app = typer.Typer(no_args_is_help=True)
app.add_typer(hive_app, name="hive", help="Local bare mirror of a remote.")
app.add_typer(ws_app, name="ws", help="Workspace lifecycle: create, read, publish, drop.")
app.add_typer(events_app, name="events", help="Event log kept on the meta branch.")

# CLI ↔ MCP 같은 뜻 — 도구 docstring 과 짝을 맞춘다(surfaces/mcp.py)
REMOTE_HELP = "git remote URL. Default: $GITSWARM_REMOTE, else discovered from the cwd."
WS_ID_HELP = "Workspace id (ULID)."

RemoteOpt = Annotated[str | None, typer.Option("--remote", envvar=REMOTE_ENV, help=REMOTE_HELP)]
WsIdArg = Annotated[str, typer.Argument(help=WS_ID_HELP)]


def _emit(payload: dict) -> None:
    typer.echo(json.dumps({"ok": True, **payload}, ensure_ascii=False))


def _parse_labels(items: list[str]) -> dict:
    labels = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise ClickUsageError(f"--label expects k=v, got {item!r}")
        labels[key] = value
    return labels


def guarded(fn):
    """GitswarmError·Usage → JSON + 종료코드. 그 밖의 예외는 그대로(버그는 숨기지 않는다)."""

    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GitswarmError as e:
            typer.echo(json.dumps(e.to_payload(), ensure_ascii=False))
            raise typer.Exit(EXIT_CODES[e.kind]) from None
        except UsageError as e:
            typer.echo(json.dumps(usage_payload(str(e)), ensure_ascii=False))
            raise typer.Exit(EXIT_USAGE) from None

    return inner


# ── hive ─────────────────────────────────────────────────────
@hive_app.command("init", help="Create the local hive (bare mirror) for a remote. Idempotent.")
@guarded
def hive_init(url: Annotated[str, typer.Argument(help="git remote URL.")]) -> None:
    hive = Hive.init(url, resolve_home())
    _emit({"url": hive.url, "path": str(hive.path)})


# ── ws ───────────────────────────────────────────────────────
@ws_app.command(
    "create",
    help="Create an isolated workspace branch from a base. Creates the hive if missing.",
)
@guarded
def ws_create(
    remote: RemoteOpt = None,
    base: Annotated[
        str | None, typer.Option("--base", help="Base branch. Default: the remote HEAD.")
    ] = None,
    agent: Annotated[str, typer.Option("--agent", help="Agent name to record.")] = "",
    run: Annotated[str, typer.Option("--run", help="Agent run id to record.")] = "",
    ttl: Annotated[
        int, typer.Option("--ttl", help="Seconds until gc may drop it. 0 = never.")
    ] = DEFAULT_TTL_S,
    from_ws: Annotated[
        str | None, typer.Option("--from-ws", help="Fork from this workspace's branch tip.")
    ] = None,
    checkout: Annotated[
        bool, typer.Option("--checkout", help="Also add a local worktree; returns its path.")
    ] = False,
    label: Annotated[
        list[str] | None, typer.Option("--label", help="Label k=v. Repeatable.")
    ] = None,
) -> None:
    agent_info = agent_dict(agent, run)
    labels = _parse_labels(label or [])
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    svc = service(remote)
    r = svc.create(resolve_base(svc, base, from_ws), agent_info, ttl, from_ws, mode, labels)
    _emit(with_remote(svc, r.to_dict()))


@ws_app.command("get", help="Show a workspace record.")
@guarded
def ws_get(ws_id: WsIdArg, remote: RemoteOpt = None) -> None:
    svc = service(remote, ws_id)
    _emit(with_remote(svc, svc.get(ws_id).to_dict()))


@ws_app.command("list", help="List workspaces; unreadable records go to 'invalid'.")
@guarded
def ws_list(
    remote: RemoteOpt = None,
    state: Annotated[WsState | None, typer.Option("--state", help="Only this state.")] = None,
) -> None:
    svc = service(remote)
    good, invalid = svc.list_report(state)
    _emit(with_remote(svc, {"workspaces": [w.to_dict() for w in good], "invalid": invalid}))


@ws_app.command(
    "read", help="Read a file at the workspace's remote branch tip, without a checkout."
)
@guarded
def ws_read(
    ws_id: WsIdArg,
    path: Annotated[str, typer.Argument(help="File path in the workspace.")],
    remote: RemoteOpt = None,
) -> None:
    svc = service(remote, ws_id)
    _emit(with_remote(svc, read_payload(path, svc.read(ws_id, path))))


@ws_app.command("tree", help="List one directory level at the workspace's remote branch tip.")
@guarded
def ws_tree(
    ws_id: WsIdArg,
    path: Annotated[str, typer.Argument(help="Directory. Default: the root.")] = "",
    remote: RemoteOpt = None,
) -> None:
    svc = service(remote, ws_id)
    _emit(with_remote(svc, {"path": path, "entries": svc.tree(ws_id, path)}))


@ws_app.command(
    "publish",
    help="Record the branch tip as published. Pushes the local worktree first if there is one.",
)
@guarded
def ws_publish(ws_id: WsIdArg, remote: RemoteOpt = None) -> None:
    svc = service(remote, ws_id)
    _emit(with_remote(svc, {"id": ws_id, "oid": svc.publish(ws_id)}))


@ws_app.command("drop", help="Delete the branch, worktree and token; mark dropped. Idempotent.")
@guarded
def ws_drop(ws_id: WsIdArg, remote: RemoteOpt = None) -> None:
    svc = service(remote, ws_id)
    _emit(with_remote(svc, svc.drop(ws_id).to_dict()))


@ws_app.command("gc", help="Drop open workspaces past their ttl.")
@guarded
def ws_gc(remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, svc.gc()))


# ── events ───────────────────────────────────────────────────
@events_app.command("tail", help="Events from the meta log, newest first.")
@guarded
def events_tail(
    remote: RemoteOpt = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Only events after this meta oid.")
    ] = None,
) -> None:
    svc = service(remote)
    events, invalid = svc.events_report(since)
    _emit(with_remote(svc, {"events": [e.to_dict() for e in events], "invalid": invalid}))


# ── 진단·집계 ─────────────────────────────────────────────────
@app.command(
    "doctor",
    help="Check git, home, config, remote, hive, unrevoked tokens and ssh multiplexing. "
    "Exit 1 if any check fails.",
)
@guarded
def doctor(remote: RemoteOpt = None) -> None:
    report = doctor_report(remote)
    typer.echo(json.dumps(report, ensure_ascii=False))
    if not report["ok"]:
        raise typer.Exit(EXIT_CHECK_FAILED)


@app.command("stats", help="Counts by event kind and state, age of the oldest open workspace.")
@guarded
def stats(remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, summarize(svc)))


# ── mcp ──────────────────────────────────────────────────────
@app.command("mcp", help="Run the MCP server on stdio.")
def mcp_serve() -> None:
    from gitswarm.surfaces.mcp import serve

    serve()


def main() -> None:
    try:
        rc = app(standalone_mode=False)
    except ClickUsageError as e:
        typer.echo(json.dumps(usage_payload(e.format_message()), ensure_ascii=False))
        sys.exit(EXIT_USAGE)
    except typer.Abort:
        typer.echo(json.dumps(usage_payload("aborted")))
        sys.exit(EXIT_USAGE)

    sys.exit(rc if isinstance(rc, int) else 0)
