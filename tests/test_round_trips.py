"""원격 왕복 예산 — 서비스 동작 하나가 타는 fetch·push·ls-remote 수.

SSH 원격에서 왕복 하나는 RTT 이상이다. 예산을 넘는 변경은 지연으로 바로 드러난다.
"""

from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import META_REF, meta_path, ws_ref
from gitswarm.driver.git import Git
from gitswarm.service.workspace import Checkout, Workspace, WorkspaceService, WsState
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import count_network, git

# 동작별 상한: create = base peek·브랜치 push·meta fetch·meta push
BUDGET_CREATE = 4
BUDGET_PUBLISH = 4
BUDGET_DROP = 4
BUDGET_READ = 2
BUDGET_QUERY = 1
# 레코드 수와 무관한 git 프로세스 수: meta fetch(fetch + rev-parse) · 목록(ls-tree|log) · 일괄 읽기
BUDGET_SUBPROCESS = 4
MANY = 100
CREATED = "2026-01-01T00:00:00Z"


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def test_create_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch):
    calls = count_network(monkeypatch)
    svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    assert len(calls) <= BUDGET_CREATE, calls


def test_publish_and_drop_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    (wt / "n.txt").write_text("n\n")
    git("add", "n.txt", cwd=wt)
    git("commit", "-q", "-m", "n", cwd=wt)
    calls = count_network(monkeypatch)

    svc.publish(r.id)
    assert len(calls) <= BUDGET_PUBLISH, calls

    calls.clear()
    svc.drop(r.id)
    assert len(calls) <= BUDGET_DROP, calls


@pytest.mark.parametrize(
    "op,budget",
    [
        (lambda s, i: s.read(i, "README.md"), BUDGET_READ),
        (lambda s, i: s.tree(i), BUDGET_READ),
        (lambda s, i: s.get(i), BUDGET_QUERY),
        (lambda s, i: s.list(None), BUDGET_QUERY),
        (lambda s, i: s.events(None), BUDGET_QUERY),
        (lambda s, i: s.gc(), BUDGET_QUERY),
    ],
    ids=["read", "tree", "get", "list", "events", "gc"],
)
def test_query_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch, op, budget: int):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    calls = count_network(monkeypatch)
    op(svc, r.id)
    assert len(calls) <= budget, calls


def _seed_many(svc: WorkspaceService) -> None:
    """MANY 개 레코드를 커밋 MANY 개로 — 커밋마다 제 레코드를 쓴 이벤트다."""
    git = svc.hive.git
    ids = [f"01J{n:023d}" for n in range(MANY)]
    files = {}
    for ws_id in ids:
        ws = Workspace(
            ws_id, WsState.OPEN, "refs/heads/main", "0" * 40, ws_ref(ws_id), created_at=CREATED
        )
        files[meta_path(ws_id)] = git.hash_object(ws.to_json())
    tree = git.build_tree(files)

    tip: list[str] = []
    for ws_id in ids:
        tip = [git.commit_tree(tree, tip, f"ws.created {ws_id}")]
    assert git.push(tip[0], META_REF, expected=None)


@pytest.mark.parametrize("op", ["list", "events"])
def test_reads_spawn_constant_subprocesses(
    svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch, op: str
):
    _seed_many(svc)
    real = Git._run
    calls: list[str] = []

    def run(self, *args, **kw):
        calls.append(args[0])
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "_run", run)
    out = svc.list(None) if op == "list" else svc.events(None)
    assert len(out) == MANY
    assert len(calls) <= BUDGET_SUBPROCESS, calls
