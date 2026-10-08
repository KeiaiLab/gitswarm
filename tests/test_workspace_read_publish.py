import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import TRASH_REF, lease_ref, meta_path, tracking_ref
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
    assert svc.get(r.id).published_oid == head
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


def _push_more(svc: WorkspaceService, branch_ref: str, tmp_path: Path, name: str) -> str:
    """다른 호스트가 같은 브랜치에 평범한 git 으로 push. 새 tip 을 돌려준다."""
    other = tmp_path / "other"
    if not other.exists():
        subprocess.run(
            [
                "git", "clone", "-q", "-b", branch_ref.removeprefix("refs/heads/"),
                svc.hive.url, str(other),
            ],
            check=True,
        )  # fmt: skip
    tip = _commit_file(other, name, "x\n")
    git("push", "-q", "origin", "HEAD", cwd=other)
    return tip


def test_publish_without_worktree_records_remote_tip(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    tip = _push_more(svc, r.branch, tmp_path, "theirs.txt")
    assert svc.publish(r.id) == tip
    ws = svc.get(r.id)
    assert ws.state is WsState.PUBLISHED
    assert ws.published_oid == tip
    assert svc.events(None)[0].payload["published_oid"] == tip


def test_publish_without_worktree_at_base_is_invalid(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(InvalidState, match="still at base; commit and push to the branch first"):
        svc.publish(r.id)
    assert svc.get(r.id).state is WsState.OPEN


def test_publish_without_worktree_missing_branch_is_not_found(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    svc.hive.git.delete_remote(r.branch, None)
    with pytest.raises(NotFound):
        svc.publish(r.id)


def test_republish_without_worktree_updates_tip(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    first = _push_more(svc, r.branch, tmp_path, "a.txt")
    assert svc.publish(r.id) == first
    second = _push_more(svc, r.branch, tmp_path, "b.txt")
    assert svc.publish(r.id) == second
    assert svc.get(r.id).published_oid == second


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
    assert svc.gc() == {"expired": [old.id], "invalid": [], "conflicted": []}
    assert svc.get(old.id).state is WsState.DROPPED
    assert svc.get(forever.id).state is WsState.OPEN
    assert svc.gc() == {"expired": [], "invalid": [], "conflicted": []}


@pytest.mark.parametrize("error", [NotFound, InvalidState])
def test_gc_isolates_a_failing_record(
    remote_url: str, home: Path, monkeypatch: pytest.MonkeyPatch, error: type
):
    hive = Hive.init(remote_url, home)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now)
    first = svc.create("main", {}, 60, None, Checkout.NONE, {})
    second = svc.create("main", {}, 60, None, Checkout.NONE, {})
    svc.clock = lambda: now + timedelta(seconds=61)

    # 처음 처리하는 레코드의 전이만 진다(남이 그 사이 지우거나 망가뜨렸다) — 다른 하나는 거둔다
    real = svc._transition
    failed: list[str] = []

    def transition(ws_id, kind, mutate):
        if not failed:
            failed.append(ws_id)
            raise error("record changed mid-gc")
        return real(ws_id, kind, mutate)

    monkeypatch.setattr(svc, "_transition", transition)
    out = svc.gc()

    # 같은 ms 의 ULID 는 순서가 무작위다 — 처리 순서로 둘을 가른다
    (lost,) = failed
    (kept,) = {first.id, second.id} - {lost}
    assert out == {
        "expired": [kept],
        "invalid": [{"id": lost, "detail": "record changed mid-gc"}],
        "conflicted": [],
    }
    assert svc.get(kept).state is WsState.DROPPED


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
    assert [i["id"] for i in svc.gc()["invalid"]] == [ws_id]


OTHER_ID = "01J00000000000000000000009"


def _drop_key(key: str):
    return lambda d: (json.dumps({k: v for k, v in d.items() if k != key}) + "\n").encode()


def _over(**kv):
    return lambda d: (json.dumps(d | kv) + "\n").encode()


@pytest.mark.parametrize(
    "forge",
    [
        _over(state="bogus"),
        _over(ttl_s=10**30),
        _over(token_id=5),
        _over(parent=5),
        _over(parent="x"),
        _over(branch=5),
        _over(id=5),
        _over(id=OTHER_ID),
        _drop_key("base_oid"),
        _drop_key("state"),
        lambda d: b"not json",
        lambda d: b"[1]",
        lambda d: b'"str"',
        lambda d: b"\xff\xfe",
    ],
)
def test_forged_records_are_invalid_state(svc: WorkspaceService, forge):
    r = svc.create("main", {}, 60, None, Checkout.NONE, {})
    body = forge(json.loads(svc.store.read(meta_path(r.id))))
    svc.store.apply(Change(meta_path(r.id), lambda _: body, f"forge {r.id}"))
    with pytest.raises(InvalidState):
        svc.get(r.id)
    good, invalid = svc.list_report(None)
    assert good == [] and [i["id"] for i in invalid] == [r.id]
    assert svc.gc()["invalid"] == invalid


def test_gc_far_future_expiry_does_not_overflow(svc: WorkspaceService):
    ws_id = _forge_fields(svc, created_at="9999-12-31T00:00:00Z", ttl_s=365 * 24 * 3600)
    assert svc.gc() == {"expired": [], "invalid": [], "conflicted": []}
    assert svc.get(ws_id).state is WsState.OPEN


def _foreign_push(svc: WorkspaceService, branch_ref: str, tmp_path: Path) -> str:
    """다른 호스트가 workspace 브랜치에 커밋을 push 한다. 반환 = 그 커밋."""
    other = tmp_path / "other"
    branch = branch_ref.removeprefix("refs/heads/")
    subprocess.run(["git", "clone", "-q", "-b", branch, svc.hive.url, str(other)], check=True)
    theirs = _commit_file(other, "theirs.txt", "x\n")
    git("push", "-q", "origin", "HEAD", cwd=other)
    return theirs


def test_plain_fetch_in_worktree_does_not_move_publish_lease(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    theirs = _foreign_push(svc, r.branch, tmp_path)
    wt = Path(r.path)
    git("fetch", "-q", "origin", cwd=wt)  # tracking ref 가 남의 커밋으로 옮겨진다
    _commit_file(wt, "mine.txt", "y\n")
    with pytest.raises(Conflict):
        svc.publish(r.id)
    assert svc.hive.git.ls_remote(r.branch) == theirs


def test_publish_after_pull_rebase_keeps_both(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    theirs = _foreign_push(svc, r.branch, tmp_path)
    wt = Path(r.path)
    _commit_file(wt, "mine.txt", "y\n")
    git("pull", "-q", "--rebase", "origin", r.branch.removeprefix("refs/heads/"), cwd=wt)

    head = svc.publish(r.id)
    assert svc.hive.git.ls_remote(r.branch) == head
    git("merge-base", "--is-ancestor", theirs, head, cwd=wt)  # 남의 커밋을 품었다
    assert svc.read(r.id, "theirs.txt") == b"x\n" and svc.read(r.id, "mine.txt") == b"y\n"
    assert svc.hive.git.rev_parse(lease_ref(r.branch)) == head

    svc.drop(r.id)
    assert svc.hive.git.exists(lease_ref(r.branch)) is False


def test_create_from_ws_keeps_parent_baseline(svc: WorkspaceService, tmp_path: Path):
    p = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    theirs = _foreign_push(svc, p.branch, tmp_path)
    child = svc.create("main", {}, 0, p.id, Checkout.NONE, {})
    assert child.base_oid == theirs

    # 자식 생성이 부모의 발행 기준을 옮기지 않는다
    assert svc.hive.git.rev_parse(tracking_ref(p.branch)) == p.base_oid
    _commit_file(Path(p.path), "mine.txt", "y\n")
    with pytest.raises(Conflict):
        svc.publish(p.id)
    assert svc.hive.git.ls_remote(p.branch) == theirs


def test_explicit_drop_follows_moved_branch(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    _foreign_push(svc, r.branch, tmp_path)  # 다른 호스트가 이 workspace 브랜치에 push
    assert svc.drop(r.id).state is WsState.DROPPED
    assert svc.hive.git.ls_remote(r.branch) is None
    assert svc.get(r.id).state is WsState.DROPPED


class _MoveAfterPeek:
    """drop 이 원격 tip 을 본 직후 다른 호스트가 또 push 한다."""

    def __init__(self, delegate, move):
        self._git = delegate
        self._move = move

    def __getattr__(self, name):
        return getattr(self._git, name)

    def peek(self, ref: str) -> str | None:
        oid = self._git.peek(ref)
        self._move()
        return oid


def test_explicit_drop_conflicts_when_branch_moves_again(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    _foreign_push(svc, r.branch, tmp_path)
    other = tmp_path / "other"

    def move() -> None:
        _commit_file(other, "again.txt", "z\n")
        git("push", "-q", "origin", "HEAD", cwd=other)

    racer = _MoveAfterPeek(svc.hive.git, move)
    svc.hive = Hive(svc.hive.path, svc.hive.url, racer)
    with pytest.raises(Conflict):
        svc.drop(r.id)
    assert svc.hive.git.ls_remote(r.branch) == git("rev-parse", "HEAD", cwd=other)
    assert svc.get(r.id).state is WsState.OPEN


def test_gc_reports_moved_branch_as_conflicted(remote_url: str, home: Path, tmp_path: Path):
    hive = Hive.init(remote_url, home)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now)
    r = svc.create("main", {}, 60, None, Checkout.NONE, {})
    theirs = _foreign_push(svc, r.branch, tmp_path)
    svc.clock = lambda: now + timedelta(seconds=61)

    # 자동 회수는 본 적 없는 작업을 지우지 않는다
    assert svc.gc() == {"expired": [], "invalid": [], "conflicted": [r.id]}
    assert svc.hive.git.ls_remote(r.branch) == theirs
    assert svc.get(r.id).state is WsState.OPEN


def _unseen_by_b(remote_url: str, home: Path, tmp_path: Path):
    """A 가 만들고 제3의 clone 이 push 한 workspace 를, 한 번도 fetch 안 한 호스트 B 가 다룬다."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hive_a = Hive.init(remote_url, home)
    svc_a = WorkspaceService(hive_a, MetaStore(hive_a.git), PlainAdapter(), clock=lambda: now)
    r = svc_a.create("main", {}, 60, None, Checkout.NONE, {})
    theirs = _foreign_push(svc_a, r.branch, tmp_path)

    hive_b = Hive.init(remote_url, tmp_path / "host-b")
    later = now + timedelta(seconds=61)
    svc_b = WorkspaceService(hive_b, MetaStore(hive_b.git), PlainAdapter(), clock=lambda: later)
    return svc_b, r, theirs


def test_gc_on_fresh_host_never_deletes_unseen_branch(remote_url: str, home: Path, tmp_path: Path):
    svc_b, r, theirs = _unseen_by_b(remote_url, home, tmp_path)
    assert svc_b.gc() == {"expired": [], "invalid": [], "conflicted": [r.id]}
    assert svc_b.hive.git.ls_remote(r.branch) == theirs
    assert svc_b.get(r.id).state is WsState.OPEN


def test_drop_on_fresh_host_follows_peeked_tip(remote_url: str, home: Path, tmp_path: Path):
    svc_b, r, _ = _unseen_by_b(remote_url, home, tmp_path)
    assert svc_b.drop(r.id).state is WsState.DROPPED
    assert svc_b.hive.git.ls_remote(r.branch) is None
    assert svc_b.get(r.id).state is WsState.DROPPED


def test_publish_from_worktree_at_base_is_invalid(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    with pytest.raises(InvalidState, match="still at base; commit in the worktree first"):
        svc.publish(r.id)
    assert svc.get(r.id).state is WsState.OPEN
    assert svc.hive.git.rev_parse(lease_ref(r.branch)) == r.base_oid


def test_publish_without_worktree_leaves_local_refs(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    git_ = svc.hive.git
    before = (git_.rev_parse(lease_ref(r.branch)), git_.rev_parse(tracking_ref(r.branch)))
    _push_more(svc, r.branch, tmp_path, "theirs.txt")
    svc.publish(r.id)
    after = (git_.rev_parse(lease_ref(r.branch)), git_.rev_parse(tracking_ref(r.branch)))
    assert after == before


def test_publish_without_worktree_when_dropped_is_invalid(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    svc.drop(r.id)
    with pytest.raises(InvalidState, match="dropped → published"):
        svc.publish(r.id)


def test_drop_keeps_published_oid(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    tip = _push_more(svc, r.branch, tmp_path, "theirs.txt")
    svc.publish(r.id)
    dropped = svc.drop(r.id)
    assert dropped.state is WsState.DROPPED and dropped.published_oid == tip
    assert svc.get(r.id).published_oid == tip


# ── 로컬 작업 보호(gc 는 worktree 의 push 안 된 작업을 지우지 않는다) ─────────
def _expired_with_worktree(remote_url: str, home: Path):
    """ttl 60 s 짜리 worktree workspace 를 만들고 시계를 61 s 뒤로 돌린다."""
    hive = Hive.init(remote_url, home)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now)
    r = svc.create("main", {}, 60, None, Checkout.WORKTREE, {})
    svc.clock = lambda: now + timedelta(seconds=61)
    return svc, r


def _assert_untouched(svc: WorkspaceService, r) -> None:
    assert svc.gc() == {"expired": [], "invalid": [], "conflicted": [r.id]}
    assert Path(r.path).exists()
    assert svc.hive.git.ls_remote(r.branch) == r.base_oid
    assert svc.hive.git.rev_parse(lease_ref(r.branch)) == r.base_oid
    assert svc.get(r.id).state is WsState.OPEN


def test_gc_keeps_worktree_with_unpushed_commit(remote_url: str, home: Path):
    svc, r = _expired_with_worktree(remote_url, home)
    mine = _commit_file(Path(r.path), "mine.txt", "y\n")
    _assert_untouched(svc, r)
    assert svc.hive.git.rev_parse(r.branch) == mine


def test_gc_keeps_dirty_worktree(remote_url: str, home: Path):
    svc, r = _expired_with_worktree(remote_url, home)
    (Path(r.path) / "draft.txt").write_text("uncommitted\n")
    _assert_untouched(svc, r)
    assert (Path(r.path) / "draft.txt").exists()


def test_gc_drops_clean_worktree_at_seen_tip(remote_url: str, home: Path):
    svc, r = _expired_with_worktree(remote_url, home)
    assert svc.gc() == {"expired": [r.id], "invalid": [], "conflicted": []}
    assert not Path(r.path).exists()


def test_drop_keeps_unpushed_commit_in_hive_reflog(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    mine = _commit_file(Path(r.path), "mine.txt", "y\n")
    assert svc.drop(r.id).state is WsState.DROPPED
    assert not Path(r.path).exists() and svc.hive.git.exists(r.branch) is False

    # 명시적 drop 은 지운다 — 그래도 hive 의 reflog 로 되살릴 수 있다
    log = git("reflog", "show", "--format=%H %gs", TRASH_REF, cwd=svc.hive.git.repo)
    assert f"{mine} drop {r.id}" in log.splitlines()
    assert git("config", "core.logAllRefUpdates", cwd=svc.hive.git.repo) == "true"


def test_gc_on_fresh_host_reclaims_branch_still_at_base(
    remote_url: str, home: Path, tmp_path: Path
):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hive_a = Hive.init(remote_url, home)
    svc_a = WorkspaceService(hive_a, MetaStore(hive_a.git), PlainAdapter(), clock=lambda: now)
    r = svc_a.create("main", {}, 60, None, Checkout.NONE, {})

    # B 는 이 브랜치를 본 적이 없다 — 그래도 tip 이 base 면 잃을 것이 없다
    hive_b = Hive.init(remote_url, tmp_path / "host-b")
    later = now + timedelta(seconds=61)
    svc_b = WorkspaceService(hive_b, MetaStore(hive_b.git), PlainAdapter(), clock=lambda: later)
    assert svc_b.gc() == {"expired": [r.id], "invalid": [], "conflicted": []}
    assert svc_b.hive.git.ls_remote(r.branch) is None
    assert svc_b.get(r.id).state is WsState.DROPPED


def test_read_of_directory_and_tree_of_file_are_not_found(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    (Path(r.path) / "src").mkdir()
    _commit_file(Path(r.path), "src/a.py", "a\n")
    svc.publish(r.id)

    # 종류가 틀린 경로는 git 실패(RemoteError, 재시도 대상)가 아니라 없는 것이다
    with pytest.raises(NotFound, match="src is a tree"):
        svc.read(r.id, "src")
    with pytest.raises(NotFound, match=r"src/a\.py is a blob"):
        svc.tree(r.id, "src/a.py")
    assert svc.read(r.id, "src/a.py") == b"a\n"
    assert [e["name"] for e in svc.tree(r.id, "src")] == ["a.py"]
