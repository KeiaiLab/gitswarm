import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import meta_path
from gitswarm.errors import Conflict, InvalidState, NotFound
from gitswarm.service.workspace import Checkout, WorkspaceService, WsState
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore
from tests.conftest import git


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def _commit_file(wt: Path, name: str, text: str) -> str:
    (wt / name).write_text(text)
    git("add", "--", name, cwd=wt)
    git("commit", "-q", "-m", f"add {name}", cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


def test_read_and_tree_before_and_after_publish(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    assert svc.read(r.id, "README.md") == b"seed\n"
    head = _commit_file(wt, "new.txt", "hi\n")
    # 발행 전에는 원격에 없다
    with pytest.raises(NotFound):
        svc.read(r.id, "new.txt")
    assert svc.publish(r.id) == head
    assert svc.read(r.id, "new.txt") == b"hi\n"
    assert svc.get(r.id).state is WsState.PUBLISHED
    names = sorted(e["name"] for e in svc.tree(r.id))
    assert names == ["README.md", "new.txt"]
    assert svc.tree(r.id)[0]["kind"] in {"blob", "tree"}


def test_read_unicode_and_space_path(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    (wt / "문서 모음").mkdir()
    _commit_file(wt, "문서 모음/한글 파일.md", "ㄱ\n")
    svc.publish(r.id)
    assert svc.read(r.id, "문서 모음/한글 파일.md") == "ㄱ\n".encode()
    assert [e["name"] for e in svc.tree(r.id, "문서 모음")] == ["한글 파일.md"]


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", ""])
def test_read_rejects_parent_traversal(svc: WorkspaceService, bad: str):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(NotFound):
        svc.read(r.id, bad)


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd"])
def test_tree_rejects_parent_traversal(svc: WorkspaceService, bad: str):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(NotFound):
        svc.tree(r.id, bad)


def test_publish_without_worktree_is_invalid(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(InvalidState):
        svc.publish(r.id)


def test_publish_lease_conflict(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    # 다른 호스트가 같은 브랜치에 먼저 push 한 상황
    other = tmp_path / "other"
    branch = r.branch.removeprefix("refs/heads/")
    subprocess.run(
        ["git", "clone", "-q", "-b", branch, svc.hive.url, str(other)],
        check=True,
    )
    _commit_file(other, "theirs.txt", "x\n")
    git("push", "-q", "origin", "HEAD", cwd=other)
    _commit_file(Path(r.path), "mine.txt", "y\n")
    with pytest.raises(Conflict):
        svc.publish(r.id)


def test_gc_drops_expired_only(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now)
    old = svc.create("main", {}, 60, None, Checkout.NONE, {})
    forever = svc.create("main", {}, 0, None, Checkout.NONE, {})
    svc.clock = lambda: now + timedelta(seconds=61)
    assert svc.gc() == [old.id]
    assert svc.get(old.id).state is WsState.DROPPED
    assert svc.get(forever.id).state is WsState.OPEN
    assert svc.gc() == []


def test_read_does_not_move_publish_baseline(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    other = tmp_path / "other"
    branch = r.branch.removeprefix("refs/heads/")
    subprocess.run(["git", "clone", "-q", "-b", branch, svc.hive.url, str(other)], check=True)
    _commit_file(other, "theirs.txt", "x\n")
    git("push", "-q", "origin", "HEAD", cwd=other)
    theirs = git("rev-parse", "HEAD", cwd=other)

    assert svc.read(r.id, "README.md") == b"seed\n"
    _commit_file(Path(r.path), "mine.txt", "y\n")
    with pytest.raises(Conflict):
        svc.publish(r.id)
    assert svc.hive.git.ls_remote(r.branch) == theirs


def _forge_fields(svc: WorkspaceService, **over: object) -> str:
    r = svc.create("main", {}, 60, None, Checkout.NONE, {})
    d = json.loads(svc.store.read(meta_path(r.id))) | over
    body = (json.dumps(d) + "\n").encode()
    svc.store.apply(Change(meta_path(r.id), lambda _: body, f"forge {r.id}"))
    return r.id


@pytest.mark.parametrize(
    "over",
    [
        {"created_at": "garbage"},
        {"created_at": "2026-01-01T00:00:00"},
        {"created_at": 5},
        {"ttl_s": "7200"},
        {"ttl_s": True},
        {"ttl_s": -1},
    ],
)
def test_forged_expiry_fields_fail_closed(svc: WorkspaceService, over: dict):
    ws_id = _forge_fields(svc, **over)
    with pytest.raises(InvalidState):
        svc.get(ws_id)
    with pytest.raises(InvalidState):
        svc.gc()
