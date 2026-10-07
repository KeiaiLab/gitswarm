# src/gitswarm/service/workspace.py
"""Workspace 생명주기. 상태는 MetaStore(원격 git)에만 있다.

create ──▶ open ──publish──▶ published ──drop──▶ dropped
             └──────────────drop────────────────▶ dropped
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from ulid import ULID

from gitswarm.adapters.remote import Capability, RemoteAdapter, Scope
from gitswarm.adapters.select import adapter_for, repo_name
from gitswarm.config import load_config
from gitswarm.constants import (
    HEADS,
    MAX_TTL_S,
    TOKEN_ID_RE,
    TTL_FOREVER,
    WS_DIR,
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
OID_RE = re.compile(r"^[0-9a-f]{40}$")

EV_CREATED = "ws.created"
EV_PUBLISHED = "ws.published"
EV_DROPPED = "ws.dropped"
EV_EXPIRED = "ws.expired"

REQUIRED_STR_FIELDS = ("id", "base_ref", "base_oid", "branch")
OPTIONAL_STR_FIELDS = ("parent", "token_id")


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


def _valid_ttl(ttl: object) -> bool:
    """정수(bool 아님), 0(무기한) ≤ ttl ≤ MAX_TTL_S."""
    return isinstance(ttl, int) and not isinstance(ttl, bool) and 0 <= ttl <= MAX_TTL_S


def _check_expiry(ws: Workspace) -> None:
    ttl = ws.ttl_s
    if not _valid_ttl(ttl):
        raise InvalidState(f"{ws.id}: invalid ttl_s {ttl!r}")
    try:
        born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as e:
        raise InvalidState(f"{ws.id}: invalid created_at {ws.created_at!r}") from e
    if born.tzinfo is None:
        raise InvalidState(f"{ws.id}: created_at {ws.created_at!r} has no timezone")


def _expired(ws: Workspace, now: datetime) -> bool:
    if ws.ttl_s == TTL_FOREVER:
        return False
    born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
    try:
        return born + timedelta(seconds=ws.ttl_s) < now
    except OverflowError:  # 9999 년 근처 — 만료가 표현 범위 밖이면 아직 아니다
        return False


def _check_str_fields(ws: Workspace) -> None:
    """원격 레코드의 타입을 정규식·비교 전에 확인한다(숫자 id 가 TypeError 로 새지 않게)."""
    for name in REQUIRED_STR_FIELDS:
        if not isinstance(getattr(ws, name), str):
            raise InvalidState(f"malformed workspace record: {name} is not a string")
    for name in OPTIONAL_STR_FIELDS:
        v = getattr(ws, name)
        if v is not None and not isinstance(v, str):
            raise InvalidState(f"malformed workspace record: {name} is not a string")


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
        """원격 레코드 → Workspace. 어떤 모양이 와도 실패는 InvalidState 하나다."""
        try:
            d = json.loads(data)
            d["state"] = WsState(d["state"])
            known = {f.name for f in fields(cls)}
            ws = cls(**{k: v for k, v in d.items() if k in known})
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            raise InvalidState(f"malformed workspace record: {type(e).__name__}") from None

        _check_str_fields(ws)
        if not ULID_RE.fullmatch(ws.id):
            raise InvalidState(f"malformed workspace record: id {ws.id!r}")
        if ws.branch != ws_ref(ws.id):
            raise InvalidState(
                f"{ws.id}: stored branch {ws.branch!r} does not match {ws_ref(ws.id)!r}"
            )
        if ws.parent is not None and not ULID_RE.fullmatch(ws.parent):
            raise InvalidState(f"{ws.id}: invalid parent {ws.parent!r}")
        if ws.token_id is not None and not TOKEN_ID_RE.fullmatch(ws.token_id):
            raise InvalidState(f"{ws.id}: invalid token id {ws.token_id!r}")
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


def _parse_record(ws_id: str, data: bytes) -> Workspace:
    """ws/<ws_id>.json 의 내용. 안의 id 가 경로와 다르면 위조다."""
    ws = Workspace.from_json(data)
    if ws.id != ws_id:
        raise InvalidState(f"{ws_id}: record carries id {ws.id!r}")
    return ws


def _path_id(path: str) -> str:
    """meta 경로 ws/<id>.json → <id>."""
    return path.removeprefix(f"{WS_DIR}/").removesuffix(".json")


def _raw_dict(data: bytes | None) -> dict:
    """망가진 레코드에서 건질 수 있는 만큼. JSON 객체가 아니면 빈 사전."""
    try:
        d = json.loads(data or b"")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _salvage(ws_id: str, prev: bytes | None, now: datetime) -> Workspace:
    """강제 drop 이 쓰는 레코드. 유효한 필드만 옮기고 나머지는 기본값, 만료 필드는 새로."""
    if prev is None:
        raise NotFound(f"workspace {ws_id} not found")
    try:
        return _parse_record(ws_id, prev).with_state(WsState.DROPPED)
    except InvalidState:
        pass

    d = _raw_dict(prev)

    def pick(key: str, ok: Callable[[object], bool], default: object) -> Any:
        v = d.get(key)
        return v if ok(v) else default

    def is_str(v: object) -> bool:
        return isinstance(v, str)

    return Workspace(
        id=ws_id,
        state=WsState.DROPPED,
        base_ref=pick("base_ref", is_str, ""),
        base_oid=pick("base_oid", is_str, ""),
        branch=ws_ref(ws_id),
        agent=pick("agent", lambda v: isinstance(v, dict), {}),
        parent=pick("parent", lambda v: is_str(v) and bool(ULID_RE.fullmatch(v)), None),
        created_at=_iso(now),
        ttl_s=TTL_FOREVER,
        labels=pick("labels", lambda v: isinstance(v, dict), {}),
        token_id=pick("token_id", lambda v: is_str(v) and bool(TOKEN_ID_RE.fullmatch(v)), None),
    )


def _new_only(prev: bytes | None, ws: Workspace) -> Workspace:
    """생성은 빈 자리에만 쓴다 — 같은 id 가 이미 있으면 ULID 충돌이다."""
    if prev is not None:
        raise Conflict(f"workspace id {ws.id} already recorded")
    return ws


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
        return _parse_record(ws_id, data)

    def list(self, state: WsState | None) -> list[Workspace]:
        return self.list_report(state)[0]

    def list_report(self, state: WsState | None) -> tuple[list[Workspace], list[dict]]:
        """(state 에 맞는 유효 레코드, 망가진 레코드 [{id, detail}]). 한 tip 에서 읽는다.

        망가진 레코드 하나가 모두의 목록·회수를 멈추지 않게 따로 보고한다.
        """
        tip = self.store.tip()
        if tip is None:
            return [], []

        good: list[Workspace] = []
        invalid: list[dict] = []
        for path in self.store.list_at(tip, f"{WS_DIR}/"):
            ws_id = _path_id(path)
            data = self.store.read_at(tip, path)
            if data is None:
                raise InvalidState(f"meta {tip}: listed {path} is unreadable")
            try:
                ws = _parse_record(ws_id, data)
            except InvalidState as e:
                invalid.append({"id": ws_id, "detail": e.detail})
                continue
            if state is None or ws.state is state:
                good.append(ws)
        return good, invalid

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
        if not _valid_ttl(ttl_s):
            raise InvalidState(f"ttl_s must be an integer in [0, {MAX_TTL_S}], got {ttl_s!r}")
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

        # 이 뒤가 실패하면 기록 없는 브랜치·토큰이 남는다(gc 가 못 본다) — 되돌리고 원래 오류를 올린다
        token: tuple[str, str] | None = None
        try:
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
            self._record(ws_id, EV_CREATED, lambda prev: _new_only(prev, ws))
        except Exception:
            self._undo_create(branch, token)
            raise

        path = None
        if checkout is Checkout.WORKTREE:
            wt = self.hive.worktree_dir(ws_id)
            self.hive.git.worktree_add(wt, ws_branch(ws_id))
            path = str(wt)
        return CreateResult(ws_id, branch, base_oid, path, token[1] if token else None)

    def _undo_create(self, branch: str, token: tuple[str, str] | None) -> None:
        """create 보상. 각 단계는 실패해도 다음 단계로 간다 — 원래 오류를 가리지 않는다."""
        steps: list[tuple[str, Callable[[], object]]] = [
            ("delete remote branch", lambda: self.hive.git.delete_remote(branch, None)),
            ("delete local ref", lambda: self.hive.git.delete_ref(branch)),
        ]
        if token:
            steps.append(("revoke token", lambda: self.adapter.revoke_token(token[0])))

        for what, step in steps:
            try:
                step()
            except Exception as e:
                print(
                    f"gitswarm: create cleanup failed to {what}: {type(e).__name__}",
                    file=sys.stderr,
                )

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
        try:
            ws = self.get(ws_id)
        except InvalidState:
            return self._force_drop(ws_id)
        if ws.state is WsState.DROPPED:
            return ws
        return self._drop_as(ws, EV_DROPPED)

    def _force_drop(self, ws_id: str) -> Workspace:
        """망가진 레코드의 회수. 지우는 브랜치는 id 로 재계산한 것뿐이다."""
        raw = _raw_dict(self.store.read(meta_path(ws_id)))
        stored = raw.get("branch")
        branch = ws_ref(ws_id)
        if isinstance(stored, str) and stored != branch:
            raise InvalidState(
                f"{ws_id}: stored branch {stored!r} does not match {branch!r}; not dropping"
            )

        # 레코드를 못 믿으니 lease 기준도 없다 — 무조건 삭제(멱등)
        self.hive.git.delete_remote(branch, None)
        self._clear_local(ws_id, branch)
        token_id = raw.get("token_id")
        if isinstance(token_id, str) and TOKEN_ID_RE.fullmatch(token_id):
            self._revoke(token_id)

        now = self.clock()
        return self._record(ws_id, EV_DROPPED, lambda prev: _salvage(ws_id, prev, now))

    def _drop_as(self, ws: Workspace, kind: str) -> Workspace:
        # 원격 먼저, lease 로 — 이 호스트가 본 뒤 남이 옮긴 브랜치는 지우지 않는다(로컬도 그대로 둔다)
        seen = self.hive.git.rev_parse(tracking_ref(ws.branch))
        if not self.hive.git.delete_remote(ws.branch, seen):
            raise Conflict(
                f"{ws.branch} moved on remote since this host last saw it; not deleting. "
                "Publish or inspect it first."
            )

        self._clear_local(ws.id, ws.branch)
        if ws.token_id:
            self._revoke(ws.token_id)

        return self._transition(ws.id, kind, lambda cur: cur.with_state(WsState.DROPPED))

    def _clear_local(self, ws_id: str, branch: str) -> None:
        wt = self.hive.worktree_dir(ws_id)
        if wt.exists():
            self.hive.git.worktree_remove(wt)
        for ref in (branch, peek_ref(branch)):
            if self.hive.git.exists(ref):
                self.hive.git.delete_ref(ref)

    def _revoke(self, token_id: str) -> None:
        if Capability.TOKEN in self.adapter.capabilities():
            self.adapter.revoke_token(token_id)

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

        self._transition(ws_id, EV_PUBLISHED, lambda cur: cur.with_state(WsState.PUBLISHED))
        return head

    # ── 회수 ──────────────────────────────────────────────────
    def gc(self) -> dict:
        """ttl 이 지난 open 을 drop. 반환 {"expired": [id], "invalid": [{id, detail}]}.

        망가진 레코드는 건너뛰고 보고만 한다 — `ws drop <id>` 가 거둔다.
        """
        now = self.clock()
        open_ws, invalid = self.list_report(WsState.OPEN)
        expired = []
        for ws in open_ws:
            if not _expired(ws, now):
                continue

            # 옮겨진 브랜치 하나가 나머지 회수를 막지 않는다
            try:
                self._drop_as(ws, EV_EXPIRED)
            except Conflict as e:
                print(f"gitswarm: gc skipped {ws.id}: {e.detail}", file=sys.stderr)
                continue
            expired.append(ws.id)
        return {"expired": expired, "invalid": invalid}

    def events(self, since: str | None) -> list[Event]:
        if since is not None and not OID_RE.fullmatch(since):
            raise NotFound(f"invalid since oid: {since!r}")

        out = []
        for entry in self.store.log(since):
            kind, ws_id = parse_subject(entry.subject)
            raw = self.store.read_at(entry.oid, meta_path(ws_id)) or b"{}"
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise InvalidState(f"corrupt workspace record at {entry.oid}: {meta_path(ws_id)}")
            out.append(
                Event(kind=kind, id=ws_id, oid=entry.oid, at=entry.committed_at, payload=data)
            )
        return out

    # ── 공통 ──────────────────────────────────────────────────
    def _record(
        self, ws_id: str, kind: str, build: Callable[[bytes | None], Workspace]
    ) -> Workspace:
        """ws_id 레코드를 build(현재 내용) 로 쓴다. CAS 재시도마다 build 가 새 내용으로 다시 돈다."""
        built: list[Workspace] = []

        def transform(prev: bytes | None) -> bytes:
            ws = build(prev)
            built.append(ws)
            return ws.to_json()

        oid = self.store.apply(Change(meta_path(ws_id), transform, f"{kind} {ws_id}"))
        ws = built[-1]

        ev = Event(kind=kind, id=ws.id, oid=oid, at=_iso(self.clock()), payload=ws.to_dict())
        for sink in self.sinks:
            sink.emit(ev)
        return ws

    def _transition(
        self, ws_id: str, kind: str, mutate: Callable[[Workspace], Workspace]
    ) -> Workspace:
        """있는 레코드의 상태 전이. 판정(TRANSITIONS)은 매 재시도의 최신 레코드에 대해 한다."""

        def build(prev: bytes | None) -> Workspace:
            if prev is None:
                raise NotFound(f"workspace {ws_id} not found")
            return mutate(Workspace.from_json(prev))

        return self._record(ws_id, kind, build)


def open_service(remote_url: str, home: Path) -> WorkspaceService:
    """CLI·MCP 가 쓰는 조립 지점. Hive 가 없으면 NotFound."""
    hive = Hive.open(remote_url, home)
    config = load_config(home)
    return WorkspaceService(
        hive, MetaStore(hive.git), adapter_for(hive.url, config), sinks_from_config(config)
    )
