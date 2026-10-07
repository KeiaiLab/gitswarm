# tests/test_workspace_lifecycle.py
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.adapters.select import repo_name
from gitswarm.constants import META_REF, lease_ref, meta_path, tracking_ref, ws_ref
from gitswarm.errors import Conflict, InvalidState, NotFound, RemoteError
from gitswarm.service.workspace import (
    ULID_RE,
    Checkout,
    Workspace,
    WorkspaceService,
    WsState,
    open_service,
)
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore
from tests.conftest import git as git_cli
from tests.conftest import ls_remote_prefix


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def test_create_records_meta_and_pushes_branch(svc: WorkspaceService):
    r = svc.create(
        "main", {"name": "impl"}, ttl_s=10, from_ws=None, checkout=Checkout.NONE, labels={}
    )
    assert ULID_RE.fullmatch(r.id)
    assert r.branch == ws_ref(r.id)
    assert r.path is None and r.token is None
    assert svc.hive.git.ls_remote(r.branch) == r.base_oid
    ws = svc.get(r.id)
    assert ws.state is WsState.OPEN and ws.base_ref == "refs/heads/main"
    assert ws.agent == {"name": "impl"} and ws.ttl_s == 10


def test_create_with_checkout_gives_worktree(svc: WorkspaceService):
    r = svc.create(
        "refs/heads/main", {}, ttl_s=0, from_ws=None, checkout=Checkout.WORKTREE, labels={}
    )
    assert r.path and (Path(r.path) / "README.md").read_text() == "seed\n"


def test_create_from_ws_records_parent(svc: WorkspaceService):
    a = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    b = svc.create("main", {}, ttl_s=0, from_ws=a.id, checkout=Checkout.NONE, labels={"k": "v"})
    ws = svc.get(b.id)
    assert ws.parent == a.id and ws.base_oid == a.base_oid and ws.labels == {"k": "v"}


def test_create_unknown_base_is_not_found(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.create("nope", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    assert svc.list(None) == []


def test_list_filters_by_state(svc: WorkspaceService):
    a = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    b = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    svc.drop(a.id)
    assert [w.id for w in svc.list(WsState.OPEN)] == [b.id]
    assert {w.id for w in svc.list(None)} == {a.id, b.id}


def test_drop_twice_is_idempotent(svc: WorkspaceService):
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.WORKTREE, labels={})
    svc.hive.git.delete_remote(r.branch, expected=None)  # 다른 쪽이 먼저 지운 상황
    ws = svc.drop(r.id)
    assert ws.state is WsState.DROPPED
    assert svc.drop(r.id).state is WsState.DROPPED
    assert not Path(r.path).exists()
    assert svc.hive.git.exists(tracking_ref(r.branch)) is False


def test_get_unknown_is_not_found(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.get("01J00000000000000000000000")


def _creator(remote_url: str, home: str) -> None:
    open_service(remote_url, Path(home)).create("main", {}, 0, None, Checkout.NONE, {})


def _run_creators(remote_url: str, home: Path, n: int) -> list[int | None]:
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_creator, args=(remote_url, str(home))) for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    return [p.exitcode for p in procs]


def test_concurrent_create_all_recorded(remote_url: str, home: Path):
    Hive.init(remote_url, home)
    assert _run_creators(remote_url, home, 3) == [0, 0, 0]
    assert len(open_service(remote_url, home).list(WsState.OPEN)) == 3


LOCKSTEP_CREATORS = 8


def test_concurrent_create_records_match_branches(remote_url: str, home: Path):
    """N 동시 생성 — 전부 성공하고, 원격 ws 브랜치 수 = meta 레코드 수(고아 0)."""
    hive = Hive.init(remote_url, home)
    codes = _run_creators(remote_url, home, LOCKSTEP_CREATORS)
    assert codes == [0] * LOCKSTEP_CREATORS
    branches = ls_remote_prefix(hive.git.repo, ws_ref(""))
    records = open_service(remote_url, home).list(WsState.OPEN)
    assert len(records) == len(branches) == LOCKSTEP_CREATORS
    assert {w.branch for w in records} == set(branches)


def test_invalid_transition(svc: WorkspaceService):
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    svc.drop(r.id)
    with pytest.raises(InvalidState):
        svc.publish(r.id)


class TokenAdapter:
    def __init__(self) -> None:
        self.issued: list[tuple[str, str, Scope]] = []
        self.revoked: list[str] = []

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TOKEN})

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        self.issued.append((repo, ws_id, scope))
        return Token("42", "secret", scope)

    def revoke_token(self, token_id: str) -> None:
        self.revoked.append(token_id)


def test_token_issued_persisted_and_revoked(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    adapter = TokenAdapter()
    svc = WorkspaceService(hive, MetaStore(hive.git), adapter)
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    assert r.token == "secret"
    assert svc.get(r.id).token_id == "42"
    assert adapter.issued == [(repo_name(hive.url), r.id, Scope.WRITE)]
    svc.drop(r.id)
    assert adapter.revoked == ["42"]


def test_branch_push_rejected_writes_nothing(svc: WorkspaceService, monkeypatch):
    monkeypatch.setattr(type(svc.hive.git), "push", lambda *a, **k: False)
    with pytest.raises(InvalidState):
        svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    assert svc.list(None) == []
    assert svc.store.list("ws/") == []


FORGED_ID = "01J00000000000000000000001"


def _forge(svc: WorkspaceService) -> None:
    body = Workspace(
        id=FORGED_ID,
        state=WsState.OPEN,
        base_ref="refs/heads/main",
        base_oid="0" * 40,
        branch="--upload-pack=x",
    ).to_json()
    svc.store.apply(Change(meta_path(FORGED_ID), lambda _: body, f"ws.created {FORGED_ID}"))


def test_get_rejects_path_like_id(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.get("../../etc/passwd/000000000")
    with pytest.raises(NotFound):
        svc.get("0" * 12 + "/" + "0" * 13)


def test_forged_branch_fails_closed(svc: WorkspaceService):
    _forge(svc)
    with pytest.raises(InvalidState):
        svc.get(FORGED_ID)
    assert svc.list(None) == []
    _, invalid = svc.list_report(None)
    assert [i["id"] for i in invalid] == [FORGED_ID]
    # 저장된 브랜치가 재계산과 다르면 강제 drop 도 거절한다
    with pytest.raises(InvalidState):
        svc.drop(FORGED_ID)


def test_forged_token_id_fails_closed(svc: WorkspaceService):
    body = Workspace(
        id=FORGED_ID,
        state=WsState.OPEN,
        base_ref="refs/heads/main",
        base_oid="0" * 40,
        branch=ws_ref(FORGED_ID),
        token_id="../x",
    ).to_json()
    svc.store.apply(Change(meta_path(FORGED_ID), lambda _: body, f"ws.created {FORGED_ID}"))
    with pytest.raises(InvalidState):
        svc.get(FORGED_ID)


@pytest.mark.parametrize("bad", ["zz", "A" * 40, 7, ""])
def test_forged_published_oid_fails_closed(svc: WorkspaceService, bad: object):
    ws = Workspace(
        id=FORGED_ID,
        state=WsState.PUBLISHED,
        base_ref="refs/heads/main",
        base_oid="0" * 40,
        branch=ws_ref(FORGED_ID),
    )
    body = json.dumps({**ws.to_dict(), "published_oid": bad}).encode()
    svc.store.apply(Change(meta_path(FORGED_ID), lambda _: body, f"ws.created {FORGED_ID}"))
    with pytest.raises(InvalidState):
        svc.get(FORGED_ID)


def test_from_json_ignores_unknown_keys():
    ws = Workspace(
        id=FORGED_ID,
        state=WsState.OPEN,
        base_ref="refs/heads/main",
        base_oid="a",
        branch=ws_ref(FORGED_ID),
        created_at="2026-01-01T00:00:00Z",
    )
    d = ws.to_dict() | {"future": 1}
    assert Workspace.from_json(json.dumps(d).encode()) == ws


class _DropDuring:
    """A 의 meta push 직전에 B 의 drop 을 끝까지 돌린다 — 두 호스트의 경합을 결정적으로 재현."""

    def __init__(self, delegate, other: WorkspaceService, ws_id: str):
        self._git = delegate
        self._other = other
        self._ws_id = ws_id
        self.other_error: Exception | None = None
        self.fired = False

    def __getattr__(self, name):
        return getattr(self._git, name)

    def push(self, oid: str, ref: str, expected: str | None) -> bool:
        if ref == META_REF and not self.fired:
            self.fired = True
            try:
                self._other.drop(self._ws_id)
            except (Conflict, InvalidState) as e:
                self.other_error = e
        return self._git.push(oid, ref, expected)


def _subjects(svc: WorkspaceService, ws_id: str) -> list[str]:
    """그 workspace 의 meta 이벤트 종류, 오래된 것부터."""
    out = []
    for e in reversed(svc.store.log(since=None)):
        kind, _, rid = e.subject.partition(" ")
        if rid == ws_id:
            out.append(kind)
    return out


@pytest.mark.parametrize("b_saw_branch", [False, True])
def test_publish_drop_race_stays_consistent(remote_url: str, tmp_path: Path, b_saw_branch: bool):
    hive_a = Hive.init(remote_url, tmp_path / "host-a")
    hive_b = Hive.init(remote_url, tmp_path / "host-b")
    svc_b = WorkspaceService(hive_b, MetaStore(hive_b.git), PlainAdapter())
    svc_a = WorkspaceService(hive_a, MetaStore(hive_a.git), PlainAdapter())
    r = svc_a.create("main", {}, 0, None, Checkout.WORKTREE, {})
    if b_saw_branch:
        hive_b.git.fetch(r.branch)

    wt = Path(r.path)
    (wt / "a.txt").write_text("a\n")
    git_cli("add", "a.txt", cwd=wt)
    git_cli("commit", "-q", "-m", "a", cwd=wt)

    racer = _DropDuring(hive_a.git, svc_b, r.id)
    svc_a = WorkspaceService(Hive(hive_a.path, hive_a.url, racer), MetaStore(racer), PlainAdapter())
    a_error: Exception | None = None
    try:
        svc_a.publish(r.id)
    except (Conflict, InvalidState) as e:
        a_error = e
    assert racer.fired

    state = svc_b.get(r.id).state
    branch = hive_b.git.ls_remote(r.branch)
    kinds = _subjects(svc_b, r.id)
    # 명시적 drop 은 A 의 push 를 따라가 지운다(본 적 있든 없든) — A 의 published 전이가 거절된다
    assert state is WsState.DROPPED and branch is None and isinstance(a_error, InvalidState)
    assert racer.other_error is None
    # §3: dropped 다음에 published 가 오는 이력은 없다
    if "ws.dropped" in kinds:
        assert "ws.published" not in kinds[kinds.index("ws.dropped") :]


def test_meta_exhaustion_compensates_branch_and_token(
    remote_url: str, home: Path, monkeypatch: pytest.MonkeyPatch
):
    hive = Hive.init(remote_url, home)
    adapter = TokenAdapter()
    svc = WorkspaceService(hive, MetaStore(hive.git, sleep=lambda s: None), adapter)
    real_push = type(hive.git).push

    def push(self, oid: str, ref: str, expected: str | None) -> bool:
        if ref == META_REF:
            return False
        return real_push(self, oid, ref, expected)

    monkeypatch.setattr(type(hive.git), "push", push)
    with pytest.raises(Conflict):
        svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})

    # 기록 없는 브랜치·토큰을 남기지 않는다
    assert ls_remote_prefix(hive.git.repo, ws_ref("")) == {}
    assert len(adapter.issued) == 1 and adapter.revoked == ["42"]
    ws_id = adapter.issued[0][1]
    assert hive.git.exists(ws_ref(ws_id)) is False
    assert hive.git.exists(lease_ref(ws_ref(ws_id))) is False


@pytest.mark.parametrize("ttl", [-5, True, "7200", 1.5, 365 * 24 * 3600 + 1])
def test_create_rejects_bad_ttl(svc: WorkspaceService, ttl):
    with pytest.raises(InvalidState):
        svc.create("main", {}, ttl_s=ttl, from_ws=None, checkout=Checkout.NONE, labels={})
    assert svc.store.list("ws/") == []
    assert ls_remote_prefix(svc.hive.git.repo, ws_ref("")) == {}


def test_malformed_record_does_not_halt_list_gc_drop(svc: WorkspaceService):
    good = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    bad = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.WORKTREE, labels={})
    d = json.loads(svc.store.read(meta_path(bad.id))) | {"ttl_s": -5}
    body = (json.dumps(d) + "\n").encode()
    svc.store.apply(Change(meta_path(bad.id), lambda _: body, f"forge {bad.id}"))

    # 나쁜 레코드 하나가 목록·회수를 멈추지 않는다
    assert [w.id for w in svc.list(None)] == [good.id]
    _, invalid = svc.list_report(None)
    assert [i["id"] for i in invalid] == [bad.id] and invalid[0]["detail"]
    assert svc.gc() == {"expired": [], "invalid": invalid, "conflicted": []}

    # 그 레코드도 drop 으로 거둘 수 있다
    dropped = svc.drop(bad.id)
    assert dropped.state is WsState.DROPPED and dropped.ttl_s == 0
    assert svc.list_report(None)[1] == []
    assert svc.get(bad.id).state is WsState.DROPPED
    assert svc.hive.git.ls_remote(bad.branch) is None
    assert not Path(bad.path).exists()
    assert svc.get(good.id).state is WsState.OPEN


class LostCapabilityAdapter(TokenAdapter):
    """설정이 바뀌어 TOKEN 을 더는 광고하지 않지만 revoke 는 할 수 있다."""

    def capabilities(self) -> frozenset[Capability]:
        return frozenset()


def test_drop_revokes_even_without_token_capability(remote_url: str, home: Path, capsys):
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), TokenAdapter())
    a = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    b = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})

    lost = LostCapabilityAdapter()
    svc.adapter = lost
    svc.drop(a.id)
    assert lost.revoked == ["42"]

    # revoke 를 아예 못 하는 어댑터면 drop 은 끝나고 한 줄 남긴다
    svc.adapter = PlainAdapter()
    assert svc.drop(b.id).state is WsState.DROPPED
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "not revoked" in err[0]


class RevokeFailsAdapter(TokenAdapter):
    """원격이 revoke 를 거절한다(만료된 관리 토큰·5xx)."""

    def revoke_token(self, token_id: str) -> None:
        raise RemoteError("HTTP 500\nfrom forge")


def test_drop_survives_revoke_remote_error(remote_url: str, home: Path, capsys):
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), RevokeFailsAdapter())
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})

    # 브랜치는 이미 지웠다 — revoke 실패가 기록을 open 에 묶어 두지 않는다
    assert svc.drop(r.id).state is WsState.DROPPED
    assert svc.get(r.id).state is WsState.DROPPED
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("gitswarm: token 42 not revoked")


def test_open_service_creates_missing_hive(remote_url: str, home: Path):
    assert open_service(remote_url, home).list(None) == []
    assert open_service(remote_url, home).hive.path == Hive.open(remote_url, home).path


def test_create_result_names_branch_and_clone(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    assert r.branch_name == f"gitswarm/ws/{r.id}"
    assert r.clone == f"git clone -b {r.branch_name} {svc.url}"


def test_clone_command_quotes_odd_urls(tmp_path: Path, home: Path):
    from tests.conftest import git as g

    bare = tmp_path / "a b.git"
    g("init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)
    g("clone", "-q", str(bare), str(tmp_path / "w"), cwd=tmp_path)
    g("commit", "-q", "--allow-empty", "-m", "s", cwd=tmp_path / "w")
    g("push", "-q", "origin", "HEAD", cwd=tmp_path / "w")
    r = open_service(str(bare), home).create("main", {}, 0, None, Checkout.NONE, {})
    assert r.clone.endswith(f"'{bare}'")
