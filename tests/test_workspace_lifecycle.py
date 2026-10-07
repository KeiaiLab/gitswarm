# tests/test_workspace_lifecycle.py
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.adapters.select import repo_name
from gitswarm.constants import ULID_LEN, meta_path, tracking_ref, ws_ref
from gitswarm.errors import InvalidState, NotFound
from gitswarm.service.workspace import (
    Checkout,
    Workspace,
    WorkspaceService,
    WsState,
    open_service,
)
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def test_create_records_meta_and_pushes_branch(svc: WorkspaceService):
    r = svc.create(
        "main", {"name": "impl"}, ttl_s=10, from_ws=None, checkout=Checkout.NONE, labels={}
    )
    assert len(r.id) == ULID_LEN
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
    svc.hive.git.delete_remote(r.branch)  # 다른 쪽이 먼저 지운 상황
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


def test_concurrent_create_all_recorded(remote_url: str, home: Path):
    Hive.init(remote_url, home)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_creator, args=(remote_url, str(home))) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert len(open_service(remote_url, home).list(WsState.OPEN)) == 3


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
    svc.store.apply(Change(meta_path(FORGED_ID), body, f"ws.created {FORGED_ID}"))


def test_get_rejects_path_like_id(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.get("../../etc/passwd/0000000000"[:ULID_LEN])
    with pytest.raises(NotFound):
        svc.get("0" * 12 + "/" + "0" * 13)


def test_forged_branch_fails_closed(svc: WorkspaceService):
    _forge(svc)
    with pytest.raises(InvalidState):
        svc.get(FORGED_ID)
    with pytest.raises(InvalidState):
        svc.list(None)


def test_forged_token_id_fails_closed(svc: WorkspaceService):
    body = Workspace(
        id=FORGED_ID,
        state=WsState.OPEN,
        base_ref="refs/heads/main",
        base_oid="0" * 40,
        branch=ws_ref(FORGED_ID),
        token_id="../x",
    ).to_json()
    svc.store.apply(Change(meta_path(FORGED_ID), body, f"ws.created {FORGED_ID}"))
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
