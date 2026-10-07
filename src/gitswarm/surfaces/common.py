"""CLI 와 MCP 가 똑같이 쓰는 모양 맞춤 — 두 표면의 반환 계약이 갈라지지 않게 한 곳에 둔다."""

from __future__ import annotations

import base64
from pathlib import Path

from gitswarm.service.discovery import discover_remote
from gitswarm.service.doctor import diagnose
from gitswarm.service.workspace import WorkspaceService, WsState, open_service
from gitswarm.store.hive import resolve_home

USAGE_KIND = "Usage"
NO_REMOTE = (
    "no remote: pass --remote (MCP: remote), set $GITSWARM_REMOTE, "
    "or run inside a git repo with an origin"
)
NO_DEFAULT_BASE = "remote HEAD names no branch; pass --base (MCP: base_ref)"


class UsageError(ValueError):
    """호출자가 잘못된 인자를 줬다. 표면이 Usage 페이로드로 바꾼다."""


def usage_payload(detail: str) -> dict:
    return {"ok": False, "error": {"kind": USAGE_KIND, "detail": detail}}


def read_payload(path: str, data: bytes) -> dict:
    """텍스트는 content, 이진은 content_b64."""
    try:
        return {"path": path, "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "content_b64": base64.b64encode(data).decode()}


def agent_dict(name: str, run: str) -> dict:
    return {k: v for k, v in (("name", name), ("run", run)) if v}


def parse_state(state: str | None) -> WsState | None:
    if not state:
        return None
    try:
        return WsState(state)
    except ValueError:
        raise UsageError(
            f"invalid state {state!r}; expected one of {[s.value for s in WsState]}"
        ) from None


def find_remote(remote: str | None) -> str | None:
    """주어진 원격, 없으면 cwd 로 찾은 원격."""
    return remote or discover_remote(Path.cwd(), resolve_home())


def resolve_remote(remote: str | None) -> str:
    """find_remote 인데 못 찾으면 Usage."""
    found = find_remote(remote)
    if found is None:
        raise UsageError(NO_REMOTE)
    return found


def doctor_report(remote: str | None) -> dict:
    """diagnose 결과. 원격을 찾았으면 무엇을 봤는지 `remote` 로 싣는다(없으면 원격 검사 생략)."""
    found = find_remote(remote)
    report = diagnose(found, resolve_home())
    return {**report, "remote": found} if found else report


def service(remote: str | None) -> WorkspaceService:
    return open_service(resolve_remote(remote), resolve_home())


def with_remote(svc: WorkspaceService, payload: dict) -> dict:
    """성공 결과에 실제로 쓴 원격을 싣는다 — 자동 발견이 무엇을 골랐는지 호출자가 본다."""
    return {**payload, "remote": svc.url}


def resolve_base(svc: WorkspaceService, base: str | None, from_ws: str | None) -> str:
    """--base, 없으면 원격 HEAD. from_ws 가 있으면 기준은 부모 브랜치라 묻지 않는다(무시되는 값)."""
    if base or from_ws:
        return base or ""
    found = svc.default_base()
    if found is None:
        raise UsageError(NO_DEFAULT_BASE)
    return found
