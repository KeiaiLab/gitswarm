# src/gitswarm/service/workspace.py
"""Workspace 생명주기. 상태는 MetaStore(원격 git)에만 있다.

create ──▶ open ──publish──▶ published ──drop──▶ dropped
             └──────────────drop────────────────▶ dropped
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ulid import ULID

from gitswarm.adapters.remote import Capability, RemoteAdapter, Scope
from gitswarm.adapters.select import adapter_for, repo_name
from gitswarm.config import load_config
from gitswarm.constants import (
    HEADS,
    TTL_FOREVER,
    ULID_LEN,
    meta_path,
    ws_branch,
    ws_ref,
)
from gitswarm.errors import InvalidState, NotFound
from gitswarm.events import Event, Sink
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore

EV_CREATED = "ws.created"
EV_PUBLISHED = "ws.published"
EV_DROPPED = "ws.dropped"
EV_EXPIRED = "ws.expired"


class WsState(StrEnum):
    OPEN = "open"
    PUBLISHED = "published"
    DROPPED = "dropped"


class Checkout(StrEnum):
    NONE = "none"
    WORKTREE = "worktree"


# 허용 전이. 같은 상태로의 재전이는 허용(멱등).
TRANSITIONS: dict[WsState, frozenset[WsState]] = {
    WsState.OPEN: frozenset({WsState.OPEN, WsState.PUBLISHED, WsState.DROPPED}),
    WsState.PUBLISHED: frozenset({WsState.PUBLISHED, WsState.DROPPED}),
    WsState.DROPPED: frozenset({WsState.DROPPED}),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Workspace:
    id: str
    state: WsState
    base_ref: str
    base_oid: str
    branch: str
    agent: dict = field(default_factory=dict)
    parent: str | None = None
    created_at: str = ""
    ttl_s: int = TTL_FOREVER
    labels: dict = field(default_factory=dict)
    token_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    def to_json(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode()

    @classmethod
    def from_json(cls, data: bytes) -> Workspace:
        d = json.loads(data)
        d["state"] = WsState(d["state"])
        return cls(**d)

    def with_state(self, state: WsState) -> Workspace:
        if state not in TRANSITIONS[self.state]:
            raise InvalidState(f"{self.id}: {self.state.value} → {state.value} not allowed")
        return Workspace(**{**asdict(self), "state": state})


@dataclass(frozen=True)
class CreateResult:
    id: str
    branch: str
    base_oid: str
    path: str | None
    token: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _full_ref(ref: str) -> str:
    return ref if ref.startswith("refs/") else HEADS + ref


class WorkspaceService:
    def __init__(
        self,
        hive: Hive,
        store: MetaStore,
        adapter: RemoteAdapter,
        sinks: list[Sink] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.hive = hive
        self.store = store
        self.adapter = adapter
        self.sinks = sinks or []
        self.clock = clock

    # ── 조회 ──────────────────────────────────────────────────
    def get(self, ws_id: str) -> Workspace:
        if len(ws_id) != ULID_LEN:
            raise NotFound(f"invalid workspace id: {ws_id}")
        data = self.store.read(meta_path(ws_id))
        if data is None:
            raise NotFound(f"workspace {ws_id} not found")
        return Workspace.from_json(data)

    def list(self, state: WsState | None) -> list[Workspace]:
        tip = self.store.tip()
        if tip is None:
            return []
        out = []
        for path in self.store.list("ws/"):
            ws = Workspace.from_json(self.store.read_at(tip, path) or b"{}")
            if state is None or ws.state is state:
                out.append(ws)
        return out

    # ── 생성 ──────────────────────────────────────────────────
    def create(
        self,
        base_ref: str,
        agent: dict,
        ttl_s: int,
        from_ws: str | None,
        checkout: Checkout,
        labels: dict,
    ) -> CreateResult:
        parent = self.get(from_ws) if from_ws else None
        src_ref = parent.branch if parent else _full_ref(base_ref)
        base_oid = self.hive.git.fetch(src_ref)
        if base_oid is None:
            raise NotFound(f"base ref {src_ref} not on remote")

        ws_id = str(ULID())
        branch = ws_ref(ws_id)
        # 원격 브랜치가 먼저다 — 실패하면 meta 에 아무것도 남지 않는다
        if not self.hive.git.push(base_oid, branch, expected=None):
            raise InvalidState(f"branch {branch} already exists on remote")
        self.hive.git.update_ref(branch, base_oid)

        token = self._issue_token(ws_id)
        ws = Workspace(
            id=ws_id,
            state=WsState.OPEN,
            base_ref=_full_ref(base_ref) if not parent else parent.branch,
            base_oid=base_oid,
            branch=branch,
            agent=agent,
            parent=parent.id if parent else None,
            created_at=_iso(self.clock()),
            ttl_s=ttl_s,
            labels=labels,
            token_id=token[0] if token else None,
        )
        self._write(ws, EV_CREATED)

        path = None
        if checkout is Checkout.WORKTREE:
            wt = self.hive.worktree_dir(ws_id)
            self.hive.git.worktree_add(wt, ws_branch(ws_id))
            path = str(wt)
        return CreateResult(ws_id, branch, base_oid, path, token[1] if token else None)

    def _issue_token(self, ws_id: str) -> tuple[str, str] | None:
        if Capability.TOKEN not in self.adapter.capabilities():
            return None
        t = self.adapter.issue_token(repo_name(self.hive.url), ws_id, Scope.WRITE)
        return (t.id, t.secret)

    # ── 폐기 ──────────────────────────────────────────────────
    def drop(self, ws_id: str) -> Workspace:
        ws = self.get(ws_id)
        if ws.state is WsState.DROPPED:
            return ws

        wt = self.hive.worktree_dir(ws_id)
        if wt.exists():
            self.hive.git.worktree_remove(wt)
        self.hive.git.delete_remote(ws.branch)
        if self.hive.git.exists(ws.branch):
            self.hive.git.delete_ref(ws.branch)
        if ws.token_id and Capability.TOKEN in self.adapter.capabilities():
            self.adapter.revoke_token(ws.token_id)

        dropped = ws.with_state(WsState.DROPPED)
        self._write(dropped, EV_DROPPED)
        return dropped

    def publish(self, ws_id: str) -> str:
        ws = self.get(ws_id).with_state(WsState.PUBLISHED)
        return self._write(ws, EV_PUBLISHED)

    # ── 공통 ──────────────────────────────────────────────────
    def _write(self, ws: Workspace, kind: str) -> str:
        oid = self.store.apply(Change(meta_path(ws.id), ws.to_json(), f"{kind} {ws.id}"))
        ev = Event(kind=kind, id=ws.id, oid=oid, at=_iso(self.clock()), payload=ws.to_dict())
        for sink in self.sinks:
            sink.emit(ev)
        return oid


def open_service(remote_url: str, home: Path) -> WorkspaceService:
    """CLI·MCP 가 쓰는 조립 지점. Hive 가 없으면 NotFound."""
    from gitswarm.events import sinks_from_config  # Task 10

    hive = Hive.open(remote_url, home)
    config = load_config(home)
    return WorkspaceService(
        hive, MetaStore(hive.git), adapter_for(hive.url, config), sinks_from_config(config)
    )
