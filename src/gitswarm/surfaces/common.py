"""CLI 와 MCP 가 똑같이 쓰는 모양 맞춤 — 두 표면의 반환 계약이 갈라지지 않게 한 곳에 둔다."""

from __future__ import annotations

import base64

from gitswarm.service.workspace import WsState

USAGE_KIND = "Usage"


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
