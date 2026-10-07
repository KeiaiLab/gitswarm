# src/gitswarm/service/workspace.py
"""Workspace 생명주기. 상태는 MetaStore(원격 git)에만 있다.

create ──▶ open ──publish──▶ published ──drop──▶ dropped
             └──────────────drop────────────────▶ dropped
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from ulid import ULID

from gitswarm.adapters.remote import Capability, RemoteAdapter, Scope
from gitswarm.adapters.select import adapter_for, repo_name
from gitswarm.config import load_config
from gitswarm.constants import (
    HEADS,
    TTL_FOREVER,
    meta_path,
    peek_ref,
    tracking_ref,
    ws_branch,
    ws_ref,
)
from gitswarm.driver.git import Git
from gitswarm.errors import Conflict, InvalidState, NotFound
from gitswarm.events import Event, Sink, parse_subject, sinks_from_config
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

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


def _check_id(ws_id: str) -> str:
    if not ULID_RE.fullmatch(ws_id):
        raise NotFound(f"invalid workspace id: {ws_id!r}")
    return ws_id


def _check_expiry(ws: Workspace) -> None:
    ttl = ws.ttl_s
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
        raise InvalidState(f"{ws.id}: invalid ttl_s {ttl!r}")
    try:
        born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as e:
        raise InvalidState(f"{ws.id}: invalid created_at {ws.created_at!r}") from e
    if born.tzinfo is None:
        raise InvalidState(f"{ws.id}: created_at {ws.created_at!r} has no timezone")


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
        known = {f.name for f in fields(cls)}
        ws = cls(**{k: v for k, v in d.items() if k in known})
        _check_id(ws.id)
        if ws.branch != ws_ref(ws.id):
            raise InvalidState(
                f"{ws.id}: stored branch {ws.branch!r} does not match {ws_ref(ws.id)!r}"
            )
        if ws.parent is not None:
            _check_id(ws.parent)
        _check_expiry(ws)
        return ws

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
        _check_id(ws_id)
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

    # ── 읽기 ──────────────────────────────────────────────────
    def _published_rev(self, ws: Workspace) -> str:
        oid = self.hive.git.peek(ws.branch)
        if oid is None:
            raise NotFound(f"branch {ws.branch} not on remote")
        return oid

    @staticmethod
    def _safe_path(path: str) -> str:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise NotFound(f"invalid path: {path!r}")
        return path

    def read(self, ws_id: str, path: str) -> bytes:
        ws = self.get(ws_id)
        rev = f"{self._published_rev(ws)}:{self._safe_path(path)}"
        if not self.hive.git.exists(rev):
            raise NotFound(f"{path} not in {ws_id}")
        return self.hive.git.cat_file(rev)

    def tree(self, ws_id: str, path: str = "") -> list[dict]:
        ws = self.get(ws_id)
        rev = self._published_rev(ws)
        if path:
            rev = f"{rev}:{self._safe_path(path)}"
            if not self.hive.git.exists(rev):
                raise NotFound(f"{path} not in {ws_id}")
        return [{"name": e.name, "kind": e.kind, "oid": e.oid} for e in self.hive.git.ls_tree(rev)]

    # ── 폐기 ──────────────────────────────────────────────────
    def drop(self, ws_id: str) -> Workspace:
        ws = self.get(ws_id)
        if ws.state is WsState.DROPPED:
            return ws
        return self._drop_as(ws, EV_DROPPED)

    def _drop_as(self, ws: Workspace, kind: str) -> Workspace:
        wt = self.hive.worktree_dir(ws.id)
        if wt.exists():
            self.hive.git.worktree_remove(wt)
        self.hive.git.delete_remote(ws.branch)
        if self.hive.git.exists(ws.branch):
            self.hive.git.delete_ref(ws.branch)
        if self.hive.git.exists(peek_ref(ws.branch)):
            self.hive.git.delete_ref(peek_ref(ws.branch))
        if ws.token_id and Capability.TOKEN in self.adapter.capabilities():
            self.adapter.revoke_token(ws.token_id)

        dropped = ws.with_state(WsState.DROPPED)
        self._write(dropped, kind)
        return dropped

    # ── 발행 ──────────────────────────────────────────────────
    def publish(self, ws_id: str) -> str:
        ws = self.get(ws_id).with_state(WsState.PUBLISHED)
        wt = self.hive.worktree_dir(ws_id)
        if not wt.exists():
            raise InvalidState(f"{ws_id} has no local worktree; push the branch with git instead")

        head = Git(wt).rev_parse("HEAD")
        if head is None:
            raise InvalidState(f"{ws_id} worktree has no HEAD")

        # 기대값 = 마지막으로 알던 원격. 지금 fetch 하면 남의 커밋을 덮어쓰게 되므로 하지 않는다.
        last_known = self.hive.git.rev_parse(tracking_ref(ws.branch))
        if not self.hive.git.push(head, ws.branch, expected=last_known):
            raise Conflict(f"{ws.branch} moved on remote; run `git pull --rebase` in the worktree")

        self._write(ws, EV_PUBLISHED)
        return head

    # ── 회수 ──────────────────────────────────────────────────
    def gc(self) -> list[str]:
        now = self.clock()
        expired = []
        for ws in self.list(WsState.OPEN):
            if ws.ttl_s == TTL_FOREVER:
                continue
            born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
            if born + timedelta(seconds=ws.ttl_s) >= now:
                continue
            self._drop_as(ws, EV_EXPIRED)
            expired.append(ws.id)
        return expired

    def events(self, since: str | None) -> list[Event]:
        out = []
        for entry in self.store.log(since):
            kind, ws_id = parse_subject(entry.subject)
            raw = self.store.read_at(entry.oid, meta_path(ws_id)) or b"{}"
            out.append(
                Event(
                    kind=kind,
                    id=ws_id,
                    oid=entry.oid,
                    at=entry.committed_at,
                    payload=json.loads(raw),
                )
            )
        return out

    # ── 공통 ──────────────────────────────────────────────────
    def _write(self, ws: Workspace, kind: str) -> str:
        oid = self.store.apply(Change(meta_path(ws.id), ws.to_json(), f"{kind} {ws.id}"))
        ev = Event(kind=kind, id=ws.id, oid=oid, at=_iso(self.clock()), payload=ws.to_dict())
        for sink in self.sinks:
            sink.emit(ev)
        return oid


def open_service(remote_url: str, home: Path) -> WorkspaceService:
    """CLI·MCP 가 쓰는 조립 지점. Hive 가 없으면 NotFound."""
    hive = Hive.open(remote_url, home)
    config = load_config(home)
    return WorkspaceService(
        hive, MetaStore(hive.git), adapter_for(hive.url, config), sinks_from_config(config)
    )
