# src/gitswarm/service/workspace.py
"""Workspace 생명주기. 상태는 MetaStore(원격 git)에만 있다.

create ──▶ open ──publish──▶ published ──drop──▶ dropped
             └──────────────drop────────────────▶ dropped
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
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
    MAX_RECORD_BYTES,
    MAX_TTL_S,
    SHOWN_TEXT_MAX,
    TOKEN_ID_RE,
    TRASH_REF,
    TTL_FOREVER,
    ULID_RE,
    WS_DIR,
    lease_ref,
    meta_path,
    peek_ref,
    tracking_ref,
    ws_branch,
    ws_ref,
)
from gitswarm.driver.git import Git, LogEntry
from gitswarm.errors import (
    TRUNCATED,
    Conflict,
    GitswarmError,
    InvalidState,
    NotFound,
    RemoteError,
    Unsupported,
)
from gitswarm.events import (
    EV_CREATED,
    EV_DROPPED,
    EV_EXPIRED,
    EV_PUBLISHED,
    EV_REVOKED,
    EVENT_KINDS,
    Event,
    Sink,
    parse_subject,
    sinks_from_config,
)
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore

RECORD_PATH_RE = re.compile(rf"{WS_DIR}/[0-9A-HJKMNP-TV-Z]{{26}}\.json")  # fullmatch 로만
UNEXPECTED_PATH = "unexpected meta path"
OID_RE = re.compile(r"^[0-9a-f]{40}$")

# 두 publish 경로가 같은 판정, 다음 행동만 다르다
NOTHING_PUBLISHED = "nothing published: branch is still at base"
COMMIT_IN_WORKTREE = "commit in the worktree first"
COMMIT_AND_PUSH = "commit and push to the branch first"

REQUIRED_STR_FIELDS = ("id", "base_ref", "base_oid", "branch")
OPTIONAL_STR_FIELDS = ("parent", "token_id", "published_oid")
TEXT_MAP_FIELDS = ("agent", "labels")  # 자유 텍스트: str → str 사전

MALFORMED = "malformed workspace record"
UNEXPECTED_SUBJECT = "unexpected subject"  # 제목은 되풀이하지 않는다 — 원격이 정한 텍스트다
BAD_CREATE = "invalid create input"


class WsState(StrEnum):
    OPEN = "open"
    PUBLISHED = "published"
    DROPPED = "dropped"


class LeaseMode(StrEnum):
    """branch 삭제의 lease. STRICT = 본 oid 만(gc). FOLLOW = 거절되면 지금 tip 으로 한 번 더(명시적 drop)."""

    STRICT = "strict"
    FOLLOW = "follow"


class Checkout(StrEnum):
    NONE = "none"
    WORKTREE = "worktree"


# 허용 전이. 같은 상태로의 재전이는 허용(멱등).
TRANSITIONS: dict[WsState, frozenset[WsState]] = {
    WsState.OPEN: frozenset({WsState.OPEN, WsState.PUBLISHED, WsState.DROPPED}),
    WsState.PUBLISHED: frozenset({WsState.PUBLISHED, WsState.DROPPED}),
    WsState.DROPPED: frozenset({WsState.DROPPED}),
}


def _shown(value: object) -> str:
    """오류 문구에 넣을 repr. 원격이 정한 값은 길이를 믿지 않는다 — 앞 SHOWN_TEXT_MAX 자만."""
    text = repr(value)
    if len(text) <= SHOWN_TEXT_MAX:
        return text
    return text[:SHOWN_TEXT_MAX] + TRUNCATED


def _no_workspace(ws_id: str) -> str:
    return f"workspace {ws_id} not found; `gitswarm ws list` shows ids"


def _check_id(ws_id: str) -> str:
    if not ULID_RE.fullmatch(ws_id):
        raise NotFound(f"invalid workspace id: {_shown(ws_id)}")
    return ws_id


def _valid_ttl(ttl: object) -> bool:
    """정수(bool 아님), 0(무기한) ≤ ttl ≤ MAX_TTL_S."""
    return isinstance(ttl, int) and not isinstance(ttl, bool) and 0 <= ttl <= MAX_TTL_S


def _check_expiry(ws: Workspace) -> None:
    ttl = ws.ttl_s
    if not _valid_ttl(ttl):
        raise InvalidState(f"{ws.id}: invalid ttl_s {_shown(ttl)}")
    try:
        born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as e:
        raise InvalidState(f"{ws.id}: invalid created_at {_shown(ws.created_at)}") from e
    if born.tzinfo is None:
        raise InvalidState(f"{ws.id}: created_at {_shown(ws.created_at)} has no timezone")


def _expired(ws: Workspace, now: datetime) -> bool:
    if ws.ttl_s == TTL_FOREVER:
        return False
    born = ws.born()
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


def _utf8(s: str) -> bool:
    """UTF-8 로 쓸 수 있는가. 외톨이 서로게이트("\\ud800")는 json.loads 를 지나도 못 쓴다."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _map_texts(v: object) -> list[str] | None:
    """str → str 사전이면 그 키·값 전부, 아니면 None."""
    if not isinstance(v, dict):
        return None
    texts = [*v.keys(), *v.values()]
    if not all(isinstance(t, str) for t in texts):
        return None
    return texts


def _check_text(what: str, texts: list[object], maps: dict[str, object]) -> None:
    """maps 는 str → str 사전, texts·maps 의 모든 str 은 UTF-8 이어야 한다.

    레코드는 UTF-8 JSON 으로 쓰인다 — 여기서 거르지 않으면 to_json 이 UnicodeEncodeError
    (GitswarmError 아님)로 drop·gc·publish 를 멈춘다.
    """
    texts = list(texts)
    for name, m in maps.items():
        items = _map_texts(m)
        if items is None:
            raise InvalidState(f"{what}: {name} is not a string map")
        texts += items

    if not all(_utf8(t) for t in texts if isinstance(t, str)):
        raise InvalidState(f"{what}: non-UTF-8 text")


def _check_record_text(ws: Workspace) -> None:
    texts = [getattr(ws, f.name) for f in fields(ws)]
    _check_text(MALFORMED, texts, {name: getattr(ws, name) for name in TEXT_MAP_FIELDS})


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
    published_oid: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    def to_json(self) -> bytes:
        """기록할 내용. 읽는 쪽이 거절할 크기면 쓰지 않는다(create 는 보상으로 되돌린다)."""
        data = (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode()
        if len(data) > MAX_RECORD_BYTES:
            raise InvalidState(f"record of {len(data)} bytes > {MAX_RECORD_BYTES}")
        return data

    @classmethod
    def from_json(cls, data: bytes) -> Workspace:
        """원격 레코드 → Workspace. 어떤 모양이 와도 실패는 InvalidState 하나다."""
        if len(data) > MAX_RECORD_BYTES:
            raise InvalidState(f"{MALFORMED}: {len(data)} bytes > {MAX_RECORD_BYTES}")
        try:
            d = json.loads(data)
            d["state"] = WsState(d["state"])
            known = {f.name for f in fields(cls)}
            ws = cls(**{k: v for k, v in d.items() if k in known})
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            raise InvalidState(f"malformed workspace record: {type(e).__name__}") from None

        _check_str_fields(ws)
        _check_record_text(ws)
        if not ULID_RE.fullmatch(ws.id):
            raise InvalidState(f"malformed workspace record: id {_shown(ws.id)}")
        if ws.branch != ws_ref(ws.id):
            raise InvalidState(
                f"{ws.id}: stored branch {_shown(ws.branch)} does not match {_shown(ws_ref(ws.id))}"
            )
        if ws.parent is not None and not ULID_RE.fullmatch(ws.parent):
            raise InvalidState(f"{ws.id}: invalid parent {_shown(ws.parent)}")
        if ws.token_id is not None and not TOKEN_ID_RE.fullmatch(ws.token_id):
            raise InvalidState(f"{ws.id}: invalid token id {_shown(ws.token_id)}")
        if ws.published_oid is not None and not OID_RE.fullmatch(ws.published_oid):
            raise InvalidState(
                f"malformed workspace record: published_oid {_shown(ws.published_oid)}"
            )
        _check_expiry(ws)
        return ws

    def born(self) -> datetime:
        """created_at 을 시각으로. from_json 이 이미 검증했다."""
        return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))

    def with_state(self, state: WsState) -> Workspace:
        if state not in TRANSITIONS[self.state]:
            hint = (
                "; dropped is final — create a new workspace"
                if self.state is WsState.DROPPED
                else ""
            )
            raise InvalidState(f"{self.id}: {self.state.value} → {state.value} not allowed{hint}")
        return Workspace(**{**asdict(self), "state": state})


@dataclass(frozen=True)
class CreateResult:
    id: str
    branch: str
    base_oid: str
    path: str | None
    token: str | None
    clone: str  # 다른 호스트가 그대로 실행할 명령

    @property
    def branch_name(self) -> str:
        """clone·checkout 이 받는 짧은 이름: gitswarm/ws/<id>."""
        return ws_branch(self.id)

    def to_dict(self) -> dict:
        return {**asdict(self), "branch_name": self.branch_name}


def _full_ref(ref: str) -> str:
    return ref if ref.startswith("refs/") else HEADS + ref


def _parse_record(ws_id: str, data: bytes) -> Workspace:
    """ws/<ws_id>.json 의 내용. 안의 id 가 경로와 다르면 위조다."""
    ws = Workspace.from_json(data)
    if ws.id != ws_id:
        raise InvalidState(f"{ws_id}: record carries id {_shown(ws.id)}")
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
        raise NotFound(_no_workspace(ws_id))
    try:
        return _parse_record(ws_id, prev).with_state(WsState.DROPPED)
    except InvalidState:
        pass

    d = _raw_dict(prev)

    def pick(key: str, ok: Callable[[object], bool], default: object) -> Any:
        v = d.get(key)
        return v if ok(v) else default

    def is_str(v: object) -> bool:
        return isinstance(v, str) and _utf8(v)

    def is_text_map(v: object) -> bool:
        texts = _map_texts(v)
        return texts is not None and all(map(_utf8, texts))

    return Workspace(
        id=ws_id,
        state=WsState.DROPPED,
        base_ref=pick("base_ref", is_str, ""),
        base_oid=pick("base_oid", is_str, ""),
        branch=ws_ref(ws_id),
        agent=pick("agent", is_text_map, {}),
        parent=pick("parent", lambda v: is_str(v) and bool(ULID_RE.fullmatch(v)), None),
        created_at=_iso(now),
        ttl_s=TTL_FOREVER,
        labels=pick("labels", is_text_map, {}),
        token_id=pick("token_id", lambda v: is_str(v) and bool(TOKEN_ID_RE.fullmatch(v)), None),
    )


class _TokenGoneError(Exception):
    """revoke 재시도 중 레코드의 token_id 가 이미 바뀌었다 — meta 에 아무것도 쓰지 않는다."""


def unrevoked(records: list[Workspace]) -> list[str]:
    """회수에 실패한 토큰을 아직 들고 있는 dropped 레코드의 id."""
    return [w.id for w in records if w.state is WsState.DROPPED and w.token_id is not None]


def _revoked_if(ws: Workspace, revoked: bool) -> Workspace:
    """회수됐으면 token_id 를 지운다 — 남은 token_id 는 회수 실패의 표시다."""
    return replace(ws, token_id=None) if revoked else ws


def _new_only(prev: bytes | None, ws: Workspace) -> Workspace:
    """생성은 빈 자리에만 쓴다 — 같은 id 가 이미 있으면 ULID 충돌이다."""
    if prev is not None:
        raise Conflict(f"workspace id {ws.id} already recorded")
    return ws


def _event_subject(subject: str) -> tuple[str, str] | None:
    """ "<kind> <id>" 이고 kind 가 닫힌 집합이면 (kind, id), 아니면 None."""
    kind, ws_id = parse_subject(subject)
    if kind not in EVENT_KINDS or not ULID_RE.fullmatch(ws_id):
        return None
    return kind, ws_id


def _to_event(
    entry: LogEntry, subject: tuple[str, str] | None, blobs: dict[tuple[str, str], bytes]
) -> Event:
    """meta 커밋 하나 → Event. 제목·레코드가 검증을 못 지나면 InvalidState."""
    if subject is None:
        raise InvalidState(UNEXPECTED_SUBJECT)
    kind, ws_id = subject

    # 커밋은 그 레코드를 썼다고 말한다 — 없으면 meta 가 위조됐다
    raw = blobs.get((entry.oid, meta_path(ws_id)))
    if raw is None:
        raise InvalidState(f"missing {meta_path(ws_id)}")
    ws = _parse_record(ws_id, raw)
    return Event(kind=kind, id=ws_id, oid=entry.oid, at=entry.committed_at, payload=ws.to_dict())


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

    @property
    def url(self) -> str:
        """이 서비스가 쓰는 원격 URL(hive 에 기록된 것)."""
        return self.hive.url

    def default_base(self) -> str | None:
        """원격 HEAD 가 가리키는 브랜치 — --base 를 생략했을 때의 기준."""
        return self.hive.git.default_branch()

    # ── 조회 ──────────────────────────────────────────────────
    def get(self, ws_id: str) -> Workspace:
        _check_id(ws_id)
        data = self.store.read(meta_path(ws_id))
        if data is None:
            raise NotFound(_no_workspace(ws_id))
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

        # 레코드 경로가 아닌 것(예: 이름에 줄바꿈)은 읽지 않고 보고만 한다 — push 권한이면 누구나 만든다
        paths: list[str] = []
        for path in self.store.list_at(tip, f"{WS_DIR}/"):
            if RECORD_PATH_RE.fullmatch(path):
                paths.append(path)
                continue
            invalid.append({"id": path, "detail": UNEXPECTED_PATH})

        blobs = self.store.read_many_at(tip, paths)  # 레코드 수와 무관하게 git 한 번
        for path in paths:
            ws_id = _path_id(path)
            data = blobs.get(path)
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
            raise InvalidState(f"ttl_s must be an integer in [0, {MAX_TTL_S}], got {_shown(ttl_s)}")
        # 기록할 수 없는 텍스트는 원격에 아무것도 만들기 전에 거른다
        _check_text(BAD_CREATE, [base_ref], {"agent": agent, "labels": labels})
        parent = self.get(from_ws) if from_ws else None
        src_ref = parent.branch if parent else _full_ref(base_ref)
        # peek: oid 만 필요하다 — tracking 을 옮기면 부모의 발행 기준이 남의 커밋으로 바뀐다
        base_oid = self.hive.git.peek(src_ref)
        if base_oid is None:
            raise NotFound(f"base ref {src_ref} not on remote; pass an existing branch as --base")

        ws_id = str(ULID())
        branch = ws_ref(ws_id)
        # 원격 브랜치가 먼저다 — 실패하면 meta 에 아무것도 남지 않는다
        if not self.hive.git.push(base_oid, branch, expected=None):
            raise InvalidState(f"branch {branch} already exists on remote")

        # 이 뒤가 실패하면 기록 없는 브랜치·토큰·worktree 가 남는다(gc 가 못 본다) — 되돌리고 올린다.
        # worktree 를 기록보다 먼저 — 기록 뒤의 실패는 되돌릴 수 없다(열린 레코드가 남는다)
        token: tuple[str, str] | None = None
        wt = self.hive.worktree_dir(ws_id) if checkout is Checkout.WORKTREE else None
        try:
            self.hive.git.update_ref(branch, base_oid)
            self.hive.git.update_ref(lease_ref(branch), base_oid)
            token = self._issue_token(ws_id)
            if wt is not None:
                self.hive.git.worktree_add(wt, ws_branch(ws_id))
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
        except Exception as e:
            self._undo_create(branch, token, wt)
            if isinstance(e, GitswarmError):
                raise type(e)(f"create {ws_id}: {e.detail}") from e
            raise

        clone = f"git clone -b {ws_branch(ws_id)} {shlex.quote(self.hive.url)}"
        return CreateResult(
            id=ws_id,
            branch=branch,
            base_oid=base_oid,
            path=str(wt) if wt else None,
            token=token[1] if token else None,
            clone=clone,
        )

    def _undo_create(self, branch: str, token: tuple[str, str] | None, wt: Path | None) -> None:
        """create 보상. 각 단계는 실패해도 다음 단계로 간다 — 원래 오류를 가리지 않는다."""
        steps: list[tuple[str, Callable[[], object]]] = []
        if wt is not None and wt.exists():
            steps.append(("remove worktree", lambda: self.hive.git.worktree_remove(wt)))
        steps += [
            ("delete remote branch", lambda: self.hive.git.delete_remote(branch, None)),
            ("delete local ref", lambda: self.hive.git.delete_ref(branch)),
            ("delete lease ref", lambda: self.hive.git.delete_ref(lease_ref(branch))),
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
            raise NotFound(f"invalid path: {_shown(path)}")
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
            # 늦은 push 가 되살린 브랜치·다른 호스트가 지운 뒤 남은 worktree 도 거둔다(둘 다 멱등)
            self._discard(ws, LeaseMode.FOLLOW)
            return self._retry_revoke(ws)
        return self._drop_as(ws, EV_DROPPED, LeaseMode.FOLLOW)

    def _retry_revoke(self, ws: Workspace) -> Workspace:
        """dropped 인데 token_id 가 남았으면 지난 회수가 실패한 것이다 — 다시 해 본다."""
        token_id = ws.token_id
        if token_id is None or not self._revoke(token_id):
            return ws

        # 그 사이 다른 재시도가 이미 지웠으면 쓸 것이 없다 — 변환을 멈추고 지금 레코드를 돌려준다
        def clear(cur: Workspace) -> Workspace:
            if cur.token_id != token_id:
                raise _TokenGoneError
            return replace(cur, token_id=None)

        try:
            return self._transition(ws.id, EV_REVOKED, clear)
        except _TokenGoneError:
            return self.get(ws.id)

    def _force_drop(self, ws_id: str) -> Workspace:
        """망가진 레코드의 회수. 지우는 브랜치는 id 로 재계산한 것뿐이다."""
        raw = _raw_dict(self.store.read(meta_path(ws_id)))
        stored = raw.get("branch")
        branch = ws_ref(ws_id)
        if isinstance(stored, str) and stored != branch:
            raise InvalidState(
                f"{ws_id}: stored branch {_shown(stored)} does not match {_shown(branch)}; not dropping"
            )

        # 레코드를 못 믿으니 lease 기준도 없다 — 무조건 삭제(멱등)
        self.hive.git.delete_remote(branch, None)
        self._clear_local(ws_id, branch)
        token_id = raw.get("token_id")
        revoked = True  # _salvage 는 유효한 token_id 만 옮긴다 — 그것만 회수 대상이다
        if isinstance(token_id, str) and TOKEN_ID_RE.fullmatch(token_id):
            revoked = self._revoke(token_id)

        now = self.clock()
        return self._record(
            ws_id, EV_DROPPED, lambda prev: _revoked_if(_salvage(ws_id, prev, now), revoked)
        )

    def _drop_as(self, ws: Workspace, kind: str, mode: LeaseMode) -> Workspace:
        # 자동 회수는 이 호스트 worktree 의 push 안 된 작업을 지우지 않는다
        if mode is LeaseMode.STRICT:
            self._check_local_work(ws)

        self._discard(ws, mode)
        revoked = self._revoke(ws.token_id) if ws.token_id else True

        return self._transition(
            ws.id, kind, lambda cur: _revoked_if(cur.with_state(WsState.DROPPED), revoked)
        )

    def _discard(self, ws: Workspace, mode: LeaseMode) -> None:
        """원격 브랜치를 lease 로 지우고 나서 로컬(worktree·ref)을 지운다. 원격이 지면 로컬도 둔다."""
        if not self._delete_branch(ws, mode):
            raise Conflict(f"{ws.branch} moved on remote while dropping; not deleting")
        self._clear_local(ws.id, ws.branch)

    def _delete_branch(self, ws: Workspace, mode: LeaseMode) -> bool:
        """원격 브랜치 삭제. 본 oid 로 lease, 거절(또는 본 적 없음)이면 지금 tip 을 본다.

        tip 이 없으면 삭제는 끝난 것이다 — STRICT 도 성공이다(meta CAS 가 진 drop 의 자가 치유).
        tip 이 base 면 잃을 커밋이 없다 — STRICT 도 그 tip 으로 지운다(남이 만든 workspace 의 회수).
        그 밖의 tip 이면 STRICT 는 거절, FOLLOW 는 그 tip 으로 한 번 더(lease) — 명시적 drop 은
        "그 workspace 를 버린다"는 의도라 남의 push 도 따라가 지운다.
        lease 는 본 것과 지우는 것 사이(TOCTOU)만 막는다 — 그 사이 또 옮겨지면 False.
        """
        git = self.hive.git
        branch = ws.branch
        seen = self._seen(branch)

        # 본 적 없으면 기준 lease 가 없다 — None 을 넘기면 무조건 삭제가 되므로 넘기지 않는다
        if seen is not None and git.delete_remote(branch, seen):
            return True
        current = git.peek(branch)
        if current is None:  # 이미 없다 — 지울 것이 없다
            return True
        if mode is LeaseMode.STRICT and current != ws.base_oid:
            return False
        return git.delete_remote(branch, current)

    def _check_local_work(self, ws: Workspace) -> None:
        """worktree 의 HEAD 가 이 호스트가 본 원격 tip 이 아니거나 커밋 안 된 변경이 있으면 Conflict."""
        wt = self.hive.worktree_dir(ws.id)
        if not wt.exists():
            return
        local = Git(wt)
        if local.rev_parse("HEAD") == self._seen(ws.branch) and not local.is_dirty():
            return
        raise Conflict(f"{ws.id} worktree has work not on the remote; not deleting")

    def _clear_local(self, ws_id: str, branch: str) -> None:
        git = self.hive.git
        wt = self.hive.worktree_dir(ws_id)

        # 지우기 전에 로컬 tip 을 reflog 에 남긴다 — 명시적 drop 도 되살릴 길은 둔다
        tips = [git.rev_parse(branch)]
        if wt.exists():
            tips.append(Git(wt).rev_parse("HEAD"))
            git.worktree_remove(wt)
        for oid in dict.fromkeys(t for t in tips if t):
            git.keep(TRASH_REF, oid, f"drop {ws_id}")

        for ref in (branch, peek_ref(branch), lease_ref(branch)):
            if self.hive.git.exists(ref):
                self.hive.git.delete_ref(ref)

    def _revoke(self, token_id: str) -> bool:
        """기록된 토큰은 항상 회수를 시도한다 — 어댑터 설정이 바뀌어도 토큰은 원격에 살아 있다.

        실패는 한 줄로 남기고 False — 브랜치는 이미 지웠으니 기록은 dropped 로 가되,
        token_id 를 남겨 stats·doctor 가 보게 한다(`ws drop <id>` 가 다시 시도한다).
        """
        try:
            self.adapter.revoke_token(token_id)
        except (Unsupported, RemoteError) as e:
            reason = e.detail.partition("\n")[0]
            print(f"gitswarm: token {token_id} not revoked: {reason}", file=sys.stderr)
            return False
        return True

    # ── 발행 ──────────────────────────────────────────────────
    def publish(self, ws_id: str) -> str:
        """원격 브랜치 tip 을 발행 결과로 기록한다. worktree 가 있으면 HEAD 를 먼저 push 한다."""
        ws = self.get(ws_id).with_state(WsState.PUBLISHED)
        wt = self.hive.worktree_dir(ws_id)
        oid = self._push_head(ws, wt) if wt.exists() else self._remote_tip(ws)

        self._transition(
            ws_id,
            EV_PUBLISHED,
            lambda cur: replace(cur.with_state(WsState.PUBLISHED), published_oid=oid),
        )
        return oid

    def _push_head(self, ws: Workspace, wt: Path) -> str:
        head = Git(wt).rev_parse("HEAD")
        if head is None:
            raise InvalidState(f"{ws.id} worktree has no HEAD")
        if head == ws.base_oid:
            raise InvalidState(f"{NOTHING_PUBLISHED}; {COMMIT_IN_WORKTREE}")

        if not self._push_leased(head, ws.branch):
            raise Conflict(
                f"{ws.branch} moved on remote; run "
                f"`git pull --rebase origin {ws_branch(ws.id)}` in the worktree"
            )
        self.hive.git.update_ref(lease_ref(ws.branch), head)
        return head

    def _remote_tip(self, ws: Workspace) -> str:
        """worktree 없는 workspace — 다른 호스트가 push 한 원격 tip. base 그대로면 발행할 것이 없다."""
        oid = self._published_rev(ws)
        if oid == ws.base_oid:
            raise InvalidState(f"{NOTHING_PUBLISHED}; {COMMIT_AND_PUSH}")
        return oid

    def _seen(self, branch: str) -> str | None:
        """이 호스트가 마지막으로 확인한 원격 oid. lease ref 가 없으면(남이 만든 것) tracking."""
        git = self.hive.git
        return git.rev_parse(lease_ref(branch)) or git.rev_parse(tracking_ref(branch))

    def _push_leased(self, head: str, branch: str) -> bool:
        """HEAD 를 lease push. 거절이어도 HEAD 가 원격 tip 을 이미 품었으면(pull --rebase 뒤)
        그 tip 을 lease 로 한 번 더 — 남의 커밋을 잃지 않음이 조상 관계로 증명된다."""
        git = self.hive.git
        if git.push(head, branch, expected=self._seen(branch)):
            return True
        remote = git.peek(branch)
        if remote is None or not git.is_ancestor(remote, head):
            return False
        return git.push(head, branch, expected=remote)

    # ── 회수 ──────────────────────────────────────────────────
    def gc(self) -> dict:
        """ttl 이 지난 open 을 drop. 반환 {"expired": [id], "invalid": [{id, detail}], "conflicted": [id]}.

        망가진 레코드(invalid)와 본 뒤 남이 옮긴 브랜치(conflicted)는 건너뛰고 보고만 한다 —
        자동 회수는 본 적 없는 작업을 지우지 않는다. 둘 다 `ws drop <id>` 가 거둔다.
        """
        now = self.clock()
        open_ws, invalid = self.list_report(WsState.OPEN)
        expired: list[str] = []
        conflicted: list[str] = []
        for ws in open_ws:
            if not _expired(ws, now):
                continue

            # 레코드 하나의 실패가 나머지 회수를 막지 않는다 — 옮겨진 브랜치는 conflicted,
            # 그 사이 사라지거나 망가진 레코드는 invalid
            try:
                self._drop_as(ws, EV_EXPIRED, LeaseMode.STRICT)
            except Conflict:
                conflicted.append(ws.id)
                continue
            except (NotFound, InvalidState) as e:
                invalid.append({"id": ws.id, "detail": e.detail})
                continue
            expired.append(ws.id)
        return {"expired": expired, "invalid": invalid, "conflicted": conflicted}

    def events(self, since: str | None) -> list[Event]:
        return self.events_report(since)[0]

    def events_report(self, since: str | None) -> tuple[list[Event], list[dict]]:
        """(이벤트, 못 읽은 meta 커밋 [{oid, detail}]), 새것부터. 나쁜 since 만 예외다.

        망가진 커밋 하나(손으로 고친 레코드·낯선 제목)가 모두의 로그·stats 를 멈추지 않게
        list 처럼 따로 보고한다. 제목·레코드는 원격이 정한다 — 검증한 것만 내보낸다.
        """
        if since is not None and not OID_RE.fullmatch(since):
            raise NotFound(f"invalid since oid: {_shown(since)}")

        # 제목이 경로를 정한다 — 닫힌 종류와 id 가 아니면 읽지 않는다
        entries = [(e, _event_subject(e.subject)) for e in self.store.log(since)]
        blobs = self.store.read_many([(e.oid, meta_path(s[1])) for e, s in entries if s])

        events: list[Event] = []
        invalid: list[dict] = []
        for entry, subject in entries:
            try:
                events.append(_to_event(entry, subject, blobs))
            except InvalidState as e:
                invalid.append({"oid": entry.oid, "detail": e.detail})
        return events, invalid

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

        # 기록은 이미 원격에 있다 — sink 오류를 올리면 호출자가 재시도해 중복을 만든다
        ev = Event(kind=kind, id=ws.id, oid=oid, at=_iso(self.clock()), payload=ws.to_dict())
        for sink in self.sinks:
            try:
                sink.emit(ev)
            except Exception as e:
                print(
                    f"gitswarm: sink {type(sink).__name__} failed: {type(e).__name__}",
                    file=sys.stderr,
                )
        return ws

    def _transition(
        self, ws_id: str, kind: str, mutate: Callable[[Workspace], Workspace]
    ) -> Workspace:
        """있는 레코드의 상태 전이. 판정(TRANSITIONS)은 매 재시도의 최신 레코드에 대해 한다."""

        def build(prev: bytes | None) -> Workspace:
            if prev is None:
                raise NotFound(_no_workspace(ws_id))
            return mutate(Workspace.from_json(prev))

        return self._record(ws_id, kind, build)


def open_service(remote_url: str, home: Path) -> WorkspaceService:
    """CLI·MCP 가 쓰는 조립 지점. Hive 가 없으면 만든다(멱등 — 있으면 원격을 타지 않는다)."""
    hive = Hive.init(remote_url, home)
    config = load_config(home)
    return WorkspaceService(
        hive, MetaStore(hive.git), adapter_for(hive.url, config), sinks_from_config(config)
    )
