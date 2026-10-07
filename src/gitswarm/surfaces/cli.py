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
    base: Annotated[str | None, typer.Option("--base", help="default: remote HEAD")] = None,
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
    svc = service(remote)
    r = svc.create(resolve_base(svc, base, from_ws), agent_info, ttl, from_ws, mode, labels)
    _emit(with_remote(svc, r.to_dict()))


@ws_app.command("get")
@guarded
def ws_get(ws_id: str, remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, svc.get(ws_id).to_dict()))


@ws_app.command("list")
@guarded
def ws_list(
    remote: RemoteOpt = None,
    state: Annotated[WsState | None, typer.Option("--state")] = None,
) -> None:
    svc = service(remote)
    good, invalid = svc.list_report(state)
    _emit(with_remote(svc, {"workspaces": [w.to_dict() for w in good], "invalid": invalid}))


@ws_app.command("read")
@guarded
def ws_read(ws_id: str, path: str, remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, read_payload(path, svc.read(ws_id, path))))


@ws_app.command("tree")
@guarded
def ws_tree(
    ws_id: str, path: Annotated[str, typer.Argument()] = "", remote: RemoteOpt = None
) -> None:
    svc = service(remote)
    _emit(with_remote(svc, {"path": path, "entries": svc.tree(ws_id, path)}))


@ws_app.command("publish")
@guarded
def ws_publish(ws_id: str, remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, {"id": ws_id, "oid": svc.publish(ws_id)}))


@ws_app.command("drop")
@guarded
def ws_drop(ws_id: str, remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, svc.drop(ws_id).to_dict()))


@ws_app.command("gc")
@guarded
def ws_gc(remote: RemoteOpt = None) -> None:
    svc = service(remote)
    _emit(with_remote(svc, svc.gc()))


# ── events ───────────────────────────────────────────────────
@events_app.command("tail")
@guarded
def events_tail(
    remote: RemoteOpt = None, since: Annotated[str | None, typer.Option("--since")] = None
) -> None:
    svc = service(remote)
    _emit(with_remote(svc, {"events": [e.to_dict() for e in svc.events(since)]}))


# ── 진단·집계 ─────────────────────────────────────────────────
@app.command("doctor")
@guarded
def doctor(remote: RemoteOpt = None) -> None:
    """git·홈·설정·원격·hive·미회수 토큰·ssh 다중화를 점검한다. 하나라도 실패면 종료코드 1."""
    report = doctor_report(remote)
    typer.echo(json.dumps(report, ensure_ascii=False))
    if not report["ok"]:
        raise typer.Exit(EXIT_CHECK_FAILED)


@app.command("stats")
@guarded
def stats(remote: RemoteOpt = None) -> None:
    """이벤트 종류별·상태별 수, 가장 오래 열린 workspace 의 나이."""
    svc = service(remote)
    _emit(with_remote(svc, summarize(svc)))


# ── mcp ──────────────────────────────────────────────────────
@app.command("mcp")
def mcp_serve() -> None:
    """stdio MCP 서버를 띄운다."""
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
